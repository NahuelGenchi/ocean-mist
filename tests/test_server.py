"""The server the page talks to (player.py): only the page's own requests are
answered, nothing a file's tags say reaches a header (#74), and a command of
the wrong shape gets a 400 (#116)."""
import http.client
import io
import json
import os
import secrets
import time
import unittest
from unittest import mock

import tests  # noqa: F401  (scratch folders first)

import player
import tags
from library import Library


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = Library()
        cls.token = secrets.token_urlsafe(16)
        cls.server = player.start_server(cls.lib, cls.token, music=object())
        cls.port = cls.server.server_address[1]
        cls.here = f"127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def ask(self, method, path, body=None, host=None, origin=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or self.here}
        if origin:
            headers["Origin"] = origin
        if body is not None:
            body = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=headers)
        answer = conn.getresponse()
        data = answer.read()
        conn.close()
        return answer, data

    def test_the_page_comes_with_its_policy(self):
        answer, data = self.ask("GET", f"/{self.token}/")
        self.assertEqual(answer.status, 200)
        self.assertEqual(answer.getheader("Content-Security-Policy"), player.PAGE_POLICY)
        self.assertIn(b"ui/app.js", data)

    def test_the_policy_allows_only_the_page_itself(self):
        rules = dict(part.strip().split(" ", 1) for part in player.PAGE_POLICY.split(";"))
        self.assertEqual(rules["script-src"], "'self'")
        self.assertEqual(rules["connect-src"], "'self'")
        self.assertEqual(rules["default-src"], "'self'")
        self.assertEqual(rules["object-src"], "'none'")
        self.assertNotIn("unsafe-eval", player.PAGE_POLICY)
        self.assertNotIn("http", player.PAGE_POLICY)

    def test_without_the_token_nothing_is_answered(self):
        for path in ("/", "/ui/app.js", "/dev/", f"/{self.token[:-1]}/", "/api/state"):
            self.assertEqual(self.ask("GET", path)[0].status, 403, path)

    def test_another_host_is_refused(self):
        for host in ("localhost:%d" % self.port, "evil.example:%d" % self.port, "127.0.0.1", "127.0.0.1:1"):
            self.assertEqual(self.ask("GET", f"/{self.token}/api/state", host=host)[0].status, 403, host)
        self.assertEqual(self.ask("GET", f"/{self.token}/api/state")[0].status, 200)

    def test_files_outside_the_interface_are_refused(self):
        for path in ("ui/../player.py", "ui/%2e%2e/player.py", "ui/../../../etc/passwd"):
            self.assertIn(self.ask("GET", f"/{self.token}/{path}")[0].status, (403, 404), path)
        self.assertEqual(self.ask("GET", f"/{self.token}/ui/app.js")[0].status, 200)

    def test_a_command_from_another_page_is_refused(self):
        self.lib.settings["volume"] = 0.5
        for origin in ("http://evil.example", "null", f"http://localhost:{self.port}", "https://" + self.here):
            answer, _ = self.ask("POST", f"/{self.token}/api/cmd",
                                 {"cmd": "settings", "values": {"volume": 0.1}}, origin=origin)
            self.assertEqual(answer.status, 403, origin)
        self.assertEqual(self.lib.settings["volume"], 0.5)
        answer, _ = self.ask("POST", f"/{self.token}/api/cmd",
                             {"cmd": "settings", "values": {"volume": 0.2}}, host="evil.example")
        self.assertEqual(answer.status, 403)
        self.assertEqual(self.lib.settings["volume"], 0.5)

    def test_the_page_and_the_host_can_send_commands(self):
        for origin, volume in ((f"http://{self.here}", 0.3), (None, 0.4)):
            answer, data = self.ask("POST", f"/{self.token}/api/cmd",
                                    {"cmd": "settings", "values": {"volume": volume, "folders": ["/"]}}, origin=origin)
            self.assertEqual(answer.status, 200)
            self.assertEqual(json.loads(data), {"ok": True})
            self.assertEqual(self.lib.settings["volume"], volume)
            self.assertNotIn("folders", self.lib.settings)   # only the page's own settings

    def test_a_cover_goes_out_only_as_a_picture_type(self):
        song, = self.lib.add_streams([{"video": "video000001", "title": "T"}])
        covers = {}
        self.lib.cover = lambda track_id: covers.get(track_id)
        try:
            for mime, sent in (("image/png", "image/png"), ("text/html", "application/octet-stream"),
                               ("image/png\r\nX-Injected: 1", "application/octet-stream")):
                covers[song["id"]] = (mime, b"\x89PNG\r\n\x1a\n...")
                answer, data = self.ask("GET", f"/{self.token}/art/{song['id']}")
                self.assertEqual(answer.status, 200)
                self.assertEqual(answer.getheader("Content-Type"), sent, mime)
                self.assertIsNone(answer.getheader("X-Injected"))
        finally:
            del self.lib.cover


