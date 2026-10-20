"""The playback engine: what plays the songs of Apple Music and Spotify.

Their audio is protected with Widevine, which only a browser engine that carries it can play, and the
view Ocean Mist is made of (WebKitGTK) does not. So a song of Apple Music or of Spotify is played by a
small helper (engine/helper/): the service's own web player in a window that is never shown, signed in
with the same Firefox sign-in apple.py or spotify.py reads (Apple's one cookie, Spotify's cookies),
run by a Chromium-based runtime that has Widevine support (castlabs's Electron build for content
security). Ocean Mist keeps the lists, the queue and the controls; the helper only makes the sound,
one song at a time, one service at a time: a helper serving the other service is ended before the
next song's starts.

The runtime is not part of Ocean Mist. It is downloaded once, when the user asks for it behind a licence
screen, from the address and with the checksum written below, and kept under
$XDG_DATA_HOME/ocean-mist/engine/. Google's Widevine module is not in that download either: the runtime
fetches it from Google the first time it starts. The helper is started only when an Apple Music or a
Spotify song is played, stops after a minute without one (ten while paused) and when the player closes.

The helper gets the sign-in once, as its first line on standard input, and keeps it in memory only: it
is never in an argument, an environment variable, a file or a log. It runs with a short list of
environment variables and keeps Chromium's sandbox on; where the system forbids that (Ubuntu does, for a
program it has no AppArmor profile for) it does not start, and engine/allow-sandbox.sh is the one
administrator step that allows it. The player asks for it in the system's own password window (pkexec,
polkit): the password goes to the system, never to Ocean Mist. Where there is no such window the page
shows the same step as a command to run in a terminal.

    python3 engine.py           says whether the engine is installed and whether its sandbox works
    python3 engine.py check     starts the installed engine for a moment and checks its sandbox
"""
import collections
import hashlib
import http.client
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(HERE, "engine", "helper")
SANDBOX_SCRIPT = os.path.join(HERE, "engine", "allow-sandbox.sh")
ROOT = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "ocean-mist", "engine")

# what is downloaded: castlabs's build of Electron (MIT) for content security, with Widevine support
VERSION = "44.5.1+wvcus"
URL = ("https://github.com/castlabs/electron-releases/releases/download/v44.5.1%2Bwvcus/"
       "electron-v44.5.1%2Bwvcus-linux-x64.zip")
SOURCE = "github.com/castlabs/electron-releases"
SHA256 = "afa7d4ea49abf1e91c5be0f88cfe1abdb977ff3454d57658e90b004aacbde9cc"   # from castlabs's SHASUMS256.txt for that release
SIZE = 123104849            # bytes of the archive
UNPACKED = 296027496        # ...and of what it holds
WIDEVINE = 45 << 20         # what Google's module and Chromium's own files add the first time it runs
DOWNLOAD_HOSTS = re.compile(r"^https://(?:[a-z0-9-]+\.)*(?:github\.com|githubusercontent\.com)/", re.I)
ENTRIES = 400               # most files the archive may hold (it holds 72)
APPARMOR_DIR = "/etc/apparmor.d"
PROFILE_PREFIX = "ocean-mist-engine"                    # what allow-sandbox.sh calls its profiles
PROFILE_PATH = re.compile(r"^/[A-Za-z0-9_./+-]+$")      # the characters that script can put in a profile
PACKAGE_PROFILE = os.path.join(APPARMOR_DIR, PROFILE_PREFIX)   # what the Debian package installs: one profile for the engine's default place
PACKAGE_NAMES = re.compile(r"^/(?:home/[^/]+|root)/\.local/share/ocean-mist/engine/electron/electron$")   # the file that profile names
PACKAGE_DIR = "/usr/lib/ocean-mist"   # where the package puts the player: gone after a removal, while the profile's file stays until a purge
PACKAGE_DISABLED = os.path.join(APPARMOR_DIR, "disable", PROFILE_PREFIX)   # the link aa-disable makes to switch that profile off
PKEXEC = "/usr/bin/pkexec"  # polkit's way to run a program as the administrator after a password in the system's window
BASH = "/usr/bin/bash"
ASK_WITHIN = 600            # seconds the password window may stay open before Ocean Mist stops waiting for it

IDLE_STOPPED = 60           # seconds without music after which the helper ends
IDLE_PAUSED = 600           # ...and while a song is paused
IDLE_LOOK = 5               # seconds between looks at whether it is time
READY_WITHIN = 330          # seconds the helper gets to be ready (Google's module can need the first time)
CHECK_WITHIN = 60           # ...and to start for the check of its sandbox
PROGRESS_EVERY = 0.25       # seconds between the download's progress reports

