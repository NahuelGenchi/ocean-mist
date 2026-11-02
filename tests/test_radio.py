"""Internet radio (#83): a SHOUTcast v1 server's "ICY 200 OK", song titles that
are not UTF-8, and station playlists that name their stream by a relative
address. Local stand-in stations on 127.0.0.1 only."""
import http.server
import os
import socket
import threading
import unittest
from unittest import mock

import tests  # noqa: F401  (scratch folders first)

import radio

AUDIO = bytes(range(256)) * 8


def icy_station(status=b"ICY 200 OK"):
    """A station that answers as a SHOUTcast v1 server does, then sends AUDIO and closes. Returns its address."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(4)

    def answer():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                request = b""
                while b"\r\n\r\n" not in request:
                    data = conn.recv(4096)
                    if not data:
                        break
                    request += data
                conn.sendall(status + b"\r\nicy-name:Invented FM\r\ncontent-type:audio/mpeg\r\nicy-metaint:8192\r\n\r\n" + AUDIO)
    threading.Thread(target=answer, daemon=True).start()
    return server, f"http://127.0.0.1:{server.getsockname()[1]}/"


class Files(http.server.BaseHTTPRequestHandler):
    """Station playlists and a stream, by path."""
    pages = {}

    def do_GET(self):
        kind, body = self.pages.get(self.path, ("text/plain", None))
        self.send_response(200 if body is not None else 404)
        self.send_header("Content-Type", kind)
        self.end_headers()
        self.wfile.write(body or b"not found")

    def log_message(self, *args):
        pass


class Local(unittest.TestCase):
    """Nothing here goes through a proxy, whatever the machine's settings."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"})
        patcher.start()
        self.addCleanup(patcher.stop)


class Shoutcast(Local):
    def test_an_icy_answer_is_read_as_http(self):
        server, url = icy_station()
        self.addCleanup(server.close)
        response = radio.open_stream(url)
        self.assertEqual(response.headers.get("icy-metaint"), "8192")
        self.assertEqual(response.headers.get("icy-name"), "Invented FM")
        self.assertEqual(response.read(len(AUDIO)), AUDIO)
        response.close()

    def test_an_ordinary_http_answer_still_works(self):
        server, url = icy_station(b"HTTP/1.0 200 OK")
        self.addCleanup(server.close)
        response = radio.open_stream(url)
        self.assertEqual(response.read(len(AUDIO)), AUDIO)
        response.close()


class Titles(unittest.TestCase):
    def test_utf_8(self):
        self.assertEqual(radio.stream_title("StreamTitle='Canción del Mar';".encode() + b"\x00" * 5), "Canción del Mar")

    def test_windows_latin_1(self):
        self.assertEqual(radio.stream_title(b"StreamTitle='Caf\xe9 \x93Song\x94';\x00"), "Café “Song”")

    def test_a_byte_cp1252_does_not_map_still_gives_a_title(self):
        self.assertEqual(radio.stream_title(b"StreamTitle='A\x81B';"), "A�B")


class Playlists(Local):
    def setUp(self):
        super().setUp()
        Files.pages = {
            "/listen/station.pls": ("audio/x-scpls", b"[playlist]\nNumberOfEntries=1\nFile1=live/high\nTitle1=Invented FM\nLength1=-1\n"),
            "/listen/station.m3u": ("audio/x-mpegurl", b"#EXTM3U\n#EXTINF:-1,Invented FM\n../streams/hi.aac\n"),
            "/listen/live/high": ("audio/mpeg", AUDIO),
            "/streams/hi.aac": ("audio/aac", AUDIO),
        }
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Files)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def test_a_pls_with_a_relative_address(self):
        response = radio.open_stream(self.base + "/listen/station.pls")
        self.assertEqual(response.url, self.base + "/listen/live/high")
        self.assertEqual(response.read(16), AUDIO[:16])
        response.close()

    def test_an_m3u_with_a_relative_address(self):
        response = radio.open_stream(self.base + "/listen/station.m3u")
        self.assertEqual(response.url, self.base + "/streams/hi.aac")
        response.close()

    def test_what_the_first_address_is(self):
        first = radio._first_address
        self.assertEqual(first("[playlist]\nNumberOfEntries=1\nFile1=http://a.example/s?x=1\n"), "http://a.example/s?x=1")
        self.assertEqual(first("#EXTM3U\n\n#EXTINF:-1,Name\nhttps://a.example/s\n"), "https://a.example/s")
        self.assertEqual(first("[playlist]\nTitle1=Invented FM\nFile1=stream.mp3\n"), "stream.mp3")
        self.assertEqual(first("rtsp://a.example/s\n/live\n"), "/live")   # another scheme is passed over
        self.assertIsNone(first("Not found"))                              # words, not an address
        self.assertIsNone(first("[playlist]\nNumberOfEntries=0\nVersion=2\n"))


if __name__ == "__main__":
    unittest.main()
