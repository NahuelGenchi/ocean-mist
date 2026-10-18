"""The YouTube Music, Apple Music and Spotify sign-ins of your Firefox, for
Ocean Mist.

Ocean Mist has no sign-in of its own. You sign in to YouTube Music, to Apple
Music or to Spotify, in Firefox, on the service's own page, and when you ask
Ocean Mist for something of your account (your library, a like, a change to a
playlist), it reads that sign-in from Firefox's cookie file and uses it.

The sign-in is that of the Firefox profile you chose. When you sign in and
more than one profile has one, you choose among them, by the names Firefox's
profile menu shows; Ocean Mist then keeps to that profile.

The file is opened read-only and read straight into memory, with the changes
Firefox still keeps in its write-ahead log: no copy goes to disk, nothing is
written, and Firefox is not disturbed. Only the profile's YouTube cookies
are taken, or, for Apple Music, the one cookie its web player signs in with
(media-user-token), or, for Spotify, the cookies its web player signs in with
(sp_dc, and the companions it sends along), and they are let go once the
request is made. The next request reads them again, so they are always the
ones Firefox has now. Standard library only.

    python3 firefox.py      which profiles have a YouTube, Apple Music or
                            Spotify sign-in (no values)
"""
import configparser
import glob
import hashlib
import os
import re
import sqlite3
import struct
import time

# YouTube Music's own sign-in link: Google's page, then back to YouTube Music
SIGN_IN = ("https://accounts.google.com/ServiceLogin?ltmpl=music&service=youtube&uilel=3"
           "&passive=true&hl=en&continue=https%3A%2F%2Fwww.youtube.com%2Fsignin%3F"
           "action_handle_signin%3Dtrue%26app%3Ddesktop%26hl%3Den%26next%3D"
           "https%253A%252F%252Fmusic.youtube.com%252F")
# Firefox's desktop entry: Mozilla's and distribution packages, the Snap, the Flatpak, ESR
DESKTOP_ENTRIES = ("firefox.desktop", "firefox_firefox.desktop", "org.mozilla.firefox.desktop",
                   "firefox-esr.desktop")
# the cookies a browser sends to music.youtube.com
HOSTS = (".youtube.com", "youtube.com", "www.youtube.com", ".music.youtube.com", "music.youtube.com")
KEYS = ("SAPISID", "__Secure-3PAPISID")   # what a signed-in request is signed with
# Apple Music: its own site, whose Sign In button opens Apple's page; and the one
# cookie Apple's web player signs in with, which a browser sends to music.apple.com
APPLE_SIGN_IN = "https://music.apple.com/"
APPLE_HOSTS = (".music.apple.com", "music.apple.com")
APPLE_KEY = "media-user-token"
# Spotify: its own site, and the cookies its web player signs in with; a browser
# sends them to open.spotify.com. sp_dc is the session; the others come along.
SPOTIFY_SIGN_IN = "https://open.spotify.com/"
SPOTIFY_HOSTS = (".spotify.com", "spotify.com", "open.spotify.com")
SPOTIFY_KEYS = ("sp_dc", "sp_key", "sp_t")
SAFE = re.compile(r"[!#-+\--:<-\[\]-~]+\Z")   # what may go in a Cookie header, unquoted
WAL_MAGIC = (0x377F0682, 0x377F0683)
WAL_VERSION = 3007000
NEEDS_NEWER_PYTHON = "signing in needs Python 3.11 or newer"


def roots():
    """Where Firefox keeps its profiles: Mozilla's and distribution packages
    (the old place and the XDG one), the Snap and the Flatpak."""
    home = os.path.expanduser("~")
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return (os.path.join(home, ".mozilla", "firefox"),
            os.path.join(config, "mozilla", "firefox"),
            os.path.join(home, "snap", "firefox", "common", ".mozilla", "firefox"),
            os.path.join(home, ".var", "app", "org.mozilla.firefox", ".mozilla", "firefox"),
            os.path.join(home, ".var", "app", "org.mozilla.firefox", "config", "mozilla", "firefox"))


def _listing(root):
    """A root's profiles.ini: {folder: its name there}."""
    listing = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        listing.read(os.path.join(root, "profiles.ini"), encoding="utf-8")
    except (configparser.Error, UnicodeDecodeError):
        pass
    found = {}
    for section in listing.sections():
        path = listing.get(section, "Path", fallback="")
        if path:
            relative = listing.get(section, "IsRelative", fallback="1") != "0"
            found[os.path.realpath(os.path.join(root, path) if relative else path)] = listing.get(
                section, "Name", fallback="")
    return found


