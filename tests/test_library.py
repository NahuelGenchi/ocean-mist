"""The Now Playing lists: My Music, and one list for each streaming service,
which can hold each other's songs, a song in one list at a time."""
import json
import os
import tempfile
import unittest

from tests import SCRATCH

import apple
import library
import spotify
import ytmusic
from library import STATE_FILE, Library, list_for


def yt(n, title=""):
    return {"video": f"video{n:06d}", "title": title or f"YouTube {n}", "artist": "A"}


def am(n):
    return {"apple": f"{n:09d}", "title": f"Apple {n}", "artist": "A"}


def sp(n):
    return {"spotify": f"{n:022d}", "title": f"Spotify {n}", "artist": "A"}


class Lists(unittest.TestCase):
    def setUp(self):
        self.lib = Library()

    def ids(self, which):
        return [t["id"] for t in self.lib.lists[which]]

    def where(self, track):
        return [which for which in library.LISTS if track["id"] in self.ids(which)]

    def test_each_song_goes_to_its_service(self):
        a, b, c = (self.lib.add_streams([item])[0] for item in (yt(1), am(1), sp(1)))
        self.assertEqual((list_for(a), list_for(b), list_for(c)), ("youtube", "apple", "spotify"))
        self.assertEqual((self.where(a), self.where(b), self.where(c)), (["youtube"], ["apple"], ["spotify"]))
        self.assertEqual(self.ids("local"), [])

    def test_bad_ids_and_untitled_service_songs_are_refused(self):
        self.assertEqual(self.lib.add_streams([{"video": "short"}, {"apple": "../x", "title": "T"},
                                               {"spotify": "tooshort", "title": "T"},
                                               {"apple": "123456789", "title": "  "}, "not a dict"]), [])
        self.assertTrue(all(not self.lib.lists[which] for which in library.LISTS))

    def test_into_the_playing_list_whatever_the_service(self):
        playing = self.lib.add_streams([am(1), am(2)])
        song = self.lib.add_streams([yt(1)], into="apple")[0]
        self.assertEqual(self.ids("apple"), [playing[0]["id"], playing[1]["id"], song["id"]])
        self.assertEqual(self.ids("youtube"), [])

    def test_into_moves_a_song_out_of_its_other_list(self):
        song = self.lib.add_streams([yt(1)])[0]
        self.lib.add_streams([am(1)])
        self.lib.add_streams([yt(1)], into="apple")
        self.assertEqual(self.where(song), ["apple"])

    def test_after_puts_songs_right_after_that_one(self):
        first, second, third = self.lib.add_streams([am(1), am(2), am(3)])
        added = self.lib.add_streams([yt(1), yt(2)], into="apple", after=first["id"])
        self.assertEqual(self.ids("apple"), [first["id"], added[0]["id"], added[1]["id"], second["id"], third["id"]])

    def test_after_moves_a_song_already_in_the_list(self):
        first, second, third = self.lib.add_streams([am(1), am(2), am(3)])
        self.lib.add_streams([am(3)], into="apple", after=first["id"])
        self.assertEqual(self.ids("apple"), [first["id"], third["id"], second["id"]])

    def test_after_a_song_that_is_not_there_appends(self):
        first, = self.lib.add_streams([am(1)])
        song, = self.lib.add_streams([yt(1)], into="apple", after=999999)
        self.assertEqual(self.ids("apple"), [first["id"], song["id"]])

    def test_into_must_be_a_streaming_list(self):
        song, = self.lib.add_streams([yt(1)], into="local")
        self.assertEqual(self.where(song), ["youtube"])

    def test_replace_ignores_into_and_names_the_list(self):
        self.lib.add_streams([am(1)])
        songs = self.lib.add_streams([yt(1), yt(2)], replace=True, name="Mix", into="apple")
        self.assertEqual(self.ids("youtube"), [t["id"] for t in songs])
        self.assertEqual(self.lib.names["youtube"], "Mix")
        self.assertEqual(len(self.ids("apple")), 1)

    def test_named_adds_nothing_once_another_list_was_chosen(self):
        self.lib.add_streams([yt(1)], replace=True, name="First")
        self.assertEqual(self.lib.add_streams([yt(2)], named="First")[0]["video"], "video000002")
        self.lib.add_streams([yt(3)], replace=True, name="Second")
        self.assertEqual(self.lib.add_streams([yt(4)], named="First"), [])
        self.assertEqual(len(self.ids("youtube")), 1)

    def test_list_of_says_where_a_song_is(self):
        song, = self.lib.add_streams([yt(1)])
        self.assertEqual(self.lib.list_of(song["id"]), "youtube")
        self.lib.add_streams([am(1)])
        self.lib.add_streams([yt(1)], into="apple")
        self.assertEqual(self.lib.list_of(song["id"]), "apple")
        self.assertIsNone(self.lib.list_of(999999))

    def test_cover_only_from_the_service_picture_hosts(self):
        song, = self.lib.add_streams([dict(yt(1), cover="https://example.com/a.jpg")])
        self.assertEqual(song["cover"], "")
        song, = self.lib.add_streams([dict(yt(2), cover="https://lh3.googleusercontent.com/a")])
        self.assertTrue(song["cover"])
        song, = self.lib.add_streams([dict(am(1), cover="https://lh3.googleusercontent.com/a")])
        self.assertEqual(song["cover"], "")


class Saved(unittest.TestCase):
    """state.json holds My Music, never a streamed song."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(dir=SCRATCH)
        self.song = os.path.join(self.folder, "song.ogg")
        with open(self.song, "wb") as fh:
            fh.write(b"not really a song")
        self.lib = Library()

    def test_streamed_songs_are_never_written(self):
        self.lib.add_paths([self.song], announce=False)
        self.lib.add_streams([yt(1), am(1), sp(1)])
        self.lib.save()
        with open(STATE_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["playlist"], [self.song])
        text = json.dumps(saved)
        for word in ("video000001", "000000001", ytmusic.WATCH, apple.SONG_URL, spotify.SONG_URL):
            self.assertNotIn(word, text)

    def test_open_playlist_puts_streamed_songs_in_the_first_ones_list(self):
        path = os.path.join(self.folder, "mixed.m3u")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#EXTM3U\n"
                     f"{self.song}\n"
                     "#EXTINF:200,A - Apple One\n"
                     f"{apple.SONG_URL}123456789\n"
                     "#EXTINF:180,A - YouTube One\n"
                     f"{ytmusic.WATCH}video000001\n"
                     "#EXTINF:190,A - Spotify One\n"
                     f"{spotify.SONG_URL}{'1' * 22}\n")
        self.lib.add_streams([yt(1)])   # already in its own list: it moves
        first = self.lib.open_playlist(path)
        self.assertEqual([t["path"] for t in self.lib.lists["local"]], [self.song])
        self.assertEqual(first, self.lib.lists["local"][0]["id"])
        apple_list = self.lib.lists["apple"]
        self.assertEqual([list_for(t) for t in apple_list], ["apple", "youtube", "spotify"])
        self.assertEqual(apple_list[0]["title"], "Apple One")
        self.assertEqual(self.lib.lists["youtube"], [])
        self.assertEqual(self.lib.names["apple"], "mixed")


if __name__ == "__main__":
    unittest.main()
