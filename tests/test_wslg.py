"""Under WSLg (player.py): the wait before the window opens (#125), and the
sound output WebKit plays to (#129). Under WSL, a window mapped before the
Windows side has connected to WSLg's compositor never shows, so the player
waits for the line the compositor logs then. The log is a scratch file and the
clock a stand-in: nothing waits for real."""
import contextlib
import io
import os
import tempfile
import unittest

from tests import SCRATCH

import player

LINE = b"[02:07:35.023] Client HandShake buildNumber:26300\n"


class Clock:
    """Stands in for time.monotonic and time.sleep: a sleep moves the clock on,
    and can write to the log on a given sleep, the way the compositor would."""

    def __init__(self, log=None, write_on=None):
        self.now = 100.0
        self.sleeps = 0
        self.log, self.write_on = log, write_on

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps += 1
        self.now += seconds
        if self.sleeps == self.write_on:
            with open(self.log, "ab") as f:
                f.write(LINE)


class WaitForWslg(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wslg-", dir=SCRATCH)
        self.log = os.path.join(self.dir, "weston.log")

    def wait(self, clock, limit=20):
        return player.wait_for_wslg(log=self.log, limit=limit, sleep=clock.sleep, clock=clock.clock)

    def test_not_wslg(self):
        clock = Clock()
        missing = os.path.join(self.dir, "no-wslg", "weston.log")
        self.assertTrue(player.wait_for_wslg(log=missing, sleep=clock.sleep, clock=clock.clock))
        self.assertEqual(clock.sleeps, 0)

    def test_already_connected(self):
        with open(self.log, "wb") as f:
            f.write(b"[02:07:32.315] Loading module '/usr/lib/libweston-9/rdp-backend.so'\n" + LINE)
        clock = Clock()
        self.assertTrue(self.wait(clock))
        self.assertEqual(clock.sleeps, 0)

    def test_connects_later(self):
        with open(self.log, "wb") as f:
            f.write(b"[02:07:32.315] Loading module '/usr/lib/libweston-9/rdp-backend.so'\n")
        clock = Clock(self.log, write_on=11)   # about 2.7 s in, as on a launch that started WSL
        self.assertTrue(self.wait(clock))
        self.assertEqual(clock.sleeps, 11)

    def test_log_not_written_yet(self):
        clock = Clock(self.log, write_on=3)
        self.assertTrue(self.wait(clock))
        self.assertEqual(clock.sleeps, 3)

    def test_never_connects(self):
        with open(self.log, "wb") as f:
            f.write(b"[02:07:32.315] Loading module '/usr/lib/libweston-9/rdp-backend.so'\n")
        clock = Clock()
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            self.assertFalse(self.wait(clock, limit=5))
        self.assertIn("had not connected after 5 s", said.getvalue())
        self.assertGreaterEqual(clock.now - 100.0, 5)
        self.assertLess(clock.now - 100.0, 5.5)   # it goes on soon after the limit


class WslgAudio(unittest.TestCase):
    """The sound output WebKit plays to under WSLg (#129): GStreamer's OpenAL sink, ranked first through the
    environment the player's processes start with. WSLg's sound server is a scratch file; nothing plays."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wslg-audio-", dir=SCRATCH)
        self.server = os.path.join(self.dir, "PulseServer")
        open(self.server, "w").close()

    def test_not_wslg(self):
        env = {}
        self.assertFalse(player.wslg_audio(env, server=os.path.join(self.dir, "none")))
        self.assertEqual(env, {})

    def test_wslg(self):
        env = {}
        self.assertTrue(player.wslg_audio(env, server=self.server))
        self.assertEqual(env, {"GST_PLUGIN_FEATURE_RANK": "openalsink:MAX"})

    def test_ranks_of_the_user_kept(self):
        env = {"GST_PLUGIN_FEATURE_RANK": "avdec_aac:PRIMARY"}
        self.assertTrue(player.wslg_audio(env, server=self.server))
        self.assertEqual(env["GST_PLUGIN_FEATURE_RANK"], "avdec_aac:PRIMARY,openalsink:MAX")

    def test_user_ranked_the_sink(self):
        for ranks in ("openalsink:NONE", "avdec_aac:PRIMARY, openalsink :NONE"):
            env = {"GST_PLUGIN_FEATURE_RANK": ranks}
            self.assertFalse(player.wslg_audio(env, server=self.server))
            self.assertEqual(env["GST_PLUGIN_FEATURE_RANK"], ranks)   # their choice, left alone


if __name__ == "__main__":
    unittest.main()