# the only environment variables the helper gets: Chromium reads others (CHROME_DESKTOP, for one, made it
# crash on newer GLib), and the player's own can hold session secrets. The proxies are there because the
# download goes through them, and a network that needs one needs it for Apple too
ENV_KEEP = ("HOME", "PATH", "USER", "LOGNAME", "LANG", "LANGUAGE", "XDG_RUNTIME_DIR", "XDG_DATA_DIRS",
            "XDG_CONFIG_DIRS", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "DBUS_SESSION_BUS_ADDRESS",
            "WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY", "PULSE_SERVER", "PULSE_COOKIE", "PULSE_RUNTIME_PATH",
            "http_proxy", "https_proxy", "all_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
SAME_SITE = {0: "no_restriction", 1: "lax", 2: "strict"}   # Firefox's number, as Electron names it
LEVELS = re.compile(r"[A-Za-z0-9+/]{512}")   # a helper's levels line: 384 bytes, as base64

# what the page is told, per kind of failure
WORDS = {
    "not-installed": "Apple Music songs need the playback engine; install it under Apple Music, Playback engine",
    "signed-out": "Apple Music says you are signed out; sign in again in Firefox",
    "no-subscription": "Your Apple Music account has no active subscription, so its songs cannot be played",
    "unavailable": "Apple Music cannot play this song for your account",
    "limit": "Apple Music is playing on another device; it allows one at a time",
    "offline": "Ocean Mist cannot reach Apple Music; check the internet connection",
    "play": "Apple Music could not play this song",
    "widevine": "The playback engine could not get Google's Widevine module; check the internet connection and try again",
    "sandbox": "Your system does not let the playback engine start its security sandbox; it needs a one-time step, on the playback engine's page",
    "crash": "The playback engine stopped unexpectedly",
    "page": "Apple Music changed something Ocean Mist depends on, so the engine needs an update",
    "start": "The playback engine could not start",
}

# the same, in Spotify's words: what the page is told while a Spotify song plays (and "no-plan", which only
# Spotify's helper says: it plays only an account whose plan its page said)
SPOTIFY_WORDS = dict(WORDS, **{
    "not-installed": "Spotify songs need the playback engine; install it under Spotify, Playback engine",
    "signed-out": "Spotify says you are signed out; sign in again in Firefox",
    "no-subscription": "Your Spotify account has no active Premium subscription, so its songs cannot be played",
    "no-plan": "Ocean Mist could not confirm that your Spotify account has Premium, so its songs are not played; try again in a moment",
    "unavailable": "Spotify cannot play this song for your account",
    "limit": "Spotify is playing on another device; it allows one player at a time",
    "offline": "Ocean Mist cannot reach Spotify; check the internet connection",
    "play": "Spotify could not play this song",
    "page": "Spotify changed something Ocean Mist depends on, so the engine needs an update",
})


class EngineError(Exception):
    """What went wrong, worded for the page. `code` is a key of WORDS where there is one."""

    def __init__(self, message, code="start"):
        super().__init__(message)
        self.code = code


class _Stay(urllib.request.HTTPRedirectHandler):
    """A download that is sent elsewhere is followed only to GitHub's own servers."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not DOWNLOAD_HOSTS.match(newurl):
            raise urllib.error.HTTPError(newurl, code, "the download was sent somewhere else", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def helper_env(environ=None):
    """The environment the helper runs with."""
    environ = os.environ if environ is None else environ
    env = {key: environ[key] for key in ENV_KEEP if key in environ}
    env.update({key: value for key, value in environ.items() if key.startswith("LC_")})
    return env


def cookie_of(token):
    """An Apple Music sign-in (firefox.AppleToken) as the helper's first line has it."""
    return {"value": token.value, "host": token.host, "expires": token.expires or 0, "secure": bool(token.secure),
            "http_only": bool(token.http_only), "same_site": SAME_SITE.get(token.same_site, "unspecified")}


def spotify_cookie_of(cookie):
    """One of a Spotify sign-in's cookies (firefox.SpotifyCookie) as that helper's first line has them."""
    return {"name": cookie.name, "value": cookie.value, "host": cookie.host, "expires": cookie.expires or 0,
            "secure": bool(cookie.secure), "http_only": bool(cookie.http_only),
            "same_site": SAME_SITE.get(cookie.same_site, "unspecified")}


def start_line(service, token, volume, muted):
    """The helper's first line for `service`: the sign-in as that service's helper takes it, and the
    volume the player is at. Apple's helper gets one cookie, Spotify's its cookies (sp_dc first)."""
    line = {"cmd": "start", "volume": volume, "muted": muted}
    if service == "spotify":
        line.update(service="spotify", cookies=[spotify_cookie_of(cookie) for cookie in token.cookies])
    else:
        line.update(cookie=cookie_of(token))
    return line


def restricted():
    """Does this system keep programs from creating the user namespaces Chromium's sandbox uses
    unless AppArmor has a profile for them (Ubuntu 24.04 and later)?"""
    try:
        with open("/proc/sys/kernel/apparmor_restrict_unprivileged_userns") as fh:
            return fh.read().strip() == "1"
    except OSError:
        return False


def profile_name(program):
    """What allow-sandbox.sh calls the profile for the file `program`."""
    return f"{PROFILE_PREFIX}-{hashlib.sha1(program.encode()).hexdigest()[:10]}"


def package_covers(program):
    """Does the Debian package's profile name the engine's file, so that no step is needed? The package is installed
    (its profile's file alone says too little: it stays after a removal), the profile has not been switched off, and
    the file sits in the default place of a home folder under /home, or root's, which is what the profile names. A
    home folder that AppArmor was told about in its own settings is covered too but is not seen here: the page then
    explains the step. Whether the profile is loaded cannot be read without administrator rights: the check of the
    sandbox decides in the end."""
    return (os.path.isdir(PACKAGE_DIR) and os.path.exists(PACKAGE_PROFILE) and not os.path.lexists(PACKAGE_DISABLED)
            and bool(PACKAGE_NAMES.fullmatch(os.path.realpath(program))))


def profile_present(program):
    """Does the system hold that script's profile for the engine's file? It outlives Remove: only
    an administrator can take it away."""
    return os.path.exists(os.path.join(APPARMOR_DIR, profile_name(os.path.realpath(program))))


def can_ask():
    """Is there a way to ask for the administrator's password in the system's own window here (pkexec)?"""
    return os.access(PKEXEC, os.X_OK) and os.path.isfile(SANDBOX_SCRIPT)


def ask_command(args):
    """The command that runs engine/allow-sandbox.sh with `args` as the administrator, after the system's
    password window. The script itself when it can be run, so the window names it, else through bash."""
    if os.access(SANDBOX_SCRIPT, os.X_OK):
        return [PKEXEC, SANDBOX_SCRIPT, *args]
    return [PKEXEC, BASH, SANDBOX_SCRIPT, *args]


def _sentence(lines):
    """The last thing the step said, as one plain line for the page (nothing but printable characters)."""
    for line in reversed(list(lines)):
        text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", str(line))   # colour codes and the like
        text = "".join(c for c in text if c.isprintable()).strip()
        if text:
            return text[:240]
    return ""


def sandbox_failure(lines):
    """Did Chromium say it could not start its sandbox? (`lines`: what it printed.)"""
    return any(re.search(r"(FATAL|ERROR).*(sandbox|namespace)", line, re.I) or "No usable sandbox" in line for line in lines)


# ---- the download ----

def download(url, path, size, sha256, progress=None, opener=None, cancelled=None):
    """Fetch `url` into the file `path`, checking its size and its SHA-256 as it comes. EngineError when
    it cannot be had or is not what was expected; nothing is left behind then."""
    opener = opener or urllib.request.build_opener(_Stay)
    request = urllib.request.Request(url, headers={"User-Agent": "Ocean Mist"})
    digest, done, last = hashlib.sha256(), 0, 0.0
    try:
        with opener.open(request, timeout=30) as response, open(path, "wb") as out:
            while True:
                if cancelled and cancelled():
                    raise EngineError("the download was cancelled", "cancelled")
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                done += len(chunk)
                if done > size:
                    raise EngineError("the download is bigger than it should be, so it was not used", "download")
                digest.update(chunk)
                out.write(chunk)
                if progress and time.monotonic() - last >= PROGRESS_EVERY:
                    last = time.monotonic()
                    progress(done, size)
    except urllib.error.HTTPError as error:
        _forget(path)
        raise EngineError(f"the download failed (HTTP {error.code})", "download") from None
    except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException):
        _forget(path)
        raise EngineError("Ocean Mist could not download the engine; check the internet connection", "download") from None
    except EngineError:
        _forget(path)
        raise
    if done != size or digest.hexdigest() != sha256:
        _forget(path)
        raise EngineError("the download is not the one that was expected (its checksum differs), so it was not used", "download")
    if progress:
        progress(done, size)


