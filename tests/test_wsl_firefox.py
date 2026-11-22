"""Under WSL, the Windows Firefox's profiles and program (#126), against a
stand-in Windows: a scratch drive mount, a scratch wsl.conf and a stand-in
cmd.exe. Nothing of the real Windows side is asked or read."""
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import tests  # noqa: F401  (scratch folders first)
import firefox

ANSWER = ("APPDATA=C:\\Users\\Invented\\AppData\\Roaming\r\n"
          "LOCALAPPDATA=C:\\Users\\Invented\\AppData\\Local\r\n"
          "ProgramFiles=C:\\Program Files\r\n"
          "ProgramFiles(x86)=%ProgramFiles(x86)%\r\n")


class WindowsFirefox(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wsl-firefox-")
        self.addCleanup(__import__("shutil").rmtree, self.dir, ignore_errors=True)
        interop = os.path.join(self.dir, "WSLInterop")
        open(interop, "w").close()
        conf = os.path.join(self.dir, "wsl.conf")
        self.mnt = os.path.join(self.dir, "drives") + "/"
        with open(conf, "w") as fh:
            fh.write(f"[automount]\nroot = {self.mnt}\n")
        self.asked = []
        self.answer = ANSWER
        for patch in (mock.patch.object(firefox, "WSL_INTEROP", interop),
                      mock.patch.object(firefox, "WSL_CONF", conf),
                      mock.patch.object(firefox, "_run", self.run_cmd),
                      mock.patch.object(firefox, "_windows", {})):
            patch.start()
            self.addCleanup(patch.stop)
        self.user = os.path.join(self.mnt, "c", "Users", "Invented")
        self.root = os.path.join(self.user, "AppData", "Roaming", "Mozilla", "Firefox")

    def run_cmd(self, args, **kwargs):
        self.asked.append(args)
        if self.answer is None:
            raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))
        return subprocess.CompletedProcess(args, 0, self.answer.encode(), b"")

    def profile(self, folder):
        os.makedirs(folder)
        open(os.path.join(folder, "cookies.sqlite"), "wb").close()
        return os.path.realpath(folder)

    def test_paths(self):
        self.assertEqual(firefox.wsl_path("C:\\Users\\A b\\x"), os.path.join(self.mnt, "c", "Users", "A b", "x"))
        self.assertEqual(firefox.wsl_path("d:/Data/"), os.path.join(self.mnt, "d", "Data"))
        self.assertEqual(firefox.wsl_path("E:"), os.path.join(self.mnt, "e"))
        self.assertIsNone(firefox.wsl_path("/home/someone"))
        self.assertIsNone(firefox.wsl_path("Profiles/abc.default"))

    def test_profiles_of_the_windows_firefox(self):
        listed = self.profile(os.path.join(self.root, "Profiles", "abc.default"))
        elsewhere = self.profile(os.path.join(self.mnt, "d", "Firefox", "Work"))
        with open(os.path.join(self.root, "profiles.ini"), "w") as fh:
            fh.write("[Profile0]\nName=Home\nIsRelative=1\nPath=Profiles/abc.default\n\n"
                     "[Profile1]\nName=Work\nIsRelative=0\nPath=D:\\Firefox\\Work\n")
        store = self.profile(os.path.join(self.user, "AppData", "Local", "Packages", "Mozilla.Firefox_x1",
                                          "LocalCache", "Roaming", "Mozilla", "Firefox", "Profiles", "s.default"))
        found = firefox.profiles()
        for folder in (listed, elsewhere, store):
            self.assertIn(folder, found)
        self.assertEqual(firefox.names().get(listed), "Home")
        self.assertEqual(firefox.names().get(elsewhere), "Work")
        self.assertEqual(len(self.asked), 1, "Windows is asked once")
        self.assertTrue(self.asked[0][0].endswith("cmd.exe"))

    def test_windows_that_does_not_answer(self):
        self.answer = None
        self.assertEqual(firefox.windows_roots(), ())
        self.answer = ANSWER
        self.assertEqual(firefox.windows_roots(), (self.root,))   # asked again, since it had no answer
        self.assertEqual(len(self.asked), 2)

    def test_not_wsl(self):
        with mock.patch.object(firefox, "WSL_INTEROP", os.path.join(self.dir, "none")):
            self.assertEqual(firefox.windows_folders(), {})
            self.assertFalse(firefox.open_in_windows("https://music.apple.com/"))
        self.assertEqual(self.asked, [])

    def test_unset_folder_is_left_out(self):
        self.assertNotIn("ProgramFiles(x86)", firefox.windows_folders())

    def test_opens_the_windows_firefox(self):
        self.assertIsNone(firefox.windows_firefox())
        self.assertFalse(firefox.open_in_windows("https://open.spotify.com/"))
        program = os.path.join(self.mnt, "c", "Program Files", "Mozilla Firefox", "firefox.exe")
        os.makedirs(os.path.dirname(program))
        open(program, "w").close()
        self.assertEqual(firefox.windows_firefox(), program)
        started = mock.MagicMock()
        with mock.patch.object(firefox.subprocess, "Popen", return_value=started) as popen:
            self.assertTrue(firefox.open_in_windows("https://open.spotify.com/"))
        self.assertEqual(popen.call_args.args[0], [program, "https://open.spotify.com/"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])


if __name__ == "__main__":
    unittest.main()
