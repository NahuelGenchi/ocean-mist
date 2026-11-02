"""Large libraries, host side (#88): the Ogg and MP3 readers read less and as
right as before, the page is told only what changed while tags are read, and
the tag cache keeps what a drive that is not plugged in held. Generated files
with invented names, in temporary folders."""
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


# ---- Ogg ----
def ogg_pages(packets, per_page=255):
    """packets as Ogg pages (each packet's segments split over pages of at most per_page segments)."""
    data, seq = b"", 0
    for packet in packets:
        segments = [255] * (len(packet) // 255) + [len(packet) % 255]
        while segments:
            page, segments = segments[:per_page], segments[per_page:]
            size = sum(page)
            body, packet = packet[:size], packet[size:]
            data += (b"OggS" + bytes(2) + struct.pack("<qII", 0, 1, seq) + bytes(4)
                     + bytes([len(page)]) + bytes(page) + body)
            seq += 1
    return data


def reference_packets(data, wanted):
    """The packets as the Ogg format defines them, the plain way."""
    packets, current, pos = [], b"", 0
    while len(packets) < wanted and data[pos:pos + 4] == b"OggS":
        count = data[pos + 26]
        lacing = data[pos + 27:pos + 27 + count]
        pos += 27 + count
        for value in lacing:
            current += data[pos:pos + value]
            pos += value
            if value < 255:
                packets.append(current)
                current = b""
                if len(packets) == wanted:
                    break
    return packets


class OggPackets(unittest.TestCase):
    def test_the_same_packets_as_the_format_says(self):
        import io
        sizes = [0, 1, 254, 255, 256, 510, 65025, 65026, 70000, 200000]
        packets = [bytes([n % 251]) * size for n, size in enumerate(sizes)]
        for per_page in (255, 7, 1):   # pages full, short, of one segment each
            data = ogg_pages(packets, per_page)
            for wanted in (1, 3, len(packets)):
                self.assertEqual(tags._ogg_packets(io.BytesIO(data), wanted), reference_packets(data, wanted),
                                 (per_page, wanted))

    def test_a_big_cover_is_read_at_once(self):
        import base64
        import io
        ident = b"\x01vorbis" + struct.pack("<IBIiii", 0, 2, 44100, 0, 128000, 0) + b"\x01"
        picture = struct.pack(">II", 3, 10) + b"image/jpeg" + bytes(20) + struct.pack(">I", 2_000_000) + b"\xff\xd8" + bytes(1_999_998)
        comments = [b"TITLE=Invented Song", b"METADATA_BLOCK_PICTURE=" + base64.b64encode(picture)]
        packet = (b"\x03vorbis" + struct.pack("<I", 8) + b"invented" + struct.pack("<I", len(comments))
                  + b"".join(struct.pack("<I", len(c)) + c for c in comments) + b"\x01")
        data = ogg_pages([ident, packet])

        class Counting(io.BytesIO):
            reads = 0

            def read(self, *args):
                Counting.reads += 1
                return super().read(*args)
        packets = tags._ogg_packets(Counting(data), 2)
        self.assertEqual(packets[1], packet)
        self.assertLess(Counting.reads, 200)   # page by page, not 255 bytes at a time (some 11,000 reads)


# ---- MP3 ----
HEADER = bytes([0xFF, 0xFB, 0x90, 0x64])   # MPEG-1 layer III, 128 kbit/s, 44.1 kHz: 417-byte frames


def mp3(path, frames, first=b"", before=b""):
    """frames 417-byte frames, the first carrying `first` after its side info, behind `before`."""
    with open(path, "wb") as fh:
        fh.write(before + HEADER + bytes(32) + first + bytes(417 - 36 - len(first)) + (HEADER + bytes(413)) * (frames - 1))


class Mp3Lengths(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(dir=SCRATCH)
        self.read = 0
        real = open

        def counting(*args, **kwargs):
            fh = real(*args, **kwargs)
            test = self

            class Counted:
                def read(self, *a):
                    data = fh.read(*a)
                    test.read += len(data)
                    return data

                def __getattr__(self, name):
                    return getattr(fh, name)

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    fh.close()
            return Counted()
        patcher = mock.patch.object(tags, "open", counting, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def length(self, name, **kwargs):
        path = os.path.join(self.folder, name)
        mp3(path, **kwargs)
        self.read = 0
        return tags.read(path)["duration"]

    def test_a_frame_count_is_read_from_the_first_8_kib(self):
        seconds = self.length("xing.mp3", frames=700, first=b"Xing" + struct.pack(">II", 1, 4000))
        self.assertAlmostEqual(seconds, 4000 * 1152 / 44100, places=3)
        self.assertLess(self.read, 9000)
        seconds = self.length("info.mp3", frames=700, first=b"Info" + struct.pack(">II", 1, 700))
        self.assertAlmostEqual(seconds, 700 * 1152 / 44100, places=3)

    def test_without_one_the_frames_are_measured_as_before(self):
        seconds = self.length("cbr.mp3", frames=700)
        self.assertAlmostEqual(seconds, 700 * 1152 / 44100, delta=0.05)
        self.assertGreater(self.read, 100_000)   # the 128 KiB it takes to measure

    def test_a_count_behind_more_than_8_kib_of_other_bytes(self):
        seconds = self.length("late.mp3", frames=700, first=b"Xing" + struct.pack(">II", 1, 4000), before=bytes(10_000))
        self.assertAlmostEqual(seconds, 4000 * 1152 / 44100, places=3)


# ---- what the page is told while tags are read ----
def atom(kind, body):
    return struct.pack(">I", 8 + len(body)) + kind + body


def m4a(path, title):
    item = atom(b"\xa9nam", atom(b"data", struct.pack(">II", 1, 0) + title.encode()))
    meta = atom(b"meta", bytes(4) + atom(b"ilst", item))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(atom(b"ftyp", b"M4A " + bytes(4)) + atom(b"moov", atom(b"mvhd", bytes(12) + struct.pack(">II", 1000, 5000) + bytes(80))
                                                         + atom(b"udta", meta)))


def wait(test, what, check, seconds=15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.05)
    test.fail(f"timed out waiting for {what}")


class WhatThePageIsTold(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(dir=SCRATCH)
        self.songs = [os.path.join(self.folder, f"Artist {n // 10}", f"{n:02d}.m4a") for n in range(30)]
        for n, path in enumerate(self.songs):
            m4a(path, f"Invented {n}")
        self.lib = Library()
        self.lib.settings["folders"] = [self.folder]

    def run_scan(self):
        box = self.lib.subscribe()
        self.lib.scan()
        wait(self, "the tags", lambda: not self.lib.scanning and len(self.lib._tagged) >= len(self.songs))
        time.sleep(1)   # the reader tells the page when it is idle
        events = []
        while not box.empty():
            events.append(box.get())
        return events

    def test_the_rows_read_come_with_the_event_and_the_lists_are_left_alone(self):
        events = self.run_scan()
        changed = [row for e in events if e["event"] == "library" for row in e.get("changed", [])]
        self.assertEqual({row["title"] for row in changed}, {f"Invented {n}" for n in range(30)})
        self.assertTrue(all("path" not in row and row["file"].endswith(".m4a") for row in changed))
        # the scan's own two (the library, and the saved playlists riding along with the lists), nothing per tag
        self.assertEqual([e["event"] for e in events if "changed" not in e], ["library", "playlist"])

    def test_a_song_of_my_music_read_tells_the_lists(self):
        box = self.lib.subscribe()
        self.lib.add_paths([self.songs[0]], announce=False)
        wait(self, "its tags", lambda: self.lib.by_path[self.songs[0]]["title"] == "Invented 0")
        time.sleep(1)
        events = []
        while not box.empty():
            events.append(box.get()["event"])
        self.assertIn("playlist", events)

    def test_the_library_is_answered_whole_and_the_same(self):
        self.run_scan()
        answer = self.lib.library_snapshot()
        self.assertEqual(len(answer["tracks"]), 30)
        self.assertEqual(sorted(t["title"] for t in answer["tracks"]), sorted(f"Invented {n}" for n in range(30)))
        json.dumps(answer)


# ---- the tag cache ----
class TagCache(unittest.TestCase):
    def setUp(self):
        base = tempfile.mkdtemp(dir=SCRATCH)
        self.here, self.away, self.empty = (os.path.join(base, name) for name in ("here", "away", "empty"))
        os.makedirs(os.path.join(self.here, "Album"))
        os.makedirs(self.empty)   # a mount point with nothing mounted on it
        self.cache = os.path.join(base, "tags.json")
        patcher = mock.patch.object(library, "TAG_CACHE", self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_what_a_drive_not_plugged_in_held_is_kept(self):
        lib = Library()
        lib.settings["folders"] = [self.here, self.away, self.empty]
        seen = os.path.join(self.here, "Album", "seen.m4a")
        m4a(seen, "Seen")
        lib.add_paths([seen], announce=False)
        entry = [1, 2, {"title": "Invented"}]
        with lib.lock:
            lib._cache.update({
                seen: entry,
                os.path.join(self.here, "Album", "deleted.m4a"): entry,   # its folder is there: it was deleted
                os.path.join(self.away, "Album", "song.m4a"): entry,       # its drive is not plugged in
                os.path.join(self.empty, "Album", "song.m4a"): entry,      # nor this one's
                os.path.join(self.away + "-other", "song.m4a"): entry,     # a folder that only starts the same
            })
            lib._cache_dirty = True
        lib._save_cache()
        with open(self.cache, encoding="utf-8") as fh:
            kept = set(json.load(fh)["files"])
        self.assertEqual(kept, {seen, os.path.join(self.away, "Album", "song.m4a"), os.path.join(self.empty, "Album", "song.m4a")})


class PlaylistOutsideTheLock(unittest.TestCase):
    def test_reading_a_playlist_does_not_hold_the_lock(self):
        folder = tempfile.mkdtemp(dir=SCRATCH)
        song = os.path.join(folder, "song.m4a")
        m4a(song, "Song")
        path = os.path.join(folder, "Mix.m3u")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#EXTM3U\nsong.m4a\n")
        lib = Library()
        held = []
        real = library.read_playlist

        def reading(*args, **kwargs):
            held.append(lib.lock._is_owned())
            return real(*args, **kwargs)
        with mock.patch.object(library, "read_playlist", reading):
            tracks = lib.playlist_tracks(path)
        self.assertEqual([t["path"] for t in tracks], [song])
        self.assertEqual(held, [False])


if __name__ == "__main__":
    unittest.main()
