"""The searches of the Apple Music and Spotify libraries, with the service's
page replaced by a stand-in: nothing is asked of the services themselves."""
import unittest

import tests  # noqa: F401  (scratch folders first)

import apple
import spotify
from apple import AppleError, AppleMusic
from spotify import SpotifyError, SpotifyMusic


def apple_song(n):
    return {"id": f"i.{n:06d}", "type": "library-songs",
            "attributes": {"name": f"Song {n}", "artistName": "Artist", "albumName": "Album",
                           "playParams": {"catalogId": f"{n:09d}"}, "durationInMillis": 200000}}


def apple_album(n):
    return {"id": f"l.{n:06d}", "type": "library-albums", "attributes": {"name": f"Album {n}", "trackCount": 3}}


class AppleSearch(unittest.TestCase):
    def setUp(self):
        self.music = AppleMusic(token_source=None, schedule=None)
        self.asked, self.answers = [], []

        def ask(path, query=None):
            self.asked.append((path, query))
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        self.music._ask = ask

    def test_asks_apple_for_the_kinds_of_one_search(self):
        self.answers.append({"results": {}})
        self.music.search("songs", "  two   words ")
        self.assertEqual(self.asked, [(apple.SEARCH, {"term": "two words", "types": "library-songs",
                                                      "limit": apple.FOUND, "offset": 0})])
        self.answers.append({"results": {}})
        self.music.search("all", "x", offset=50)
        self.assertEqual(self.asked[1][1]["types"],
                         "library-songs,library-albums,library-artists,library-playlists")
        self.assertEqual(self.asked[1][1]["offset"], 50)

    def test_words_are_cut_and_nothing_is_asked_without_any(self):
        self.answers.append({"results": {}})
        self.music.search("songs", "x" * 500)
        self.assertEqual(len(self.asked[0][1]["term"]), apple.WORDS)
        self.assertEqual(self.music.search("songs", "   "), {"items": [], "more": None, "total": None})
        self.assertEqual(len(self.asked), 1)

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(AppleError):
            self.music.search("catalog", "x")
        self.assertEqual(self.asked, [])

    def test_songs_come_first_and_a_page_with_more_says_where_it_goes_on(self):
        self.answers.append({"results": {
            "library-albums": {"data": [apple_album(1)]},
            "library-songs": {"data": [apple_song(1), apple_song(2), "junk"], "next": "/more"},
        }})
        found = self.music.search("all", "song", offset=25)
        self.assertEqual([item["kind"] for item in found["items"]], ["song", "song", "album"])
        self.assertEqual(found["items"][0]["apple"], "000000001")
        self.assertEqual(found["items"][0]["apple_lib"], "i.000001")
        self.assertEqual(found["more"], 25 + apple.FOUND)

    def test_not_found_is_an_empty_answer_and_other_errors_go_on(self):
        self.answers.append(AppleError("not found", status=404))
        self.assertEqual(self.music.search("songs", "x"), {"items": [], "more": None, "total": None})
        self.answers.append(AppleError("signed out", signed_out=True, status=401))
        with self.assertRaises(AppleError):
            self.music.search("songs", "x")


def spotify_row(title, artist="", album=""):
    return {"service": "spotify", "kind": "song", "title": title, "artist": artist, "album": album}


class SpotifySearch(unittest.TestCase):
    def setUp(self):
        self.music = SpotifyMusic(token_source=None, schedule=None)
        self.pages, self.read = {}, []

        def library(kind, offset=0):
            self.read.append((kind, offset))
            return self.pages[(kind, offset)]
        self.music.library = library

    def test_every_word_matches_whatever_its_case_and_accents(self):
        self.pages[("songs", 0)] = {"items": [spotify_row("Canción del Mar", "Ana"),
                                              spotify_row("Sea Song", "Bob", "Mar Azul"),
                                              spotify_row("Other")], "more": None}
        found = self.music.search("songs", "  CANCION  mar ")
        self.assertEqual([row["title"] for row in found["items"]], ["Canción del Mar"])
        self.assertEqual((found["total"], found["read"], found["whole"]), (1, 3, True))
        found = self.music.search("songs", "mar")
        self.assertEqual(len(found["items"]), 2)

    def test_the_list_is_read_once_and_kept(self):
        self.pages[("albums", 0)] = {"items": [spotify_row("One")], "more": 1}
        self.pages[("albums", 1)] = {"items": [spotify_row("Two")], "more": None}
        self.music.search("albums", "one")
        self.music.search("albums", "two")
        self.assertEqual(self.read, [("albums", 0), ("albums", 1)])
        self.music.generation += 1   # signed out: what was read before is not used
        self.music.search("albums", "two")
        self.assertEqual(len(self.read), 4)

    def test_a_list_longer_than_the_limit_is_not_whole(self):
        size = 400
        for offset in range(0, spotify.LIMIT + size, size):
            self.pages[("artists", offset)] = {"items": [spotify_row(f"Row {n}") for n in range(offset, offset + size)],
                                               "more": offset + size}
        found = self.music.search("artists", "row")
        self.assertEqual(found["read"], spotify.LIMIT)
        self.assertFalse(found["whole"])

    def test_no_words_reads_nothing_and_an_unknown_kind_is_refused(self):
        self.assertEqual(self.music.search("songs", " ")["items"], [])
        with self.assertRaises(SpotifyError):
            self.music.search("podcasts", "x")
        self.assertEqual(self.read, [])


if __name__ == "__main__":
    unittest.main()
