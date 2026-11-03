"""The playback engine (engine.py) against a stand-in runtime: a small script put where castlabs's Electron goes,
which speaks the helper's line protocol, so nothing is downloaded and no Chromium runs. Removing the engine
while a song plays tells the page that the song stopped (#86)."""
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest

from tests import SCRATCH

import engine
import firefox

# the stand-in: it starts, says it is ready, plays what it is asked to (it says so), and ends when asked
RUNTIME = r'''#!%s
import json, sys
def say(**event):
    print("OMP " + json.dumps(event), flush=True)
sys.stdin.readline()   # the sign-in, never kept
say(event="start")
say(event="ready", store="")
for line in sys.stdin:
    cmd = json.loads(line)
    if cmd.get("cmd") == "play":
        say(event="state", serial=cmd["serial"], state="playing", position=cmd.get("from", 0), duration=200)
    elif cmd.get("cmd") == "quit":
        break
'''


class Events:
    """What the engine tells the page, and a wait for one of them."""

    def __init__(self):
        self.seen, self.changed = [], threading.Condition()

    def __call__(self, kind, **fields):
        with self.changed:
            self.seen.append(dict(fields, kind=kind))
            self.changed.notify_all()

    def wait(self, test, within=10):
        until = time.monotonic() + within
        with self.changed:
            while not any(test(event) for event in self.seen):
                left = until - time.monotonic()
                if left <= 0:
                    return None
                self.changed.wait(left)
            return next(event for event in self.seen if test(event))


class StandInRuntime(unittest.TestCase):
    def setUp(self):
        self.root = os.path.join(tempfile.mkdtemp(dir=SCRATCH), "engine")
        program = os.path.join(self.root, "electron", "electron")
        os.makedirs(os.path.dirname(program))
        with open(program, "w", encoding="utf-8") as fh:
            fh.write(RUNTIME % sys.executable)
        os.chmod(program, os.stat(program).st_mode | stat.S_IXUSR)
        with open(os.path.join(self.root, "engine.json"), "w", encoding="utf-8") as fh:
            json.dump({"version": engine.VERSION}, fh)
        self.events = Events()
        token = firefox.AppleToken("invented-sign-in", "music.apple.com", 0, True, True, 1)
        self.engine = engine.Engine(lambda service: token, self.events, root=self.root)
        self.addCleanup(self.engine.close)
        self.addCleanup(shutil.rmtree, os.path.dirname(self.root), True)

    def playing(self, serial):
        self.engine.play(serial, ["1000000001"], service="apple")
        return self.events.wait(lambda e: e["kind"] == "engine" and e.get("serial") == serial
                                and e.get("phase") == "playing")

    def test_the_stand_in_plays(self):
        self.assertTrue(self.engine.installed())
        self.assertIsNotNone(self.playing(1))

    def test_removing_the_engine_mid_song_tells_the_page_the_song_stopped(self):
        self.assertIsNotNone(self.playing(7))
        proc = self.engine.proc
        self.engine.remove()
        told = self.events.wait(lambda e: e["kind"] == "engine" and e.get("serial") == 7 and e.get("phase") == "error")
        self.assertIsNotNone(told, "the page heard nothing: its song would go on 'playing' in silence")
        self.assertEqual(told["code"], "not-installed")
        self.assertEqual(told["message"], engine.WORDS["not-installed"])
        self.assertIsNotNone(proc.poll(), "the helper has ended")
        self.assertFalse(os.path.exists(self.root))
        self.assertFalse(self.engine.installed())
        time.sleep(0.5)   # the helper's end says nothing more: one word for the song, not a crash after it
        self.assertEqual([e.get("code") for e in self.events.seen if e.get("phase") == "error"], ["not-installed"])

    def test_removing_it_with_nothing_playing_tells_the_page_nothing(self):
        self.assertIsNotNone(self.playing(3))
        self.engine.stop()
        self.engine.remove()
        time.sleep(0.5)
        self.assertEqual([e for e in self.events.seen if e.get("phase") == "error"], [])


if __name__ == "__main__":
    unittest.main()
