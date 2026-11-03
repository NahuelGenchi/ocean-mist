"""The playback engine when something goes wrong (#90): a download cut short goes on from where it stopped, a
full disk is named, an engine of another version is an update, the runtime changes places with the old one in
one step, a damaged runtime is found at the start, a helper that will not end is ended with what it started,
and the administrator step refuses a file under a folder others can change. The runtime is a stand-in, the
download comes from a local server (127.0.0.1) or a local zip: nothing is asked of GitHub, and no Chromium runs."""
import errno
import grp
import hashlib
import http.server
import io
import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from unittest import mock

from tests import ROOT, SCRATCH

import engine
import firefox

BLOB = os.urandom(300_000)   # the "archive" a download test fetches
BLOB_SHA = hashlib.sha256(BLOB).hexdigest()


class Server(http.server.BaseHTTPRequestHandler):
    """Serves BLOB, with Range requests; `cut` bytes into the first answers it closes the connection (as a
    network that drops would), and `ranges` says whether it honours a Range at all."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        server = self.server
        asked = self.headers.get("Range") or ""
        server.asked.append(asked)
        start = int(asked[6:-1]) if asked.startswith("bytes=") and asked.endswith("-") and server.ranges else 0
        body = BLOB[start:]
        self.send_response(206 if start else 200)
        if start:
            self.send_header("Content-Range", f"bytes {start}-{len(BLOB) - 1}/{len(BLOB)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if server.cuts:
            server.cuts -= 1
            self.wfile.write(body[:server.cut])
            self.wfile.flush()
            self.connection.shutdown(2)   # the connection drops halfway
            return
        self.wfile.write(body)


class Download(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Server)
        self.server.asked, self.server.cuts, self.server.cut, self.server.ranges = [], 0, 100_000, True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/electron.zip"
        self.path = os.path.join(tempfile.mkdtemp(dir=SCRATCH), engine.PARTIAL)

    def fetch(self, **kwargs):
        engine.download(self.url, self.path, len(BLOB), kwargs.pop("sha", BLOB_SHA), wait=0, **kwargs)
        with open(self.path, "rb") as fh:
            return fh.read()

    def test_a_download_cut_halfway_goes_on_from_there(self):
        self.server.cuts = 1
        self.assertEqual(self.fetch(), BLOB)
        self.assertEqual(self.server.asked, ["", "bytes=100000-"])

    def test_what_an_earlier_try_left_is_gone_on_from(self):
        with open(self.path, "wb") as fh:
            fh.write(BLOB[:123_456])
        self.assertEqual(self.fetch(), BLOB)
        self.assertEqual(self.server.asked, ["bytes=123456-"])

    def test_a_server_that_sends_all_of_it_again_is_taken_from_the_start(self):
        self.server.ranges = False
        with open(self.path, "wb") as fh:
            fh.write(BLOB[:50_000])
        self.assertEqual(self.fetch(), BLOB)

    def test_three_tries_then_what_came_is_kept(self):
        self.server.cuts, self.server.cut = 5, 50_000
        with self.assertRaises(engine.EngineError) as caught:
            self.fetch()
        self.assertIn("internet connection", str(caught.exception))
        self.assertEqual(len(self.server.asked), engine.TRIES)
        self.assertEqual(os.path.getsize(self.path), 3 * 50_000)   # each try went on from the last
        self.server.cuts = 0
        self.assertEqual(self.fetch(), BLOB)   # ...and the next one finishes it

    def test_a_wrong_download_is_not_kept(self):
        with self.assertRaises(engine.EngineError) as caught:
            self.fetch(sha="0" * 64)
        self.assertIn("checksum", str(caught.exception))
        self.assertFalse(os.path.exists(self.path))

    def test_a_cancelled_download_is_not_kept(self):
        with self.assertRaises(engine.EngineError) as caught:
            self.fetch(cancelled=lambda: True)
        self.assertEqual(caught.exception.code, "cancelled")
        self.assertFalse(os.path.exists(self.path))

    def test_a_full_disk_is_named_and_what_came_is_kept(self):
        real = open

        class Full(io.FileIO):
            def write(self, data):
                raise OSError(errno.ENOSPC, "No space left on device")

        def opening(path, mode="r", *args, **kwargs):
            if path == self.path and "a" in mode or path == self.path and "w" in mode:
                return Full(path, "ab")
            return real(path, mode, *args, **kwargs)
        with open(self.path, "wb") as fh:
            fh.write(BLOB[:10_000])
        with mock.patch.object(engine, "open", opening, create=True):
            with self.assertRaises(engine.EngineError) as caught:
                self.fetch()
        self.assertEqual(caught.exception.code, "space")
        self.assertIn("disk is full", str(caught.exception))
        self.assertNotIn("internet", str(caught.exception))
        self.assertEqual(os.path.getsize(self.path), 10_000)


# ---- installs, from a local zip ----

STAND_IN = r'''#!%s
import json, os, signal, subprocess, sys
def say(**event):
    print("OMP " + json.dumps(event), flush=True)
if os.environ.get("OM_STUBBORN") or os.path.exists(os.path.join(os.path.dirname(sys.argv[0]), "stubborn")):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)   # a helper that will not end, with a process of its own
    child = subprocess.Popen(["sleep", "60"])
    with open("child.pid", "w") as fh:
        fh.write(str(child.pid))
sys.stdin.readline()
say(event="start")
say(event="ready", store="")
for line in sys.stdin:
    cmd = json.loads(line)
    if cmd.get("cmd") == "play":
        say(event="state", serial=cmd["serial"], state="playing", position=cmd.get("from", 0), duration=200)
    elif cmd.get("cmd") == "quit" and not os.path.exists(os.path.join(os.path.dirname(sys.argv[0]), "stubborn")):
        break
'''


def runtime_zip(version=engine.VERSION):
    """A runtime as castlabs's archive holds one: electron (the stand-in), version, libffmpeg.so and more."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        def add(name, data, mode=0o644):
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFREG | mode) << 16
            zf.writestr(info, data)
        add("electron", STAND_IN % sys.executable, 0o755)
        add("version", version)
        add("libffmpeg.so", b"\0" * 1000)
        add("resources/default_app.asar", b"invented" * 100)
    return buffer.getvalue()


