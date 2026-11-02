"""How the Media Library learns about files (#81): MP4's album artist, the tag
cache's version, a rescan that reads changed files again, and linked folders.
Generated files with invented names, in temporary folders."""
import json
import os
import struct
import tempfile
import time
import unittest
from unittest import mock

from tests import SCRATCH

import library
import tags
from library import Library


def atom(kind, body):
    return struct.pack(">I", 8 + len(body)) + kind + body


def m4a(**fields):
    """A tiny MP4 holding only what tags.py reads: a length, and the text items given (title=, artist=,
    albumartist=), in the order given."""
    names = {"title": b"\xa9nam", "artist": b"\xa9ART", "albumartist": b"aART", "album": b"\xa9alb"}
    items = b"".join(atom(names[key], atom(b"data", struct.pack(">II", 1, 0) + value.encode()))
                     for key, value in fields.items())
    mvhd = atom(b"mvhd", bytes(12) + struct.pack(">II", 1000, 5000) + bytes(80))
    meta = atom(b"meta", bytes(4) + atom(b"ilst", items))
    return atom(b"ftyp", b"M4A " + bytes(4)) + atom(b"moov", mvhd + atom(b"udta", meta))


def write(path, data, later=0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    if later:   # a time the file system tells apart from the one before, however coarse its clock
        info = os.stat(path)
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + later * 1_000_000_000))


def wait(test, what, check, seconds=10):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.05)
    test.fail(f"timed out waiting for {what}")


class AlbumArtist(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(dir=SCRATCH), "song.m4a")

    def test_album_artist_is_not_the_artist(self):
        write(self.path, m4a(title="Tide", albumartist="Various Artists", artist="Ana"))
        found = tags.read(self.path)
        self.assertEqual((found["artist"], found["albumartist"]), ("Ana", "Various Artists"))
        self.assertEqual(found["duration"], 5.0)

    def test_the_album_artist_stands_in_when_there_is_no_artist(self):
        write(self.path, m4a(title="Tide", albumartist="Various Artists"))
        self.assertEqual(tags.read(self.path)["artist"], "Various Artists")


class CacheVersion(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(dir=SCRATCH)
        self.song = os.path.join(self.folder, "song.m4a")
        write(self.song, m4a(title="Tide", artist="Ana"))
        info = os.stat(self.song)
        self.stamp = [info.st_mtime_ns, info.st_size]
        self.cache = os.path.join(self.folder, "tags.json")
        patcher = mock.patch.object(library, "TAG_CACHE", self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cached(self, version):
        with open(self.cache, "w", encoding="utf-8") as fh:
            json.dump({"version": version, "files": {self.song: self.stamp + [{"title": "From the cache"}]}}, fh)
        lib = Library()
        lib.load()
        return lib

    def test_an_older_readers_answers_are_read_again(self):
        lib = self.cached(1)
        self.assertEqual(lib._cache, {})
        self.assertEqual(lib._tags_for(self.song)["title"], "Tide")

    def test_this_readers_answers_are_kept(self):
        lib = self.cached(tags.VERSION)
        self.assertEqual(lib._tags_for(self.song)["title"], "From the cache")

    def test_the_cache_is_saved_with_this_readers_version(self):
        lib = Library()
        lib.by_path[self.song] = {}
        lib._cache, lib._cache_dirty = {self.song: self.stamp + [{"title": "Tide"}]}, True
        lib._save_cache()
        with open(self.cache, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["version"], tags.VERSION)


class Rescan(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(dir=SCRATCH)
        self.edited = os.path.join(self.folder, "01 Edited.m4a")
        self.other = os.path.join(self.folder, "02 Other.m4a")
        write(self.edited, m4a(title="First Title", artist="Ana", album="Mar Azul"))
        write(self.other, m4a(title="Other Title", artist="Bob"))
        self.lib = Library()
        self.lib.settings["folders"] = [self.folder]

    def scan(self):
        self.lib.scan()
        wait(self, "the scan", lambda: not self.lib.scanning)

    def title(self, path):
        track = self.lib.by_path.get(path)
        return track and track["title"]

    def test_a_file_changed_on_disk_is_read_again_and_the_others_are_not(self):
        self.scan()
        wait(self, "the first tags", lambda: self.title(self.edited) == "First Title" and self.title(self.other) == "Other Title")
        write(self.edited, m4a(title="Edited Title"), later=5)   # the artist and the album are gone from its tags
        with mock.patch.object(tags, "read", wraps=tags.read) as read:
            self.scan()
            wait(self, "the edited tags", lambda: self.title(self.edited) == "Edited Title")
            track = self.lib.by_path[self.edited]
            self.assertEqual((track["artist"], track["album"]), ("", ""))
            self.assertEqual([call.args[0] for call in read.call_args_list], [self.edited])
        self.assertEqual(self.title(self.other), "Other Title")

    def test_a_rescan_with_nothing_changed_reads_nothing(self):
        self.scan()
        wait(self, "the first tags", lambda: self.title(self.other) == "Other Title")
        with mock.patch.object(tags, "read", wraps=tags.read) as read:
            self.scan()
            time.sleep(0.5)
            self.assertEqual(read.call_count, 0)


class LinkedFolders(unittest.TestCase):
    def setUp(self):
        base = tempfile.mkdtemp(dir=SCRATCH)
        self.music = os.path.join(base, "music")
        self.real = os.path.join(self.music, "Album", "a.m4a")
        self.far = os.path.join(base, "elsewhere", "Linked", "b.m4a")
        write(self.real, m4a(title="A"))
        write(self.far, m4a(title="B"))
        os.symlink(os.path.join(base, "elsewhere", "Linked"), os.path.join(self.music, "Linked"))   # a linked album
        os.symlink(self.music, os.path.join(self.music, "Album", "up"))                              # a link back up
        os.symlink(os.path.join(self.music, "Album"), os.path.join(self.music, "Again"))             # the same album twice
        self.found = [self.real, os.path.join(self.music, "Linked", "b.m4a")]

    def test_opening_the_folder_follows_links_and_lists_each_file_once(self):
        self.assertEqual(Library._expand([self.music]), self.found)

    def test_the_library_scan_follows_links_and_lists_each_file_once(self):
        lib = Library()
        lib.settings["folders"] = [self.music]
        lib.scan()
        wait(self, "the scan", lambda: not lib.scanning)
        self.assertEqual([t["path"] for t in lib.collection], self.found)


if __name__ == "__main__":
    unittest.main()
