"""Cover files (library.py): written only in a folder of the user's own, never
through a link, and kept only in the runtime folder, which is not on disk (#74)."""
import os
import subprocess
import sys
import tempfile
import unittest

from tests import ROOT, SCRATCH

from library import Library


class WriteCover(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(dir=SCRATCH)
        self.folder = os.path.join(self.base, "covers")
        self.outside = os.path.join(self.base, "outside")
        with open(self.outside, "wb") as fh:
            fh.write(b"someone else's file")

    def outside_is_untouched(self):
        with open(self.outside, "rb") as fh:
            self.assertEqual(fh.read(), b"someone else's file")

    def test_writes_the_file(self):
        path = Library._write_cover(os.path.join(self.folder, "a.jpg"), b"picture")
        self.assertEqual(path, os.path.join(self.folder, "a.jpg"))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"picture")
        self.assertEqual(os.stat(self.folder).st_mode & 0o777, 0o700)
        self.assertEqual(os.listdir(self.folder), ["a.jpg"])   # no temporary file left

    def test_a_link_in_place_of_the_file_is_replaced_not_followed(self):
        os.makedirs(self.folder)
        path = os.path.join(self.folder, "a.jpg")
        os.symlink(self.outside, path)
        self.assertEqual(Library._write_cover(path, b"picture"), path)
        self.assertFalse(os.path.islink(path))
        self.outside_is_untouched()

    def test_a_link_at_any_temporary_name_is_not_followed(self):
        os.makedirs(self.folder)
        for name in ("a.jpg.tmp", "a.jpg.tmp.tmp", ".a.jpg.tmp"):
            os.symlink(self.outside, os.path.join(self.folder, name))
        Library._write_cover(os.path.join(self.folder, "a.jpg"), b"picture")
        self.outside_is_untouched()

    def test_a_folder_that_is_a_link_is_refused(self):
        real = os.path.join(self.base, "real")
        os.makedirs(real)
        os.symlink(real, self.folder)
        self.assertIsNone(Library._write_cover(os.path.join(self.folder, "a.jpg"), b"picture"))
        self.assertEqual(os.listdir(real), [])

    def test_keeps_the_last_forty(self):
        for n in range(45):
            Library._write_cover(os.path.join(self.folder, f"{n}.jpg"), b"picture")
        self.assertEqual(len(os.listdir(self.folder)), 40)


class StreamCovers(unittest.TestCase):
    """Without a runtime folder there is no folder for streamed songs' covers."""

    def folder(self, runtime):
        env = {k: v for k, v in os.environ.items() if k != "XDG_RUNTIME_DIR"}
        if runtime is not None:
            env["XDG_RUNTIME_DIR"] = runtime
        out = subprocess.run([sys.executable, "-c", "import library; print(library.STREAM_COVERS)"],
                             cwd=ROOT, env=env, capture_output=True, text=True, check=True)
        return out.stdout.strip()

    def test_none_without_a_runtime_folder(self):
        self.assertEqual(self.folder(None), "None")
        self.assertEqual(self.folder(os.path.join(SCRATCH, "missing")), "None")

    def test_inside_the_runtime_folder(self):
        runtime = tempfile.mkdtemp(dir=SCRATCH)
        self.assertEqual(self.folder(runtime), os.path.join(runtime, os.environ["OCEAN_MIST_APP_ID"]))


if __name__ == "__main__":
    unittest.main()