class Install(unittest.TestCase):
    def setUp(self):
        self.root = os.path.join(tempfile.mkdtemp(dir=SCRATCH), "engine")
        self.zip = runtime_zip()
        pins = {"SIZE": len(self.zip), "SHA256": hashlib.sha256(self.zip).hexdigest(), "UNPACKED": 1 << 20,
                "URL": "https://github.com/invented/electron.zip"}
        for name, value in pins.items():
            patcher = mock.patch.object(engine, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.events = []
        token = firefox.AppleToken("invented-sign-in", "music.apple.com", 0, True, True, 1)
        self.engine = engine.Engine(lambda service: token, lambda kind, **f: self.events.append(dict(f, kind=kind)),
                                    root=self.root, fetch=self.local)
        self.addCleanup(self.engine.close)

    def local(self, url, path, size, sha256, progress=None, cancelled=None):
        """The download, "fetched" from the local zip, checked as the real one is."""
        self.assertEqual((url, size, sha256), (engine.URL, engine.SIZE, engine.SHA256))
        with open(path, "wb") as fh:
            fh.write(self.zip)

    def old_engine(self, version="1.0.0+old"):
        """An engine of another version, as an earlier Ocean Mist installed it."""
        folder = os.path.join(self.root, "electron")
        os.makedirs(folder)
        program = os.path.join(folder, "electron")
        with open(program, "w") as fh:
            fh.write(STAND_IN % sys.executable)
        os.chmod(program, 0o755)
        with open(os.path.join(folder, "old-file"), "w") as fh:
            fh.write("from the old engine")
        with open(os.path.join(self.root, "engine.json"), "w") as fh:
            json.dump({"version": version}, fh)

    def test_an_install_keeps_the_list_of_its_files(self):
        engine.install_runtime(self.root, self.local)
        self.assertTrue(self.engine.installed())
        with open(os.path.join(self.root, "engine.json")) as fh:
            meta = json.load(fh)
        self.assertEqual(sorted(meta["files"]), ["electron", "libffmpeg.so", "resources/default_app.asar", "version"])
        self.assertEqual(meta["files"]["libffmpeg.so"][2], hashlib.sha256(b"\0" * 1000).hexdigest())
        self.assertEqual([n for n in os.listdir(self.root) if n.startswith(".")], [])   # nothing left beside it

    def test_an_engine_of_another_version_is_an_update(self):
        self.old_engine()
        self.assertFalse(self.engine.installed())
        status = self.engine.status()
        self.assertEqual(status["outdated"], {"from": "1.0.0+old", "to": engine.VERSION})
        with self.assertRaises(engine.EngineError) as caught:
            self.engine.play(1, ["1000000001"])
        self.assertEqual((caught.exception.code, str(caught.exception)), ("not-installed", engine.WORDS["outdated"]))
        engine.install_runtime(self.root, self.local)
        self.assertTrue(self.engine.installed())
        self.assertIsNone(self.engine.status()["outdated"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "electron", "old-file")))

    def test_the_new_runtime_takes_the_old_ones_place_with_no_moment_without_one(self):
        self.old_engine()
        a, b = (os.path.join(tempfile.mkdtemp(dir=SCRATCH), n) for n in "ab")
        os.makedirs(a)
        os.makedirs(b)
        if not engine._exchange(a, b):
            self.skipTest("this file system cannot swap two folders in one step")
        program, gaps, done = os.path.join(self.root, "electron", "electron"), [], threading.Event()

        def watch():
            while not done.is_set():
                if not os.path.exists(program):
                    gaps.append(time.monotonic())
        watcher = threading.Thread(target=watch)
        watcher.start()
        try:
            engine.install_runtime(self.root, self.local)
        finally:
            done.set()
            watcher.join()
        self.assertEqual(gaps, [])
        self.assertTrue(self.engine.installed())

    def test_where_folders_cannot_be_swapped_a_failed_move_puts_the_old_runtime_back(self):
        self.old_engine()
        place, fresh = os.path.join(self.root, "electron"), os.path.join(tempfile.mkdtemp(dir=self.root), "electron")
        os.makedirs(fresh)
        real = os.replace

        def replace(a, b):
            if a == fresh:
                raise OSError(errno.EIO, "invented")
            return real(a, b)
        with mock.patch.object(engine, "_exchange", lambda a, b: False), mock.patch.object(engine.os, "replace", replace):
            with self.assertRaises(OSError):
                engine._swap(fresh, place)
        self.assertTrue(os.path.isfile(os.path.join(place, "old-file")))
        self.assertEqual([n for n in os.listdir(self.root) if n.startswith(".replaced-")], [])

    def test_where_folders_cannot_be_swapped_the_old_one_is_moved_aside(self):
        self.old_engine()
        place, fresh = os.path.join(self.root, "electron"), os.path.join(tempfile.mkdtemp(dir=self.root), "electron")
        os.makedirs(fresh)
        open(os.path.join(fresh, "new-file"), "w").close()
        with mock.patch.object(engine, "_exchange", lambda a, b: False):
            gone = engine._swap(fresh, place)
        self.assertTrue(os.path.isfile(os.path.join(place, "new-file")))
        self.assertTrue(os.path.isfile(os.path.join(gone, "old-file")))

    def test_engine_json_is_written_whole_or_not_at_all(self):
        path = os.path.join(tempfile.mkdtemp(dir=SCRATCH), "engine.json")
        engine._write_json(path, {"version": "before"})
        with mock.patch.object(engine.json, "dump", side_effect=OSError(errno.ENOSPC, "full")):
            with self.assertRaises(OSError):
                engine._write_json(path, {"version": "after"})
        with open(path) as fh:
            self.assertEqual(json.load(fh), {"version": "before"})
        self.assertEqual(os.listdir(os.path.dirname(path)), ["engine.json"])

    def test_room_is_made_by_an_engine_that_plays_nothing_any_more(self):
        self.old_engine()
        needed = max(0, engine.SIZE) + engine.UNPACKED + (100 << 20)
        there = engine._folder_size(os.path.join(self.root, "electron"))
        usage = shutil.disk_usage(self.root)._replace(free=needed - there)   # short by just the old engine
        with mock.patch.object(engine.shutil, "disk_usage", return_value=usage):
            engine.install_runtime(self.root, self.local)
        self.assertTrue(self.engine.installed())

    def test_short_of_room_the_installed_engine_stays(self):
        engine.install_runtime(self.root, self.local)
        usage = shutil.disk_usage(self.root)._replace(free=1 << 20)
        with mock.patch.object(engine.shutil, "disk_usage", return_value=usage):
            with self.assertRaises(engine.EngineError) as caught:
                engine.install_runtime(self.root, self.local)
        self.assertIn("MB are free", str(caught.exception))
        self.assertTrue(self.engine.installed())

    def test_a_damaged_runtime_is_found_at_the_start(self):
        engine.install_runtime(self.root, self.local)
        with open(os.path.join(self.root, "electron", "libffmpeg.so"), "ab") as fh:
            fh.write(b"changed")
        self.engine.play(5, ["1000000001"])
        deadline = time.monotonic() + 5
        while not any(e.get("phase") == "error" for e in self.events) and time.monotonic() < deadline:
            time.sleep(0.05)
        told = [e for e in self.events if e.get("phase") == "error"]
        self.assertEqual([(e["code"], e["message"]) for e in told], [("not-installed", engine.WORDS["damaged"])])
        self.assertIsNone(self.engine.proc, "nothing was started")
        self.assertFalse(self.engine.installed())
        self.assertTrue(self.engine.status()["damaged"])
        self.engine._install()   # installed again (the licence was accepted before): the mark goes
        self.assertTrue(self.engine.installed())
        self.assertFalse(self.engine.status()["damaged"])

    def test_a_file_whose_time_changed_but_not_its_bytes_is_fine(self):
        engine.install_runtime(self.root, self.local)
        path = os.path.join(self.root, "electron", "version")
        os.utime(path, ns=(10**18, 10**18))
        self.assertTrue(self.engine._intact())
        with open(os.path.join(self.root, "engine.json")) as fh:
            self.assertEqual(json.load(fh)["files"]["version"][1], 10**18)   # noted, so it is not hashed again

    def test_a_missing_file_is_damage(self):
        engine.install_runtime(self.root, self.local)
        os.unlink(os.path.join(self.root, "electron", "resources", "default_app.asar"))
        self.assertFalse(self.engine._intact())
        self.assertTrue(self.engine.damaged)

    def test_an_engine_from_before_the_list_is_taken_as_it_is(self):
        self.old_engine(engine.VERSION)
        self.assertTrue(self.engine._intact())
        self.assertTrue(self.engine.installed())

    def test_a_helper_that_will_not_end_is_ended_with_what_it_started(self):
        engine.install_runtime(self.root, self.local)
        open(os.path.join(self.root, "electron", "stubborn"), "w").close()
        self.engine.play(9, ["1000000001"])
        deadline = time.monotonic() + 5
        pid_file = os.path.join(self.root, "child.pid")
        while not (os.path.exists(pid_file) and os.path.getsize(pid_file)) and time.monotonic() < deadline:
            time.sleep(0.05)
        with open(pid_file) as fh:
            child, proc = int(fh.read()), self.engine.proc
        self.assertEqual(os.getpgid(proc.pid), proc.pid, "the helper leads a process group of its own")
        self.assertTrue(alive(child))
        engine.Engine._reap(proc, 0.2)   # the kill fallback: it ignores SIGTERM
        self.assertIsNotNone(proc.poll())
        deadline = time.monotonic() + 3
        while alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(alive(child), "what the helper started went with it")


