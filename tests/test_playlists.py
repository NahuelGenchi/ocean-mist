"""Saved playlists (#82): every song comes back, whatever its name, a failed
save leaves nothing behind, and a name with .m3u added after the dialog never
replaces a file. Generated files with invented names, in temporary folders."""
import os
import tempfile
import unittest

from tests import SCRATCH

from library import Library, read_playlist, save_name, write_playlist


class RoundTrip(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(dir=SCRATCH)
        names = ["#1 Crush.wav", " Leading Space.wav", "Canción.wav", "Plain.wav",
                 os.fsdecode(b"Caf\xe9 Latin-1 Name.wav")]   # a file name that is not UTF-8 itself
        self.songs = [os.path.join(self.folder, "Album", name) for name in names]
        os.makedirs(os.path.join(self.folder, "Album"))
        for song in self.songs:
            with open(song, "wb") as fh:
                fh.write(b"not really a song")
        self.lib = Library()
        self.lib.add_paths(self.songs, announce=False)

    def test_every_song_comes_back(self):
        path = os.path.join(self.folder, "Mix.m3u")
        self.lib.save_playlist(path)
        self.assertEqual(read_playlist(path), self.songs)

    def test_a_playlist_in_the_songs_own_folder(self):
        path = os.path.join(self.folder, "Album", "Here.m3u8")
        self.lib.save_playlist(path)
        self.assertEqual(read_playlist(path), self.songs)
        with open(path, "rb") as fh:
            text = fh.read()
        self.assertIn(b"\n./#1 Crush.wav\n", text)          # not a comment
        self.assertIn(b"\n./ Leading Space.wav\n", text)    # its space kept
        self.assertIn(b"\nCaf\xe9 Latin-1 Name.wav\n", text)  # the name's own bytes

    def test_a_latin_1_playlist_of_another_player_still_reads(self):
        path = os.path.join(self.folder, "Album", "Old.m3u")
        with open(path, "wb") as fh:
            fh.write(b"#EXTM3U\r\n#EXTINF:120,Ana - Canci\xf3n\r\nCanci\xf3n.wav\r\nPlain.wav\r\n")
        labels = {}
        self.assertEqual(read_playlist(path, labels), [self.songs[2], self.songs[3]])
        self.assertEqual(labels, {})   # labels are kept for streamed songs only


class FailedSave(unittest.TestCase):
    def test_nothing_is_left_behind(self):
        folder = tempfile.mkdtemp(dir=SCRATCH)
        path = os.path.join(folder, "Mix.m3u")
        track = {"path": os.path.join(folder, "a.wav"), "title": "Broken \ud800", "artist": "", "duration": 0}
        with self.assertRaises(ValueError):
            write_playlist(path, [track])
        self.assertEqual(os.listdir(folder), [])


class SaveName(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(dir=SCRATCH)
        self.mix = os.path.join(self.folder, "Mix")

    def touch(self, name):
        open(os.path.join(self.folder, name), "w").close()

    def test_with_its_extension_the_name_is_kept(self):
        self.touch("Mix.m3u")   # the dialog asked before replacing it
        self.assertEqual(save_name(self.mix + ".m3u"), self.mix + ".m3u")
        self.assertEqual(save_name(self.mix + ".M3U8"), self.mix + ".M3U8")

    def test_without_it_m3u_is_added(self):
        self.assertEqual(save_name(self.mix), self.mix + ".m3u")

    def test_an_added_m3u_never_replaces_a_file(self):
        self.touch("Mix.m3u")
        self.assertEqual(save_name(self.mix), self.mix + " (2).m3u")
        self.touch("Mix (2).m3u")
        self.assertEqual(save_name(self.mix), self.mix + " (3).m3u")


if __name__ == "__main__":
    unittest.main()
