"""The server's routes for Apple Music and Spotify (player.py): every request the
page builds for them reaches the reader that answers it (#78). The readers are
stand-ins: nothing is asked of the services themselves."""
import http.client
import inspect
import json
import os
import re
import secrets
import tempfile
import unittest

from tests import ROOT, SCRATCH

import apple
import player
import spotify
from library import Library


class Reader:
    """Stands in for AppleMusic or SpotifyMusic, and notes what it was asked."""

    def __init__(self):
        self.asked = []

    def library(self, kind, offset=0):
        self.asked.append(("library", kind, offset))
        return {"items": [], "more": None}

    def page_of(self, kind, ident):
        self.asked.append(("page", kind, ident))
        return {"title": "", "tracks": []}


class AppleReader(Reader):
    def search(self, kind, words, offset=0):
        self.asked.append(("search", kind, words, offset))
        return {"items": [], "more": None, "total": None}


class SpotifyReader(Reader):
    def search(self, kind, words):   # Spotify's search takes no offset: it matches the list it read
        self.asked.append(("search", kind, words))
        return {"items": [], "more": None, "total": 0, "read": 0, "whole": True}


def page_routes():
    """(service, route) for each request ui/premium.js builds with getApple or getSpotify."""
    with open(os.path.join(ROOT, "ui", "premium.js"), encoding="utf-8") as fh:
        source = fh.read()
    # a lookahead, so that `(apple ? getApple : getSpotify)(`search?...` counts for both services
    found = re.finditer(r"(?=get(Apple|Spotify)\b[^`\n]*`(\w+)\?)", source)
    return {(service.lower(), route) for service, route in (m.groups() for m in found)}


class StandIns(unittest.TestCase):
    """The stand-ins take what the real readers take, so a route that would fail there fails here."""

    def test_same_arguments_as_the_real_readers(self):
        for fake, real in ((AppleReader, apple.AppleMusic), (SpotifyReader, spotify.SpotifyMusic)):
            for name in ("library", "search", "page_of"):
                self.assertEqual(list(inspect.signature(getattr(fake, name)).parameters),
                                 list(inspect.signature(getattr(real, name)).parameters), f"{real.__name__}.{name}")


class Routes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = Library()
        profile = tempfile.mkdtemp(dir=SCRATCH, suffix=".invented")
        cls.lib.settings["apple_account"] = cls.lib.settings["spotify_account"] = profile   # signed in, as far as the server knows
        cls.readers = {"apple": AppleReader(), "spotify": SpotifyReader()}
        cls.token = secrets.token_urlsafe(16)
        cls.server = player.start_server(cls.lib, cls.token, music=object(),
                                         apple_music=cls.readers["apple"], spotify_music=cls.readers["spotify"])
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        for reader in self.readers.values():
            reader.asked.clear()

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", f"/{self.token}/{path}", headers={"Host": f"127.0.0.1:{self.port}"})
        answer = conn.getresponse()
        data = answer.read()
        conn.close()
        return answer, data

    def test_the_page_builds_the_routes_these_tests_know(self):
        routes = page_routes()
        for service in ("apple", "spotify"):
            for route in ("library", "search", "page"):
                self.assertIn((service, route), routes)

    def test_every_route_the_page_builds_is_answered(self):
        for service, route in sorted(page_routes()):
            with self.subTest(service=service, route=route):
                self.readers[service].asked.clear()
                answer, data = self.get(f"{service}/{route}?kind=songs&q=words&id=p.Invented1&offset=25")
                self.assertEqual(answer.status, 200, data)
                self.assertNotIn("error", json.loads(data))
                self.assertEqual([call[0] for call in self.readers[service].asked], [route])

    def test_spotify_search_reaches_the_spotify_reader(self):
        answer, data = self.get("spotify/search?kind=albums&q=two%20words")
        self.assertEqual(answer.status, 200, data)
        self.assertEqual(json.loads(data)["whole"], True)
        self.assertEqual(self.readers["spotify"].asked, [("search", "albums", "two words")])
        self.assertEqual(self.readers["apple"].asked, [])

    def test_apple_search_pages_with_its_offset(self):
        answer, _ = self.get("apple/search?kind=songs&q=x&offset=50")
        self.assertEqual(answer.status, 200)
        self.assertEqual(self.readers["apple"].asked, [("search", "songs", "x", 50)])
        self.get("apple/search?kind=songs&q=x&offset=-1")
        self.assertEqual(self.readers["apple"].asked[-1], ("search", "songs", "x", 0))
        self.assertEqual(self.readers["spotify"].asked, [])

    def test_an_unknown_route_is_still_refused(self):
        for service in ("apple", "spotify"):
            self.assertEqual(self.get(f"{service}/nothing?kind=songs")[0].status, 404)


if __name__ == "__main__":
    unittest.main()