def profiles():
    """The profile folders that have a cookie file, each once: those in the
    usual places, and those a profiles.ini lists, wherever they are."""
    found = []
    for root in roots():
        folders = glob.glob(os.path.join(glob.escape(root), "*", "")) + list(_listing(root))
        for folder in folders:
            folder = os.path.realpath(folder)
            if folder not in found and os.path.isfile(os.path.join(folder, "cookies.sqlite")):
                found.append(folder)
    return found


# ---- reading a database Firefox has open ----

def _checksum(data, s0, s1, big):
    """SQLite's running checksum over its write-ahead log."""
    words = struct.unpack((">" if big else "<") + "%dI" % (len(data) // 4), data)
    for i in range(0, len(words), 2):
        s0 = (s0 + words[i] + s1) & 0xFFFFFFFF
        s1 = (s1 + words[i + 1] + s0) & 0xFFFFFFFF
    return s0, s1


def replay(db, wal):
    """The database as SQLite sees it: the file, with the committed frames of
    its write-ahead log put in, the way SQLite recovers a log. Frames after
    the last commit, torn ones and those of an older log are left out."""
    if len(wal) < 32:
        return db
    magic, version, size, _, salt1, salt2, sum1, sum2 = struct.unpack(">8I", wal[:32])
    if magic not in WAL_MAGIC or version != WAL_VERSION or not 512 <= size <= 65536 or size & (size - 1):
        return db
    big = magic & 1
    s0, s1 = _checksum(wal[:24], 0, 0, big)
    if (s0, s1) != (sum1, sum2):
        return db
    frames, committed, pages = [], 0, 0
    step = 24 + size
    for at in range(32, len(wal) - step + 1, step):
        number, commit, frame_salt1, frame_salt2, check1, check2 = struct.unpack(">6I", wal[at:at + 24])
        if (frame_salt1, frame_salt2) != (salt1, salt2):
            break   # left over from an older log
        s0, s1 = _checksum(wal[at:at + 8], s0, s1, big)
        s0, s1 = _checksum(wal[at + 24:at + step], s0, s1, big)
        if (s0, s1) != (check1, check2):
            break   # torn: written only in part
        frames.append((number, at + 24))
        if commit:
            committed, pages = len(frames), commit
    if not committed:
        return db
    image = bytearray(db[:pages * size].ljust(pages * size, b"\0"))
    for number, at in frames[:committed]:
        if 0 < number <= pages:
            image[(number - 1) * size:number * size] = wal[at:at + size]
    return bytes(image)


def _contents(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return b""


def _snapshot(path, table):
    """A database Firefox has open, as an sqlite3 connection to a copy that
    lives in memory only; None when there is none to read, or it has no
    `table`."""
    if not hasattr(sqlite3.Connection, "deserialize"):
        raise RuntimeError(NEEDS_NEWER_PYTHON)
    for _ in range(3):
        start = _contents(path + "-wal")[:32]
        db, wal = _contents(path), _contents(path + "-wal")
        if not db and not wal:
            return None
        if wal[:32] != start:
            continue   # Firefox started its log over while it was read: read again
        image = bytearray(replay(db, wal))
        if len(image) < 100:
            return None
        image[18:20] = b"\x01\x01"   # a plain journal, so the copy looks for no log of its own
        con = sqlite3.connect(":memory:")
        try:
            con.deserialize(bytes(image))
            con.execute(f'SELECT count(*) FROM "{table}"').fetchone()   # reads all of the table
            return con
        except sqlite3.DatabaseError:   # caught in the middle of a write: read again
            con.close()
    return None


def open_cookies(folder):
    """A profile's cookie database, in memory only (see _snapshot)."""
    return _snapshot(os.path.join(folder, "cookies.sqlite"), "moz_cookies")


def names():
    """What each profile is called, as Firefox's profile menu shows it:
    {folder: name}. The names of a profile group come from the group's own
    database; profiles.ini then lists only one profile of the group."""
    found = {}
    for root in roots():
        found.update({folder: name for folder, name in _listing(root).items() if name})
        for group in sorted(glob.glob(os.path.join(glob.escape(root), "Profile Groups", "*.sqlite"))):
            try:
                con = _snapshot(group, "Profiles")
            except (OSError, RuntimeError):   # names only label the profiles: go on without
                con = None
            if con is None:
                continue
            try:
                for path, name in con.execute("SELECT path, name FROM Profiles"):
                    if isinstance(path, str) and isinstance(name, str) and name.strip():
                        found[os.path.realpath(os.path.join(root, path))] = name.strip()
            except sqlite3.DatabaseError:
                pass
            finally:
                con.close()
    return found


def name_of(folder):
    """A profile's name as Firefox shows it, or its folder's when it has none."""
    folder = os.path.realpath(folder)
    return names().get(folder) or os.path.basename(folder).split(".", 1)[-1]


# ---- the sign-in ----

class Session:
    """A YouTube sign-in as Firefox has it now: what a signed-in request needs."""

    def __init__(self, cookies, agent):
        self.cookies = cookies   # [(name, value)], in the order a browser sends them
        self.agent = agent       # the user agent of the Firefox that signed in

    def headers(self, origin):
        """The headers that sign a request to `origin`, as the site signs its own."""
        jar = dict(reversed(self.cookies))
        key = next(jar[name] for name in KEYS if name in jar)
        stamp = str(int(time.time()))
        digest = hashlib.sha1(f"{stamp} {key} {origin}".encode()).hexdigest()
        return {"Cookie": "; ".join(f"{name}={value}" for name, value in self.cookies),
                "Authorization": f"SAPISIDHASH {stamp}_{digest}",
                "X-Goog-AuthUser": "0", "X-Origin": origin, "User-Agent": self.agent}

    def __repr__(self):
        return "<YouTube sign-in>"   # never the cookies, in a log or a traceback


def _agent(folder):
    """The user agent of the Firefox version that last opened the profile."""
    version = ""
    try:
        with open(os.path.join(folder, "compatibility.ini"), encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("LastVersion="):
                    version = line.split("=", 1)[1]
    except (OSError, UnicodeDecodeError):
        pass
    major = re.match(r"\d+", version)
    major = major.group(0) if major else "140"
    return f"Mozilla/5.0 (X11; Linux x86_64; rv:{major}.0) Gecko/20100101 Firefox/{major}.0"


def _signed_in(folder, now):
    """A profile's YouTube sign-in: (when it was last used, its cookies), or None."""
    con = open_cookies(folder)
    if con is None:
        return None
    marks = ",".join("?" * len(HOSTS))
    try:   # not those of containers, private windows or partitioned (third-party) storage
        rows = con.execute(
            "SELECT name, value, expiry, lastAccessed FROM moz_cookies"
            f" WHERE host IN ({marks}) AND originAttributes = '' AND path = '/'"
            " ORDER BY length(host) DESC, creationTime", HOSTS).fetchall()
    except sqlite3.DatabaseError:
        return None
    finally:
        con.close()
    cookies, used = [], 0
    for name, value, expiry, last in rows:
        expiry = expiry / 1000 if expiry > 1e11 else expiry   # seconds, or milliseconds in newer versions
        if expiry <= now or not SAFE.match(str(name)) or not SAFE.match(str(value)):
            continue
        cookies.append((name, value))
        if name in KEYS:
            used = max(used, last)
    return (used, cookies) if used else None


def _ranked(found):
    """[(when it was last used, folder)] as [(folder, name)], the most
    recently used first."""
    called = names() if found else {}
    return [(folder, called.get(folder) or os.path.basename(folder).split(".", 1)[-1])
            for _, folder in sorted(found, reverse=True)]


def signed_in():
    """The profiles that have a YouTube sign-in, the most recently used
    first: [(folder, name)]. No cookie is kept."""
    now, found = time.time(), []
    for folder in profiles():
        sign = _signed_in(folder, now)
        if sign:
            found.append((sign[0], folder))
    return _ranked(found)


def session(folder):
    """The YouTube sign-in of one of Firefox's profiles (a folder that
    profiles() lists) as Firefox has it now, or None. Read now, each time:
    keep it only as long as a request needs it."""
    folder = os.path.realpath(folder)
    found = _signed_in(folder, time.time()) if folder in profiles() else None
    return Session(found[1], _agent(folder)) if found else None


class AppleToken:
    """An Apple Music sign-in as Firefox has it now: the one cookie Apple's
    web player signs in with, and how Firefox keeps it."""

    def __init__(self, value, host, expires, secure, http_only, same_site):
        self.value, self.host, self.expires = value, host, expires
        self.secure, self.http_only, self.same_site = secure, http_only, same_site

    def __repr__(self):
        return "<Apple Music sign-in>"   # never the cookie, in a log or a traceback


def _apple_cookie(folder, now):
    """A profile's Apple Music sign-in: (when it was last used, AppleToken), or None."""
    con = open_cookies(folder)
    if con is None:
        return None
    marks = ",".join("?" * len(APPLE_HOSTS))
    try:   # not those of containers, private windows or partitioned (third-party) storage
        rows = con.execute(
            "SELECT value, host, expiry, lastAccessed, isSecure, isHttpOnly, sameSite FROM moz_cookies"
            f" WHERE name = ? AND host IN ({marks}) AND originAttributes = '' AND path = '/'"
            " ORDER BY length(host) DESC, creationTime DESC", (APPLE_KEY, *APPLE_HOSTS)).fetchall()
    except sqlite3.DatabaseError:
        return None
    finally:
        con.close()
    for value, host, expiry, last, secure, http_only, same_site in rows:
        expiry = expiry / 1000 if expiry > 1e11 else expiry   # seconds, or milliseconds in newer versions
        if (expiry and expiry <= now) or not SAFE.match(str(value)):
            continue
        return last, AppleToken(value, host, expiry, bool(secure), bool(http_only), same_site)
    return None


def apple_signed_in():
    """The profiles that have an Apple Music sign-in, the most recently used
    first: [(folder, name)]. No cookie is kept."""
    now, found = time.time(), []
    for folder in profiles():
        sign = _apple_cookie(folder, now)
        if sign:
            found.append((sign[0], folder))
    return _ranked(found)


def apple_token(folder):
    """The Apple Music sign-in of one of Firefox's profiles (a folder that
    profiles() lists) as Firefox has it now, or None. Read now, each time:
    keep it only as long as it is needed."""
    folder = os.path.realpath(folder)
    found = _apple_cookie(folder, time.time()) if folder in profiles() else None
    return found[1] if found else None


class SpotifyCookie:
    """One cookie of a Spotify sign-in, as Firefox keeps it."""

    def __init__(self, name, value, host, expires, secure, http_only, same_site):
        self.name, self.value, self.host, self.expires = name, value, host, expires
        self.secure, self.http_only, self.same_site = secure, http_only, same_site

    def __repr__(self):
        return f"<Spotify cookie {self.name}>"   # never the value, in a log or a traceback


class SpotifyToken:
    """A Spotify sign-in as Firefox has it now: the cookies its web player
    signs in with (sp_dc first), and how Firefox keeps them."""

    def __init__(self, cookies):
        self.cookies = cookies   # [SpotifyCookie]

    def __repr__(self):
        return "<Spotify sign-in>"   # never the cookies, in a log or a traceback


def _spotify_cookies(folder, now):
    """A profile's Spotify sign-in: (when it was last used, SpotifyToken), or None."""
    con = open_cookies(folder)
    if con is None:
        return None
    marks = ",".join("?" * len(SPOTIFY_HOSTS))
    names = ",".join("?" * len(SPOTIFY_KEYS))
    try:   # not those of containers, private windows or partitioned (third-party) storage
        rows = con.execute(
            "SELECT name, value, host, expiry, lastAccessed, isSecure, isHttpOnly, sameSite FROM moz_cookies"
            f" WHERE name IN ({names}) AND host IN ({marks}) AND originAttributes = '' AND path = '/'"
            " ORDER BY length(host) DESC, creationTime DESC", (*SPOTIFY_KEYS, *SPOTIFY_HOSTS)).fetchall()
    except sqlite3.DatabaseError:
        return None
    finally:
        con.close()
    found, used = {}, 0
    for name, value, host, expiry, last, secure, http_only, same_site in rows:
        expiry = expiry / 1000 if expiry > 1e11 else expiry   # seconds, or milliseconds in newer versions
        if (expiry and expiry <= now) or name in found or not SAFE.match(str(value)):
            continue
        found[name] = SpotifyCookie(name, value, host, expiry, bool(secure), bool(http_only), same_site)
        used = max(used, last)
    if "sp_dc" not in found or not used:
        return None
    return used, SpotifyToken([found[name] for name in SPOTIFY_KEYS if name in found])


def spotify_signed_in():
    """The profiles that have a Spotify sign-in, the most recently used
    first: [(folder, name)]. No cookie is kept."""
    now, found = time.time(), []
    for folder in profiles():
        sign = _spotify_cookies(folder, now)
        if sign:
            found.append((sign[0], folder))
    return _ranked(found)


def spotify_token(folder):
    """The Spotify sign-in of one of Firefox's profiles (a folder that
    profiles() lists) as Firefox has it now, or None. Read now, each time:
    keep it only as long as it is needed."""
    folder = os.path.realpath(folder)
    found = _spotify_cookies(folder, time.time()) if folder in profiles() else None
    return found[1] if found else None


if __name__ == "__main__":
    now = time.time()
    folders, called = profiles(), names()

    def ago(last):
        return f"last used {max(0, int((now - last / 1e6) / 60))} min ago"

    for folder in folders:
        youtube, apple, spotify = _signed_in(folder, now), _apple_cookie(folder, now), _spotify_cookies(folder, now)
        where = os.path.relpath(folder, os.path.expanduser("~"))
        print(f"{where} ({called.get(folder) or 'no name'}): "
              + (f"signed in to YouTube, {ago(youtube[0])}" if youtube else "no YouTube sign-in") + "; "
              + (f"signed in to Apple Music, {ago(apple[0])}" if apple else "no Apple Music sign-in") + "; "
              + (f"signed in to Spotify, {ago(spotify[0])}" if spotify else "no Spotify sign-in"))
    if not folders:
        print("no Firefox profile found")