def _forget(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def unpack(archive, folder):
    """Unpack the runtime's archive into the new folder `folder`, as plain files: nothing may land outside
    it, links are refused, and a file keeps its executable bit but never a set-user-id one."""
    try:
        with zipfile.ZipFile(archive) as zf:
            infos = zf.infolist()
            if not infos or len(infos) > ENTRIES or sum(i.file_size for i in infos) > 2 * UNPACKED:
                raise EngineError("the archive is not the one that was expected", "download")
            top = os.path.realpath(folder)
            for info in infos:
                name = info.filename
                mode = info.external_attr >> 16
                target = os.path.realpath(os.path.join(top, name))
                if (name.startswith("/") or ".." in name.split("/") or "\\" in name or (target != top and not target.startswith(top + os.sep))
                        or (mode & 0o170000) == 0o120000):
                    raise EngineError("the archive holds a path that is not allowed, so it was not used", "download")
                if info.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                os.chmod(target, 0o755 if mode & 0o111 else 0o644)
    except zipfile.BadZipFile:
        raise EngineError("the download is not a readable archive, so it was not used", "download") from None
    except OSError:
        raise EngineError("Ocean Mist could not unpack the engine; check the free space", "download") from None


def install_runtime(root, fetch=download, progress=None, steps=None, cancelled=None):
    """Download and unpack the runtime into `root`/electron. It lands whole or not at all: it is made beside
    the old one and moved into place. `steps(name)` says which part is on. EngineError otherwise."""
    say = steps or (lambda name: None)
    os.makedirs(root, exist_ok=True)
    need = SIZE + UNPACKED + (100 << 20)
    if shutil.disk_usage(root).free < need:
        raise EngineError(f"there is not enough free space for the engine (it needs about {need >> 20} MB)", "download")
    for name in os.listdir(root):   # what an install that was cut short left
        if name.startswith(".installing-"):
            shutil.rmtree(os.path.join(root, name), ignore_errors=True)
    work = tempfile.mkdtemp(prefix=".installing-", dir=root)
    try:
        archive = os.path.join(work, "archive.zip")
        say("downloading")
        fetch(URL, archive, SIZE, SHA256, progress, cancelled=cancelled)
        say("unpacking")
        fresh = os.path.join(work, "electron")
        os.makedirs(fresh)
        unpack(archive, fresh)
        _forget(archive)
        try:
            with open(os.path.join(fresh, "version")) as fh:
                same = fh.read().strip() == VERSION
        except OSError:
            same = False
        program = os.path.join(fresh, "electron")
        if not (same and os.access(program, os.X_OK) and os.path.isfile(os.path.join(fresh, "libffmpeg.so"))):
            raise EngineError("the download is not the engine that was expected, so it was not used", "download")
        old = os.path.join(root, "electron")
        if os.path.lexists(old):
            shutil.rmtree(old)
        os.replace(fresh, old)
        with open(os.path.join(root, "engine.json"), "w") as fh:
            json.dump({"version": VERSION, "sha256": SHA256, "installed": time.strftime("%Y-%m-%d")}, fh)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---- the engine ----

class Engine:
    """The playback engine of the player. Methods are called from any thread.

    `token_source(service)` gives the sign-in of `service` ("apple", firefox.AppleToken, or "spotify",
    firefox.SpotifyToken), or raises EngineError; `emit(event, **data)` tells the page: "engine" for what
    the song is doing (`phase`: starting, loading, playing, paused, ended, error) and "engine-setup" for
    the install and the check."""

    def __init__(self, token_source, emit, root=ROOT, helper=HELPER, popen=subprocess.Popen, fetch=download):
        self.token_source, self.emit = token_source, emit
        self.root, self.helper, self.popen, self.fetch = root, helper, popen, fetch
        self.program = os.path.join(root, "electron", "electron")
        self.lock = threading.RLock()
        self.proc = None           # the running helper
        self.service = None        # ...and the service it serves ("apple" or "spotify")
        self.ready = False         # ...and whether it said so
        self.started = False       # ...and whether it got as far as saying it started
        self.leaving = None        # the helper that was asked to end
        self.fail = None           # what the helper said when it gave up
        self.tail = collections.deque(maxlen=40)   # the last lines it printed on its error output
        self.song = None           # what the page asked for last: {serial, ids, position, duration, phase}
        self.volume, self.muted = 0.5, False
        self.quiet = time.monotonic()     # since when nothing of Apple's plays
        self.installing = None     # {"step", "done", "total"} while the runtime comes
        self.stop_install = False
        self.sandbox = "unknown"   # ok | blocked, as the last start or check found
        self.error = ""            # why the last install or check failed
        self.watcher = None
        self.checking = threading.Lock()   # one check of the sandbox at a time: they share a folder
        self.asking = False        # whether the administrator step is out for a password (or running)
        self.asker = None          # ...and the process that waits for it
        self.ask_cancelled = False
        self.ask_failed = False    # whether the last time it was asked for, it did not work

    # -- what is there --
    def installed(self):
        try:
            with open(os.path.join(self.root, "engine.json")) as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            return False
        return isinstance(meta, dict) and meta.get("version") == VERSION and os.access(self.program, os.X_OK)

    def status(self):
        with self.lock:
            running = self.proc is not None and self.proc.poll() is None
            real = os.path.realpath(self.program)   # a profile matches the file as the kernel resolves it
            return {
                "installed": self.installed(), "version": VERSION, "source": SOURCE, "size": SIZE, "unpacked": UNPACKED,
                "extra": WIDEVINE, "sha256": SHA256, "folder": self.root, "running": running, "ready": running and self.ready,
                "installing": dict(self.installing) if self.installing else None, "error": self.error,
                "sandbox": self.sandbox, "restricted": restricted(), "covered": package_covers(self.program),
                "profile": profile_present(self.program), "path_ok": bool(PROFILE_PATH.match(real)),
                "ask": can_ask() and bool(PROFILE_PATH.match(real)), "asking": self.asking, "ask_failed": self.ask_failed,
                "command": f"sudo bash {shlex.quote(SANDBOX_SCRIPT)} {shlex.quote(real)}",
                "undo": f"sudo bash {shlex.quote(SANDBOX_SCRIPT)} --remove {shlex.quote(real)}",
            }

    def setup(self):
        """Tell the page how the engine stands (the install's progress, a check's result)."""
        self.emit("engine-setup", **self.status())

    # -- installing --
    def install(self, accept, version):
        """Download and unpack the runtime, in a thread, when the user accepted the licence screen
        (for `version` of the engine, the one it showed). The page hears how it goes."""
        if accept is not True or version != VERSION:
            raise EngineError("the licence screen was not accepted for this version of the engine", "start")
        if restricted() and not PROFILE_PATH.match(os.path.realpath(self.program)) and not package_covers(self.program):
            raise EngineError(
                "This system needs a one-time administrator step for the engine, and the step cannot name a folder with spaces "
                f"or unusual characters in its path ({self.root}). Start Ocean Mist with XDG_DATA_HOME set to a folder whose "
                "path has only letters, digits and _ . / + - in it, then install the engine.", "start")
        with self.lock:
            if self.installing:
                return
            self.installing = {"step": "downloading", "done": 0, "total": SIZE}
            self.stop_install = False
            self.error = ""
        threading.Thread(target=self._install, daemon=True).start()

    def cancel(self):
        self.stop_install = True

    def _install(self):
        def progress(done, total):
            with self.lock:
                if self.installing:
                    self.installing.update(done=done, total=total)
            self.setup()

        def step(name):
            with self.lock:
                if self.installing:
                    self.installing.update(step=name)
            self.setup()
        try:
            gone = self.stop(final=True)
            if gone is not None:
                self._reap(gone, 5)
            install_runtime(self.root, self.fetch, progress, step, cancelled=lambda: self.stop_install)
            step("checking")
            self.check(inside=True)
        except EngineError as error:
            self.error = "" if error.code == "cancelled" else str(error)
        except Exception as error:   # whatever else the disk or the archive did
            self.error = f"the engine could not be installed ({type(error).__name__})"
        finally:
            with self.lock:
                self.installing = None
            self.setup()

    def remove(self):
        """Stop the helper and delete the engine and everything it kept."""
        if self.installing:
            raise EngineError("the engine is being installed", "start")
        gone = self.stop(final=True)
        if gone is not None:
            self._reap(gone, 5)   # Chromium writes while it ends: the folder goes after it
        if os.path.basename(self.root) == "engine" and os.path.isdir(self.root) and not os.path.islink(self.root):
            shutil.rmtree(self.root, ignore_errors=True)
        with self.lock:
            self.sandbox = "unknown"
            self.error = "the engine's folder could not be deleted completely" if os.path.exists(self.root) else ""
        self.setup()

    # -- the administrator step --
    def allow(self):
        """Add the profile that lets the engine's file start its sandbox: the system asks for the
        administrator's password in its own window, and engine/allow-sandbox.sh runs after it. In a thread;
        the page hears how it goes, and the sandbox is checked once the profile is in. EngineError when
        it cannot be asked."""
        self._start_ask("allow")

    def disallow(self):
        """Take that profile away again, the same way."""
        self._start_ask("remove")

    def cancel_ask(self):
        """Stop waiting for the password window; it closes with the process that opened it."""
        with self.lock:
            if not self.asking:
                return
            self.ask_cancelled = True
            proc = self.asker
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:   # (past the password it is the administrator's process, which cannot be signalled)
                pass

    def _start_ask(self, kind):
        real = os.path.realpath(self.program)
        if kind == "allow" and not self.installed():
            raise EngineError("the engine is not installed yet", "start")
        if kind == "allow" and (not restricted() or self.sandbox == "ok"):   # (no reason to ask for a password)
            raise EngineError("the engine's sandbox does not need the step", "start")
        if kind == "remove" and not profile_present(self.program):
            raise EngineError("this system has no profile of Ocean Mist's for the engine's file", "start")
        if not PROFILE_PATH.match(real):
            raise EngineError("the step cannot name a folder with spaces or unusual characters in its path "
                              f"({self.root}); start Ocean Mist with XDG_DATA_HOME set to a folder whose path has only "
                              "letters, digits and _ . / + - in it", "start")
        if not can_ask():
            raise EngineError("this system has no window to ask for a password in; run the command shown instead", "start")
        with self.lock:
            if self.installing:
                raise EngineError("the engine is being installed", "start")
            if self.asking:
                return
            self.asking, self.ask_cancelled, self.ask_failed, self.error = True, False, False, ""
        threading.Thread(target=self._ask, args=(kind, real), daemon=True).start()
        self.setup()

    def _ask(self, kind, real):
        lines, code, note, expired = collections.deque(maxlen=12), None, "", []
        try:
            try:
                proc = self.popen(ask_command([real] if kind == "allow" else ["--remove", real]), stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=helper_env(), cwd="/",
                                  text=True, encoding="utf-8", errors="replace")
            except OSError:
                proc = None
                note = "Ocean Mist could not ask the system for the step"
            if proc is not None:
                with self.lock:
                    self.asker = proc
                    early = self.ask_cancelled   # (Cancel came before the window was there)
                if early:
                    try:
                        proc.terminate()
                    except OSError:
                        pass

                def give_up():   # once the password was accepted the step is the administrator's process: it cannot be signalled
                    expired.append(True)
                    try:
                        proc.kill()
                    except OSError:
                        pass
                timer = threading.Timer(ASK_WITHIN, give_up)
                timer.daemon = True
                timer.start()
                try:
                    for line in proc.stdout:
                        lines.append(line)
                    code = proc.wait()
                finally:
                    timer.cancel()
        except Exception as error:   # whatever else went wrong while it waited
            note = f"the step could not be run ({type(error).__name__})"
        finally:
            with self.lock:
                self.asker, self.asking = None, False
        said = _sentence(lines)
        with self.lock:
            cancelled = self.ask_cancelled
        if code == 0 or cancelled:   # (a step that was already running when Cancel came has done its work)
            note = ""
        elif expired:
            note = "The password window was left open too long, so nothing was changed"
        elif code == 126:
            note = "The password window was closed, so nothing was changed"
        elif code == 127:
            note = "Your system did not give the permission: the password was not accepted, or no password window could be shown"
        elif code is not None:
            note = said or "the step did not work"
        if code == 0:
            self.error, self.ask_failed = "", False
            if kind == "allow":
                self.check(inside=True)
                if self.sandbox == "blocked":   # the step went through and the sandbox still does not start
                    self.error, self.ask_failed = "The permission was added, but the engine's sandbox still does not start", True
            else:
                self.sandbox = "unknown"
        else:
            self.error, self.ask_failed = note, bool(note)
        self.setup()

    # -- checking the sandbox --
    def check(self, inside=False):
        """Start the installed engine for a moment and see whether Chromium's sandbox is on. The result is
        `sandbox` in the status; the page hears it. True when it works (False too when a check is already on
        its way: that one tells the page)."""
        if not self.checking.acquire(blocking=False):
            return False
        try:
            return self._check(inside)
        finally:
            self.checking.release()

    def _check(self, inside):
        if not self.installed():
            return False
        folder = os.path.join(self.root, "check")
        shutil.rmtree(folder, ignore_errors=True)
        lines, tail = [], collections.deque(maxlen=40)
        ok = False
        try:
            proc = self.popen([self.program, self.helper, "--selftest", f"--data={folder}"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=helper_env(), cwd=self.root,
                              text=True, encoding="utf-8", errors="replace")
        except OSError:
            self.error = WORDS["start"]
            self.sandbox = "unknown"
            if not inside:
                self.setup()
            return False
        drain = threading.Thread(target=_drain, args=(proc.stderr, tail), daemon=True)
        drain.start()
        timer = threading.Timer(CHECK_WITHIN, proc.kill)
        timer.daemon = True
        timer.start()
        try:
            for line in proc.stdout:
                if line.startswith("OMP "):
                    try:
                        lines.append(json.loads(line[4:]))
                    except ValueError:
                        pass
            proc.wait()
            drain.join(1)   # its last words may still be in the pipe (a child that kept it open is not waited for)
        finally:
            timer.cancel()
            shutil.rmtree(folder, ignore_errors=True)
        said = next((e for e in lines if e.get("event") == "selftest"), None)
        if said and said.get("ok"):
            self.sandbox, self.error, ok = "ok", "", True
        elif said or sandbox_failure(list(tail)):
            self.sandbox, self.error = "blocked", WORDS["sandbox"]
        else:
            self.sandbox, self.error = "unknown", WORDS["start"]
        if not inside:
            self.setup()
        return ok

    # -- playing --
    def play(self, serial, ids, start=0.0, volume=None, muted=None, service="apple"):
        """Play the first of `ids` (the ids of the song of `service`, "apple" or "spotify") that the
        service takes, from `start` seconds. The page hears how it goes. EngineError when the engine
        cannot be started at all. A running helper of the other service ends first."""
        if not self.installed():
            words = SPOTIFY_WORDS if service == "spotify" else WORDS
            raise EngineError(words["not-installed"], "not-installed")
        with self.lock:
            if volume is not None:
                self.volume = max(0.0, min(1.0, _finite(volume, self.volume)))
            if muted is not None:
                self.muted = bool(muted)
            self.song = {"serial": serial, "ids": list(ids), "position": max(0.0, _finite(start)),
                         "duration": 0.0, "phase": "starting", "sent": False, "service": service}
            self.quiet = time.monotonic()
            if self.proc is not None and self.proc.poll() is None and self.leaving is not self.proc:
                if self.service == service:
                    if self.ready:
                        self._send_play()
                        self.emit("engine", phase="loading", serial=serial)
                    else:   # it is starting: the song goes to it when it is ready
                        self.emit("engine", phase="starting", serial=serial)
                    return
                self._end()   # it serves the other service: it ends, and the launch waits for it
            self.emit("engine", phase="starting", serial=serial)
        threading.Thread(target=self._launch, args=(serial, service), daemon=True).start()

    def pause(self):
        with self.lock:
            if self.song and self.song["phase"] in ("playing", "loading"):
                self.song["phase"] = "paused"
            self.quiet = time.monotonic()
            self._send({"cmd": "pause"})

    def resume(self):
        """Go on with the paused song: the helper may have ended meanwhile, so it starts again at the place."""
        with self.lock:
            song = self.song
            if not song:
                return False
            if self.proc is not None and self.proc.poll() is None and self.leaving is not self.proc and self.ready:
                if song["sent"]:
                    song["phase"] = "playing"
                    self._send({"cmd": "resume"})
                else:   # it was paused before the helper was ready for it
                    self._send_play()
                    self.emit("engine", phase="loading", serial=song["serial"])
                return True
            ids, position, serial = list(song["ids"]), song["position"], song["serial"]
            service = song.get("service", "apple")
        self.play(serial, ids, position, service=service)
        return True

    def _send_play(self):
        """Hand the song asked for to the helper (the caller holds the lock)."""
        song = self.song
        self._send({"cmd": "volume", "value": self.volume, "muted": self.muted})
        song.update(phase="loading", sent=True)
        return self._send({"cmd": "play", "serial": song["serial"], "ids": song["ids"], "from": song["position"]})

    def seek(self, to):
        with self.lock:
            if self.song:
                self.song["position"] = max(0.0, _finite(to))
            self._send({"cmd": "seek", "to": max(0.0, _finite(to))})

    def set_volume(self, value, muted):
        with self.lock:
            self.volume, self.muted = max(0.0, min(1.0, _finite(value, self.volume))), bool(muted)
            self._send({"cmd": "volume", "value": self.volume, "muted": self.muted})

    def stop(self, final=False):
        """Stop the song. The helper goes on a while, in case another comes (`final`: it is asked to end now,
        and the helper that was asked, if there was one, is returned)."""
        with self.lock:
            self.song = None
            self.quiet = time.monotonic()
            if final:
                return self._end()
            self._send({"cmd": "stop"})
        return None

    def close(self):
        """The player is closing: end the helper and wait for it."""
        self.cancel_ask()
        proc = self._end()
        if proc is not None:
            self._reap(proc, 2)

    # -- the helper --
    def _words(self):
        """What the page is told, in the running helper's service's words."""
        return SPOTIFY_WORDS if self.service == "spotify" else WORDS

    def _send(self, message):
        proc = self.proc
        if proc is None or proc.poll() is not None or not self.ready and message.get("cmd") != "start":
            return False
        try:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def _end(self):
        """Ask the helper to end; the process, or None."""
        with self.lock:
            proc = self.proc
            if proc is None or proc.poll() is not None:
                return None
            self.leaving = proc
            try:
                proc.stdin.write('{"cmd":"quit"}\n')
                proc.stdin.flush()
            except (OSError, ValueError):
                pass
        threading.Thread(target=self._reap, args=(proc, 5), daemon=True).start()
        return proc

    @staticmethod
    def _reap(proc, patience):
        try:
            proc.wait(patience)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(2)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _launch(self, serial, service):
        """Start the helper and hand it the sign-in; its events carry on from there."""
        words = SPOTIFY_WORDS if service == "spotify" else WORDS
        try:
            token = self.token_source(service)
            line = start_line(service, token, self.volume, self.muted)
        except EngineError as error:
            self._broken(serial, error.code, str(error))
            return
        folder = os.path.join(self.root, "profile")
        old = self.leaving
        if old is not None:   # two Chromiums cannot share a folder: let the one that is ending end
            self._reap(old, 10)
        try:
            os.makedirs(folder, exist_ok=True)
            with self.lock:
                if self.song is None or self.song["serial"] != serial:
                    return   # something else was chosen meanwhile
                if self.proc is not None and self.proc.poll() is None and self.leaving is not self.proc:
                    return   # another try has started it; the song goes to it when it is ready
                self.leaving = None
                tail = collections.deque(maxlen=40)
                proc = self.popen([self.program, self.helper, f"--data={folder}"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=helper_env(), cwd=self.root,
                                  text=True, encoding="utf-8", errors="replace", bufsize=1)
                self.proc, self.service, self.ready, self.started, self.fail, self.tail = \
                    proc, service, False, False, None, tail
                proc.stdin.write(json.dumps(line) + "\n")
                proc.stdin.flush()
        except OSError:
            self._broken(serial, "start", words["start"])
            return
        finally:
            line = token = None
        reader = threading.Thread(target=self._read, args=(proc,), daemon=True)
        drain = threading.Thread(target=_drain, args=(proc.stderr, tail), daemon=True)
        reader.start()
        drain.start()
        threading.Thread(target=self._wait, args=(proc, reader, drain), daemon=True).start()
        timer = threading.Timer(READY_WITHIN, self._late, args=(proc,))
        timer.daemon = True
        timer.start()
        if self.watcher is None:
            self.watcher = threading.Thread(target=self._idle, daemon=True)
            self.watcher.start()

    def _late(self, proc):
        with self.lock:
            if proc is self.proc and not self.ready and proc.poll() is None:
                self.fail = "offline"
                proc.kill()

    def _read(self, proc):
        for line in proc.stdout:
            if not line.startswith("OMP "):
                continue
            try:
                event = json.loads(line[4:])
            except ValueError:
                continue
            if isinstance(event, dict):
                self._event(proc, event)

    def _event(self, proc, event):
        name = event.get("event")
        with self.lock:
            if proc is not self.proc or proc is self.leaving:   # a helper that was asked to end serves no song
                return
            song = self.song
            if name == "start":
                self.started = True
            elif name == "ready":
                self.ready, self.sandbox = True, "ok"
                if song and song["phase"] != "paused":   # what was asked for while it started
                    self._send_play()
                    self.emit("engine", phase="loading", serial=song["serial"])
                else:
                    self._send({"cmd": "volume", "value": self.volume, "muted": self.muted})
            elif name == "state" and song and event.get("serial") == song["serial"]:
                phase = event.get("state")
                if phase not in ("loading", "playing", "paused", "ended"):
                    return
                position, duration = _number(event.get("position")), _number(event.get("duration"))
                song.update(phase=phase, position=position, duration=duration or song["duration"])
                if phase in ("ended", "paused"):
                    self.quiet = time.monotonic()
                self.emit("engine", phase=phase, serial=song["serial"], position=position, duration=song["duration"])
            elif name == "problem" and song and event.get("serial") == song["serial"]:
                code = event.get("code") if event.get("code") in self._words() else "play"
                self.song = None
                self.quiet = time.monotonic()
                self.emit("engine", phase="error", serial=song["serial"], code=code, message=self._words()[code])
            elif name == "fail":
                self.fail = event.get("code") if event.get("code") in self._words() else "crash"
            elif name == "levels" and song and event.get("serial") == song["serial"] and song["phase"] == "playing":
                # the spectrum of what plays, for the visualizations: 384 bands, a byte each, as base64
                bands = event.get("b")
                if isinstance(bands, str) and LEVELS.fullmatch(bands):
                    self.emit("levels", serial=song["serial"], b=bands)

    def _wait(self, proc, reader, drain):
        proc.wait()
        reader.join(1)   # what it said last (a "fail", a sandbox line) may still be in its pipes; a child
        drain.join(1)    # that kept a pipe open is not waited for
        with self.lock:
            if proc is not self.proc:
                return
            service = self.service
            self.proc, self.service, self.ready = None, None, False
            song, fail, started = self.song, self.fail, self.started
            if self.leaving is proc:
                self.leaving = None
                return
            if not started and sandbox_failure(list(self.tail)):
                fail = "sandbox"
            fail = fail or "crash"
            if fail == "sandbox":
                self.sandbox = "blocked"
            self.song = None
        words = SPOTIFY_WORDS if service == "spotify" else WORDS
        if song:
            self.emit("engine", phase="error", serial=song["serial"], code=fail, message=words.get(fail, words["crash"]))
        if fail == "sandbox":
            self.setup()

    def _broken(self, serial, code, message):
        with self.lock:
            if self.song and self.song["serial"] == serial:
                self.song = None
        self.emit("engine", phase="error", serial=serial, code=code if code in WORDS else "start", message=message)

    def _idle(self):
        while True:
            time.sleep(IDLE_LOOK)
            with self.lock:
                proc = self.proc
                if proc is None or proc.poll() is not None or self.leaving is proc:
                    continue
                phase = self.song["phase"] if self.song else ""
                if phase in ("playing", "loading", "starting"):
                    continue
                if time.monotonic() - self.quiet >= (IDLE_PAUSED if phase == "paused" else IDLE_STOPPED):
                    self._end()


def _finite(value, default=0.0):
    """`value` as a finite number (a place or a volume that is not one becomes `default`)."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _number(value):
    return round(float(value), 1) if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 1e6 else 0.0


def _drain(stream, tail):
    """Keep the last lines a helper printed on its error output (Chromium is talkative)."""
    for line in stream:
        tail.append(line.rstrip()[:300])


def _no_sign_in(service=None):
    raise EngineError(WORDS["signed-out"], "signed-out")


def main(argv):
    """Say how the engine stands, or check it."""
    engine = Engine(_no_sign_in, lambda *args, **data: None)
    if argv[1:2] == ["check"]:
        ok = engine.check()
        print("the sandbox works" if ok else "the sandbox is not on: " + (engine.error or "the engine is not installed"))
        return 0 if ok else 1
    status = engine.status()
    print(f"engine {'installed' if status['installed'] else 'not installed'} (version {VERSION}) in {engine.root}")
    print(f"this system restricts user namespaces: {status['restricted']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