class Malformed(unittest.TestCase):
    """A command whose body is not what the page sends gets an answer, not a dropped connection (#116)."""

    @classmethod
    def setUpClass(cls):
        cls.lib = Library()
        cls.token = secrets.token_urlsafe(16)
        cls.server = player.start_server(cls.lib, cls.token, music=object())
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def post(self, body, length=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", f"/{self.token}/api/cmd")
        conn.putheader("Host", f"127.0.0.1:{self.port}")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(len(body)) if length is None else length)
        conn.endheaders()
        conn.send(body)
        answer = conn.getresponse()
        answer.read()
        conn.close()
        return answer

    def test_a_body_of_the_wrong_shape_is_a_bad_request(self):
        for body in (b"[]", b'"x"', b"5", b"null", b"{}", b'{"cmd": 5}', b"{oops",
                     b'{"cmd": "duration", "id": 1, "seconds": "x"}',
                     b'{"cmd": "settings", "values": [1, 2]}',
                     b'{"cmd": "remove", "ids": 5}'):
            with self.subTest(body=body):
                self.assertEqual(self.post(body).status, 400)

    def test_a_length_that_is_not_one_is_refused_without_waiting(self):
        for length, status in (("-1", 400), ("x", 400), (str(player.BODY_LIMIT + 1), 413)):
            with self.subTest(length=length):
                self.assertEqual(self.post(b"{}", length).status, status)

    def test_what_the_page_sends_is_answered_as_before(self):
        answer = self.post(json.dumps({"cmd": "settings", "values": {"vis": 2}}).encode())
        self.assertEqual(answer.status, 200)
        self.assertEqual(self.lib.settings.get("vis"), 2)

    def test_every_answer_says_its_type_is_what_it_is(self):
        self.assertEqual(self.post(b"[]").getheader("X-Content-Type-Options"), "nosniff")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        for path in (f"/{self.token}/", f"/{self.token}/ui/app.js", "/nothing"):
            conn.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}"})
            answer = conn.getresponse()
            answer.read()
            self.assertEqual(answer.getheader("X-Content-Type-Options"), "nosniff", path)
        conn.close()


class DebugLog(unittest.TestCase):
    """With OCEAN_MIST_DEBUG the server logs its requests, but not the page's token nor what a request's query
    says (a search's words, a cover's address): a log is handed over with bug reports (#118)."""

    def test_the_log_says_neither_the_token_nor_the_words(self):
        class Music:   # YouTube Music, stood in: a search that finds nothing
            def search(self, words, kind="songs"):
                return {"items": [], "more": None}
        lib, token = Library(), secrets.token_urlsafe(16)
        server = player.start_server(lib, token, music=Music())
        port = server.server_address[1]
        log = io.StringIO()
        try:
            with mock.patch.dict(os.environ, {"OCEAN_MIST_DEBUG": "1"}), mock.patch("sys.stderr", log):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                for path in (f"/{token}/yt/search?q=invented+secret+words&kind=songs",
                             f"/{token}/ui/app.js?u=https%3A%2F%2Fi.ytimg.com%2Fvi%2Finvented%2Fhq.jpg",
                             f"/{token}/nothing?also=invented"):
                    conn.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
                    conn.getresponse().read()
                conn.close()
                time.sleep(0.2)   # (the server's thread writes the last line after it answers)
        finally:
            server.shutdown()
            server.server_close()
        said = log.getvalue()
        self.assertEqual(len(said.strip().splitlines()), 3, said)
        for secret in (token, "invented", "secret", "words", "ytimg"):
            self.assertNotIn(secret, said)
        self.assertIn("/TOKEN/yt/search?…", said)
        self.assertRegex(said, r"/TOKEN/nothing\?… HTTP/1.1\" 404")


class PictureType(unittest.TestCase):
    """A tag's own words for its picture's type never go out as they are."""

    def test_the_picture_says_its_type(self):
        self.assertEqual(tags.picture_type("text/html", b"\xff\xd8\xff\xe0"), "image/jpeg")
        self.assertEqual(tags.picture_type("image/jpeg", b"\x89PNG\r\n\x1a\n"), "image/png")
        self.assertEqual(tags.picture_type("", b"RIFF\0\0\0\0WEBPVP8 "), "image/webp")
        self.assertEqual(tags.picture_type("", b"GIF89a"), "image/gif")

    def test_otherwise_only_a_known_type_passes(self):
        self.assertEqual(tags.picture_type("image/webp", b"????"), "image/webp")
        for mime in ("image/png\r\nX: 1", "text/html", "image/svg+xml", ""):
            self.assertIn(tags.picture_type(mime, b"????"), tags.PICTURE_TYPES, mime)
            self.assertEqual(tags.picture_type(mime, b"????"), "image/jpeg")


if __name__ == "__main__":
    unittest.main()