def alive(pid):
    """Whether `pid` runs (a zombie waiting for its parent does not)."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


class SandboxStep(unittest.TestCase):
    """allow-sandbox.sh's check of the folders above the engine's file, loaded from the script (which runs only
    when started): nothing is run as root and nothing is added to the system."""

    def check(self, *modes):
        top = tempfile.mkdtemp(dir=SCRATCH)
        os.chmod(top, 0o755)
        folder = top
        for n, mode in enumerate(modes):
            folder = os.path.join(folder, f"d{n}")
            os.mkdir(folder)
            os.chmod(folder, mode)
        program = os.path.join(folder, "electron")
        open(program, "w").close()
        script = os.path.join(ROOT, "engine", "allow-sandbox.sh")
        ran = subprocess.run(["bash", "-c", 'source "$1"; check_folders "$2" "$3"', "check", script, program, str(os.getuid())],
                             capture_output=True, text=True, timeout=30)
        for n in range(len(modes)):   # (so the scratch folder can go)
            os.chmod(os.path.join(top, *[f"d{i}" for i in range(n + 1)]), 0o755)
        return ran.returncode, ran.stdout

    def test_folders_only_their_owner_may_change_are_fine(self):
        self.assertEqual(self.check(0o755, 0o700)[0], 0)

    def test_a_folder_anyone_may_change_is_refused(self):
        code, said = self.check(0o755, 0o777)
        self.assertEqual(code, 1)
        self.assertIn("Anyone may change a folder above", said)

    def test_a_folder_anyone_may_add_to_but_with_the_sticky_bit_is_fine(self):
        self.assertEqual(self.check(0o1777, 0o755)[0], 0)

    def test_a_group_writable_folder_is_fine_only_for_a_private_group(self):
        code, said = self.check(0o775)
        me = pwd.getpwuid(os.getuid()).pw_name
        group = grp.getgrgid(os.getgid())
        private = group.gr_name == me and not group.gr_mem
        self.assertEqual(code, 0 if private else 1, said)


if __name__ == "__main__":
    unittest.main()
