"""What the player knows: the two Now Playing lists, the media library, saved
playlists, radio stations and the settings, kept in state.json; the files
themselves are only ever read.

My Music holds files. YouTube Music holds YouTube Music songs, whose "path" is
their address on music.youtube.com, and the songs of Apple Music, which play
from there (apple.py): their "path" is an Apple Music address, and "video" stays
empty, as the YouTube Music song that plays one is found when its turn comes.
That list lives in memory only: nothing from streaming is written to disk. So
it starts empty each time, a song in it is never the resume point, and its
covers wait in the runtime folder, which is not on disk, until the player
closes."""
import hashlib
import json
import os
import queue
import re
import tempfile
import threading
import time
import urllib.parse

import apple
import tags
import ytmusic

APP_ID = os.environ.get("OCEAN_MIST_APP_ID", "io.github.nahuelgenchi.OceanMist")   # tests run beside the real player
CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "ocean-mist",
)
CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "ocean-mist",
)
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")
TAG_CACHE = os.path.join(CACHE_DIR, "tags.json")
# YouTube Music covers, as files for the desktop's media controls; named after
# the app id, so a test instance never clears the covers of the real player
STREAM_COVERS = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(), APP_ID)
LISTS = ("local", "youtube")   # the Now Playing lists: My Music, and YouTube Music
AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".wav", ".wma",
}
PLAYLIST_EXTENSIONS = {".m3u", ".m3u8", ".pls"}
COVER_NAMES = ("cover", "folder", "front", "album", "albumart")

# settings the page may change; the rest (window size, zoom, folders) are the host's
PAGE_SETTINGS = ("volume", "muted", "vis", "shuffle", "repeat", "hue",
                 "eq", "eq_on", "eq_preset", "eq_open", "notify", "yt_radio")
STREAM_FIELDS = ("title", "artist", "album", "year", "duration", "cover", "artist_id", "album_id")
DEFAULT_STATIONS = (
    ("SomaFM: Groove Salad", "https://somafm.com/groovesalad.pls"),
    ("SomaFM: Drone Zone", "https://somafm.com/dronezone.pls"),
    ("SomaFM: Secret Agent", "https://somafm.com/secretagent.pls"),
    ("SomaFM: Indie Pop Rocks!", "https://somafm.com/indiepop.pls"),
)


def music_dir():
    try:
        from gi.repository import GLib
        found = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_MUSIC)
    except Exception:
        found = None
    return found or os.path.expanduser("~/Music")


def _natural(name):
    """Sort key in file-manager order: "2 - b" comes before "10 - a"."""
    return [int(part) if index % 2 else part.casefold()
            for index, part in enumerate(re.split(r"([0-9]+)", name))]


def _is_audio(name):
    return os.path.splitext(name)[1].lower() in AUDIO_EXTENSIONS


def is_playlist(name):
    return os.path.splitext(name)[1].lower() in PLAYLIST_EXTENSIONS


def list_for(track):
    """The Now Playing list a track belongs in."""
    return "youtube" if track.get("source") else "local"


def stream_key(track):
    """What names a streamed song in a request of the page: "apple:" and its
    Apple Music id, or its video id."""
    return "apple:" + track["apple"] if track.get("apple") else track.get("video")


def folder_songs(folder):
    """The songs directly inside a folder, in file-manager order."""
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return [os.path.join(folder, name) for name in sorted(names, key=_natural)
            if _is_audio(name) and not name.startswith(".")
            and os.path.isfile(os.path.join(folder, name))]


def folder_cover(folder):
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    for name in names:
        stem, ext = os.path.splitext(name.lower())
        if stem in COVER_NAMES and ext in (".jpg", ".jpeg", ".png", ".webp"):
            return os.path.join(folder, name)
    return None


def read_playlist(path, labels=None):
    """The entries of an .m3u/.m3u8/.pls file: file paths and stream addresses.
    `labels`, a dict, gets each entry's (seconds, "artist - title") when an
    #EXTINF line gives them."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(4 << 20)
    except OSError:
        return []
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError:
        text = raw.decode("latin-1")
    folder = os.path.dirname(os.path.abspath(path))
    pls = path.lower().endswith(".pls")
    entries, label = [], None
    for line in text.splitlines():
        line = line.strip()
        if pls:
            key, _, line = line.partition("=")
            if not key.strip().lower().startswith("file"):
                continue
            line = line.strip()
        if line.upper().startswith("#EXTINF:"):
            seconds, _, name = line[8:].partition(",")
            try:
                label = (max(0.0, float(seconds.strip())), name.strip())
            except ValueError:
                label = (0.0, name.strip())
        if not line or line.startswith("#"):
            continue
        if re.match(r"[a-z][a-z0-9+.-]*://", line, re.I):
            if not line.lower().startswith("file://"):
                entries.append(line)
                if label and labels is not None:
                    labels[line] = label
                label = None
                continue
            line = urllib.parse.unquote(urllib.parse.urlparse(line).path)
        target = os.path.normpath(os.path.join(folder, os.path.expanduser(line)))
        if not os.path.exists(target) and "\\" in line:   # written on Windows
            target = os.path.normpath(os.path.join(folder, line.replace("\\", "/")))
        entries.append(target)
        label = None
    return entries


def write_playlist(path, tracks):
    folder = os.path.dirname(os.path.abspath(path))
    lines = ["#EXTM3U"]
    for track in tracks:
        label = "" if track.get("bare") else track["title"]   # a song not looked up yet has no title
        if track["artist"] and label:
            label = f'{track["artist"]} - {label}'
        label = " ".join(label.split())   # one line, whatever the tags hold
        seconds = int(round(track["duration"])) if track["duration"] else -1
        lines.append(f"#EXTINF:{seconds},{label}")
        if track.get("source"):   # a YouTube Music song: its address
            lines.append(track["path"])
            continue
        inside = os.path.relpath(track["path"], folder)
        lines.append(track["path"] if inside.startswith("..") else inside)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def _write_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except OSError:
        pass


class Library:
    def __init__(self):
        self.lock = threading.RLock()
        self.by_id, self.by_path = {}, {}   # every track met this session
        self.lists = {which: [] for which in LISTS}   # the Now Playing lists, in order
        self.names = {which: "" for which in LISTS}   # the saved playlist (or page) each came from
        self.collection = []      # what the library folders hold
        self.scanning = False
        self.art = {}             # track id -> cover file path, True if embedded
        self.stations = []        # {"id", "name", "url"}
        self.settings = {
            "volume": 0.5, "muted": False, "vis": 0, "shuffle": False,
            "repeat": False, "hue": 0, "eq": [0] * 10, "eq_on": False,
            "eq_preset": "Flat", "eq_open": False, "notify": False,
            "yt_radio": True, "zoom": 1, "mini": False,
            "yt_account": False,   # the Firefox profile you signed in to YouTube Music with (only which one)
            "apple_account": False,   # ...and to Apple Music
        }
        self.listeners = set()
        self.pending_play = None  # what to start once the page is listening
        self.now = {"id": None, "station": None, "mode": "stopped",
                    "position": 0.0, "duration": 0.0, "title": "", "at": 0.0, "started": False}
        self.on_now = None        # called (from any thread) when it changes
        self.resume = None        # {"path", "position"} or {"station"}
        self._resume_offer = None
        self._resume_saved = 0.0
        self._next_id = 1
        self._untagged = queue.PriorityQueue()   # (urgency, order, track id)
        self._order = 0
        self._tagged = set()
        self._reader = None
        self._covers = {}         # path -> (mime, bytes) of the last few shown
        self._folder_covers = {}
        self._cache, self._cache_dirty = {}, False
        self._rescan = False
        self._found_lists = []    # playlist files inside the library folders
        self._remembered = []     # ...and ones saved or opened elsewhere
        self._playlist_lists = {}  # playlist path -> ((mtime, size), the list its songs go to)

    # ---- persistence ----
    def load(self):
        try:
            with open(TAG_CACHE, encoding="utf-8") as fh:
                self._cache = json.load(fh).get("files", {})
        except (OSError, ValueError, AttributeError):
            self._cache = {}
        try:
            with open(STATE_FILE, encoding="utf-8") as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            saved = {}
        self.settings.update(saved.get("settings", {}))
        stations = saved.get("stations")
        if stations is None:
            stations = [{"id": n + 1, "name": name, "url": url}
                        for n, (name, url) in enumerate(DEFAULT_STATIONS)]
        self.stations = [s for s in stations if s.get("url")]
        self._remembered = [p for p in saved.get("playlists", []) if isinstance(p, str)]
        self.names["local"] = saved.get("list", "")
        dropped = self._restore(saved.get("playlist", []))
        resume = saved.get("resume")
        if not isinstance(resume, dict):
            resume = None
        elif str(resume.get("path", "")).startswith(ytmusic.WATCH):
            resume, dropped = None, True
        self.resume = self._resume_offer = resume
        # the previous version kept YouTube Music in the list, the resume
        # point and the cache: none of it stays on disk
        self.forget_stream_covers(os.path.join(CACHE_DIR, "covers"))
        if dropped:
            self.save()

    def _restore(self, entries):
        """The saved My Music list. Returns whether YouTube Music songs were
        left out: the previous version kept them in the same list."""
        with self.lock:
            chosen, listed, dropped = [], set(), False
            for entry in entries if isinstance(entries, list) else []:
                if isinstance(entry, dict):   # {"yt": video id, ...}
                    dropped = True
                elif isinstance(entry, str) and _is_audio(entry) and os.path.isfile(entry):
                    track = self._entry(os.path.abspath(entry))
                    if track["id"] not in listed:
                        listed.add(track["id"])
                        chosen.append(track)
            self.lists["local"] = chosen
            if dropped and not chosen:
                self.names["local"] = ""   # named after a YouTube Music page
        return dropped

    def save(self):
        with self.lock:   # written inside it too, so two saves cannot mix in the file
            data = {"settings": dict(self.settings),
                    "playlist": [t["path"] for t in self.lists["local"]],   # never the YouTube Music list
                    "list": self.names["local"], "stations": list(self.stations),
                    "playlists": list(self._remembered), "resume": self.resume}
            _write_json(STATE_FILE, data)

    def _save_cache(self):
        with self.lock:
            if not self._cache_dirty:
                return
            files = {p: v for p, v in self._cache.items() if p in self.by_path}
            self._cache_dirty = False
        _write_json(TAG_CACHE, {"version": 1, "files": files})

    # ---- events to the page ----
    def subscribe(self):
        box = queue.Queue()
        with self.lock:
            self.listeners.add(box)
        return box

    def unsubscribe(self, box):
        with self.lock:
            self.listeners.discard(box)

    def emit(self, event, **payload):
        payload["event"] = event
        with self.lock:
            targets = list(self.listeners)
        for box in targets:
            box.put(payload)

    def request_play(self, target):
        """Start a track (its id) or a station ({"station": id})."""
        payload = target if isinstance(target, dict) else {"id": target}
        with self.lock:
            self._resume_offer = None
            listening = bool(self.listeners)
            if not listening:
                self.pending_play = payload
        if listening:
            self.emit("play", **payload)

    def snapshot(self):
        with self.lock:
            pending, self.pending_play = self.pending_play, None
            offer, self._resume_offer = self._resume_offer, None
            resume = None
            if pending is None and offer:
                track = self.by_path.get(offer.get("path"))
                if track and track in self.lists["local"]:
                    resume = {"id": track["id"], "position": offer.get("position") or 0}
                elif any(s["id"] == offer.get("station") for s in self.stations):
                    resume = {"station": offer["station"]}
            return {"lists": {which: {"tracks": [self._public(t) for t in self.lists[which]],
                                      "name": self.names[which]} for which in LISTS},
                    "settings": dict(self.settings), "play": pending,
                    "resume": resume, "stations": list(self.stations),
                    "playlists": self.playlist_files(),
                    "folders": self.folders()}

    def library_snapshot(self):
        with self.lock:
            return {"tracks": [self._public(t) for t in self.collection],
                    "scanning": self.scanning}

    @staticmethod
    def _public(track):
        if track.get("source"):
            return {k: v for k, v in track.items() if k != "path"} | {"file": "", "folder": ""}
        return {k: v for k, v in track.items() if k != "path"} | {
            "file": os.path.basename(track["path"]),
            "folder": os.path.dirname(track["path"]),
        }

    # ---- tracks ----
    def _entry(self, path, urgent=True):
        """The track for a file, made on first sight (caller holds the lock)."""
        track = self.by_path.get(path)
        if track is None:
            track = {
                "id": self._next_id, "path": path,
                "title": os.path.splitext(os.path.basename(path))[0],
                "artist": "", "album": "", "year": "", "genre": "",
                "track": 0, "duration": 0, "art": False,
            }
            self._next_id += 1
            self.by_id[track["id"]] = self.by_path[path] = track
            self._order += 1
            self._untagged.put((0 if urgent else 1, self._order, track["id"]))
            if self._reader is None:
                self._reader = threading.Thread(target=self._read_tags, daemon=True)
                self._reader.start()
        return track

    def _stream(self, item):
        """The track for a YouTube Music song, or for an Apple Music one
        (`item["apple"]`), made on first sight, with what a search or a page
        said about it (caller holds the lock)."""
        ident = str(item.get("apple") or "")
        if ident:   # found on YouTube Music when its turn comes, so it has no video yet
            video, path = "", apple.SONG_URL + ident
            if not apple.ITEM_ID.match(ident):
                return None
        else:
            video = str(item.get("video") or "")
            if not ytmusic.VIDEO_ID.match(video):
                return None
            path = ytmusic.WATCH + video
        track = self.by_path.get(path)
        if track is None:
            if ident and not str(item.get("title") or "").strip():
                return None   # it is looked up by its title
            track = {"id": self._next_id, "path": path, "source": "youtube", "video": video,
                     "title": video, "artist": "", "album": "", "year": "", "genre": "",
                     "track": 0, "duration": 0, "art": False, "cover": "",
                     "artist_id": "", "album_id": "", "bare": True}
            if ident:
                track["apple"] = ident
            self._next_id += 1
            self.by_id[track["id"]] = self.by_path[path] = track
            self._tagged.add(track["id"])   # nothing to read from a file
        for key in STREAM_FIELDS:
            value = item.get(key)
            if key == "duration":
                try:
                    value = round(min(86400.0, max(0.0, float(value or 0))), 2)
                except (TypeError, ValueError):
                    value = 0
            else:
                value = str(value or "").strip()[:300]
                if key == "cover" and not (apple.PICTURE_HOSTS if ident else ytmusic.PICTURE_HOSTS).match(value):
                    value = ""
                elif key in ("artist_id", "album_id") and not ytmusic.BROWSE_ID.match(value):
                    value = ""
            if value:
                track[key] = value
        if str(item.get("title") or "").strip():
            track.pop("bare", None)
        track["art"] = bool(track["cover"])
        return track

    def add_streams(self, items, replace=False, name=""):
        """Put YouTube Music songs, and Apple Music ones, in their list: after
        what is there, or as the whole list (named `name`). Returns their tracks."""
        with self.lock:
            chosen, listed = [], set()
            for item in items[:ytmusic.PLAYLIST_LIMIT]:
                track = self._stream(item) if isinstance(item, dict) else None
                if track and track["id"] not in listed:
                    listed.add(track["id"])
                    chosen.append(track)
            if not chosen:
                return []
            if replace:
                self.lists["youtube"], self.names["youtube"] = chosen, str(name or "")[:200]
            else:
                listed = {t["id"] for t in self.lists["youtube"]}
                self.lists["youtube"] += [t for t in chosen if t["id"] not in listed]
        self._changed("youtube")
        return chosen

    def stream_found(self, video, details):
        """What YouTube said about a song while getting it ready, for its
        track if that knew less (it came from a bare address)."""
        self._learned(ytmusic.WATCH + str(video), details)

    def apple_found(self, ident, details):
        """The same for the YouTube Music song that plays an Apple Music one:
        the Apple Music song keeps what Apple said about it."""
        self._learned(apple.SONG_URL + str(ident), details)

    def apple_track(self, ident):
        """A copy of the track of an Apple Music song of the player's list, or None."""
        with self.lock:
            track = self.by_path.get(apple.SONG_URL + str(ident))
            return dict(track) if track else None

    def _learned(self, path, details):
        changed = False
        with self.lock:
            track = self.by_path.get(path)
            if track is None:
                return
            for key in ("title", "artist", "duration", "cover"):
                if details.get(key) and (not track.get(key) or track.get("bare")):
                    changed = changed or track.get(key) != details[key]
                    track[key] = details[key]
            if track.pop("bare", None):
                changed = True
            track["art"] = bool(track["cover"])
        if changed:
            self._changed("youtube")

    def track(self, track_id):
        with self.lock:
            return self.by_id.get(track_id)

    def list_of(self, track_id):
        """The Now Playing list a track is in ("local" or "youtube"), or None."""
        with self.lock:
            return next((which for which in LISTS
                         if any(t["id"] == track_id for t in self.lists[which])), None)

    def _changed(self, *which):
        """After a change to these lists: My Music is written down (the
        YouTube Music list never is), and the page is told."""
        if "local" in which:
            self.save()
        self.emit("playlist")

    @staticmethod
    def _expand(paths):
        files = []
        for path in paths:
            path = os.path.abspath(os.path.expanduser(path))
            if os.path.isdir(path):
                for root, dirs, names in os.walk(path):
                    dirs[:] = sorted((d for d in dirs if not d.startswith(".")), key=_natural)
                    files += [os.path.join(root, name) for name in sorted(names, key=_natural)
                              if _is_audio(name) and not name.startswith(".")]
            elif os.path.isfile(path) and _is_audio(path):
                files.append(path)
        return list(dict.fromkeys(files))

    def add_paths(self, paths, announce=True, replace=False):
        """Add files to My Music (folders are scanned); with replace they
        become the whole list. Returns the id of the first of them."""
        files = self._expand(paths)
        if not files:
            return None
        with self.lock:
            chosen = [self._entry(path) for path in files]
            if replace:
                self.lists["local"], self.names["local"] = chosen, ""
            else:
                listed = {t["id"] for t in self.lists["local"]}
                self.lists["local"] += [t for t in chosen if t["id"] not in listed]
        if announce:
            self._changed("local")
        return chosen[0]["id"]

    def open_paths(self, paths):
        """Open songs the way a file manager expects: they become My Music,
        and a single song brings the rest of its folder along as the queue.
        A playlist file is loaded instead. Returns what to play."""
        target = None
        if len(paths) == 1:
            only = os.path.abspath(os.path.expanduser(paths[0]))
            if os.path.isfile(only) and is_playlist(only):
                return self.open_playlist(only)
            if os.path.isfile(only) and _is_audio(only):
                target = only
                paths = folder_songs(os.path.dirname(only))
                if only not in paths:
                    paths = [only]
        first = self.add_paths(paths, announce=False, replace=True)
        if first is None:
            return None
        if target is not None:
            with self.lock:
                first = self.by_path[target]["id"]
        self._tag(first)  # the song about to play shows its real title at once
        self._changed("local")
        return first

    def play_set(self, ids, start=None):
        """Make these tracks (from the Media Library) the list they belong
        in; the song to start with decides which. Returns its id."""
        with self.lock:
            chosen = [self.by_id[i] for i in dict.fromkeys(ids) if i in self.by_id]
            if not chosen:
                return None
            first = next((t for t in chosen if t["id"] == start), chosen[0])
            which = list_for(first)
            self.lists[which] = [t for t in chosen if list_for(t) == which]
            self.names[which] = ""
        self._changed(which)
        return first["id"]

    def add_ids(self, ids):
        """Add tracks to the end of the lists they belong in."""
        with self.lock:
            listed = {t["id"] for which in LISTS for t in self.lists[which]}
            touched = set()
            for track_id in dict.fromkeys(ids):
                track = self.by_id.get(track_id)
                if track is not None and track_id not in listed:
                    listed.add(track_id)
                    touched.add(list_for(track))
                    self.lists[list_for(track)].append(track)
        self._changed(*touched)

    def move(self, ids, before):
        """Move tracks in front of another one (or to the end of their list)."""
        with self.lock:
            which = next((w for w in LISTS if any(t["id"] in ids for t in self.lists[w])), None)
            if which is None:
                return
            moving = [t for t in self.lists[which] if t["id"] in ids]
            rest = [t for t in self.lists[which] if t["id"] not in ids]
            at = next((n for n, t in enumerate(rest) if t["id"] == before), len(rest))
            self.lists[which] = rest[:at] + moving + rest[at:]
        self._changed(which)

    def remove(self, ids):
        with self.lock:
            touched = [w for w in LISTS if any(t["id"] in ids for t in self.lists[w])]
            for which in touched:
                self.lists[which] = [t for t in self.lists[which] if t["id"] not in ids]
        self._changed(*touched)

    def clear(self, which="local"):
        which = which if which in LISTS else "local"
        with self.lock:
            self.lists[which], self.names[which] = [], ""
        self._changed(which)

    # ---- titles and covers ----
    # read in the background, so a big folder never holds up the song that
    # was asked for; what was read is remembered until the file changes
    def _tags_for(self, path):
        try:
            info = os.stat(path)
        except OSError:
            return {}
        with self.lock:
            hit = self._cache.get(path)
        if hit and hit[0] == info.st_mtime_ns and hit[1] == info.st_size:
            return hit[2]
        found = tags.read(path)
        with self.lock:
            self._cache[path] = [info.st_mtime_ns, info.st_size, found]
            self._cache_dirty = True
        return found

    def _folder_cover(self, folder):
        with self.lock:
            if folder in self._folder_covers:
                return self._folder_covers[folder]
        cover = folder_cover(folder)
        with self.lock:
            self._folder_covers[folder] = cover
        return cover

    def _tag(self, track_id):
        with self.lock:
            track = self.by_id.get(track_id)
            if track is None or track_id in self._tagged:
                return False
        found = self._tags_for(track["path"])
        cover = True if found.get("has_art") else self._folder_cover(os.path.dirname(track["path"]))
        with self.lock:
            for key in ("title", "artist", "album", "year", "genre", "track"):
                if found.get(key):
                    track[key] = found[key]
            if found.get("duration") and not track["duration"]:
                track["duration"] = round(found["duration"], 2)
            track["art"] = bool(cover)
            if cover:
                self.art[track_id] = cover
            self._tagged.add(track_id)
        return True

    def _read_tags(self):
        dirty = stale = False     # the page's list / its library view is behind
        last = last_library = 0.0
        while True:
            try:
                track_id = self._untagged.get(timeout=0.25)[2]
            except queue.Empty:
                track_id = None
            try:
                if track_id is not None and self._tag(track_id):
                    dirty = stale = True
            except Exception:   # one odd file must not end tag reading for the rest
                pass
            now, idle = time.monotonic(), track_id is None
            if dirty and (idle or now - last > 0.3):
                self.emit("playlist")
                dirty, last = False, now
            if stale and (idle or now - last_library > 1.5):
                if self.collection:
                    self.emit("library")
                stale, last_library = False, now
            if idle:
                self._save_cache()

    def set_duration(self, track_id, seconds):
        """The length the page measured while playing; beats any estimate."""
        with self.lock:
            track = self.by_id.get(track_id)
            if track is None or seconds <= 0:
                return
            track["duration"] = round(seconds, 2)
            hit = self._cache.get(track["path"])
            if hit:
                hit[2]["duration"] = track["duration"]
                self._cache_dirty = True

    def cover(self, track_id):
        """A track's cover: (mime, bytes) from its tag, a file path, or None.
        A YouTube Music song's cover is a file once it was fetched (keep_cover)."""
        with self.lock:
            track = self.by_id.get(track_id)
            art = self.art.get(track_id)
            cached = self._covers.get(track["path"]) if track else None
        if track is not None and track.get("source"):
            path = self._stream_cover(track)
            return path if path and os.path.isfile(path) else None
        if track is None or not art:
            return None
        if art is not True:
            return art
        if cached:
            return cached
        picture = tags.read(track["path"], art=True).get("art")
        if picture:
            with self.lock:
                self._covers[track["path"]] = picture
                while len(self._covers) > 8:
                    self._covers.pop(next(iter(self._covers)))
        return picture

    def cover_file(self, track_id):
        """The cover as a file on disk, for the desktop's media controls."""
        art = self.cover(track_id)
        if art is None or isinstance(art, str):
            return art
        mime, data = art
        ext = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(mime, ".jpg")
        path = os.path.join(CACHE_DIR, "covers", hashlib.sha1(data).hexdigest()[:16] + ext)
        if not os.path.exists(path):
            return self._write_cover(path, data)
        return path

    @staticmethod
    def _stream_cover(track):
        if not track.get("cover"):
            return None
        name = "yt-" + hashlib.sha1(track["cover"].encode()).hexdigest()[:16] + ".jpg"
        return os.path.join(STREAM_COVERS, name)

    def keep_cover(self, track, picture):
        """Keep a YouTube Music song's cover, (mime, bytes), as a file in the
        runtime folder."""
        path = self._stream_cover(track)
        return self._write_cover(path, picture[1]) if path else None

    @staticmethod
    def forget_stream_covers(folder=STREAM_COVERS):
        """Remove the YouTube Music covers kept in a folder: the runtime one
        when the player closes, or the cache, where the previous version
        kept them."""
        try:
            names = os.listdir(folder)
        except OSError:
            return
        for name in names:
            if name.startswith("yt-"):
                try:
                    os.remove(os.path.join(folder, name))
                except OSError:
                    pass
        if folder == STREAM_COVERS:
            try:
                os.rmdir(folder)
            except OSError:
                pass

    @staticmethod
    def _write_cover(path, data):
        """Write a cover file; its folder keeps the last forty."""
        folder = os.path.dirname(path)
        try:
            os.makedirs(folder, mode=0o700, exist_ok=True)
            with open(path + ".tmp", "wb") as fh:
                fh.write(data)
            os.replace(path + ".tmp", path)
            old = sorted((os.path.join(folder, n) for n in os.listdir(folder)),
                         key=os.path.getmtime)
            for stale in old[:-40]:
                os.remove(stale)
        except OSError:
            return None
        return path

    # ---- the media library ----
    def folders(self):
        with self.lock:
            chosen = self.settings.get("folders")
        if chosen is None:
            chosen = [music_dir()]
        return [f for f in chosen if isinstance(f, str)]

    def add_folder(self, folder):
        folder = os.path.abspath(folder)
        current = self.folders()
        if folder not in current:
            with self.lock:
                self.settings["folders"] = current + [folder]
            self.save()
        self.scan()

    def remove_folder(self, folder):
        with self.lock:
            self.settings["folders"] = [f for f in self.folders() if f != folder]
        self.save()
        self.scan()

    def scan(self):
        """List what the library folders hold (in a thread)."""
        with self.lock:
            if self.scanning:
                self._rescan = True
                return
            self.scanning = True
        threading.Thread(target=self._scan, daemon=True).start()

    def _scan(self):
        while True:
            files, lists = [], []
            for folder in self.folders():
                for root, dirs, names in os.walk(folder):
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                    for name in names:
                        if name.startswith("."):
                            continue
                        ext = os.path.splitext(name)[1].lower()
                        if ext in AUDIO_EXTENSIONS:
                            files.append(os.path.join(root, name))
                        elif ext in PLAYLIST_EXTENSIONS:
                            lists.append(os.path.join(root, name))
            with self.lock:
                self._folder_covers = {}
                self.collection = [self._entry(path, urgent=False)
                                   for path in dict.fromkeys(files)]
                self._found_lists = lists
                again, self._rescan = self._rescan, False
                if not again:
                    self.scanning = False
            if not again:
                break
        self.emit("library")
        self.emit("playlist")   # the list of saved playlists rides along

    # ---- saved playlists ----
    def playlist_files(self):
        with self.lock:
            paths = list(dict.fromkeys(self._found_lists + self._remembered))
        found = [{"name": os.path.splitext(os.path.basename(p))[0], "path": p,
                  "list": self._playlist_list(p)} for p in paths if os.path.isfile(p)]
        return sorted(found, key=lambda item: _natural(item["name"]))

    def _playlist_list(self, path):
        """The list a saved playlist belongs with: YouTube Music when it holds
        only YouTube Music songs, My Music otherwise (read again when the
        file changes)."""
        try:
            info = os.stat(path)
        except OSError:
            return "local"
        stamp = (info.st_mtime_ns, info.st_size)
        with self.lock:
            known = self._playlist_lists.get(path)
        if known and known[0] == stamp:
            return known[1]
        entries = read_playlist(path)
        which = "youtube" if entries and all(
            "://" in entry and (ytmusic.video_id(entry) or apple.song_id(entry)) for entry in entries) else "local"
        with self.lock:
            self._playlist_lists[path] = (stamp, which)
        return which

    def _remember(self, path):
        with self.lock:
            if path not in self._found_lists and path not in self._remembered:
                self._remembered.append(path)

    def playlist_tracks(self, path):
        """The songs of a saved playlist that still exist, as tracks; YouTube
        Music addresses become YouTube Music songs, Apple Music ones Apple
        Music songs (those with a title in the file)."""
        labels, chosen = {}, []
        with self.lock:
            for entry in dict.fromkeys(read_playlist(path, labels)):
                video = ytmusic.video_id(entry) if "://" in entry else None
                ident = apple.song_id(entry) if "://" in entry else None
                if video or ident:
                    seconds, name = labels.get(entry, (0, ""))
                    artist, _, title = name.partition(" - ")
                    if not title:
                        artist, title = "", name
                    known = self.by_path.get(ytmusic.WATCH + video if video else apple.SONG_URL + ident)
                    chosen.append(known if known and not known.get("bare") else self._stream(
                        {"apple": ident, "title": title, "artist": artist, "duration": seconds} if ident
                        else {"video": video, "title": title, "artist": artist, "duration": seconds}))
                elif "://" not in entry and _is_audio(entry) and os.path.isfile(entry):
                    chosen.append(self._entry(entry))
        unique, listed = [], set()
        for track in chosen:
            if track and track["id"] not in listed:
                listed.add(track["id"])
                unique.append(track)
        return unique

    def open_playlist(self, path):
        """Load a saved playlist: its files become My Music, its YouTube
        Music songs the YouTube Music list (a playlist holds one kind, but the
        previous version could mix them). A playlist that only names a
        stream becomes a radio station. Returns what to play."""
        path = os.path.abspath(path)
        chosen = self.playlist_tracks(path)
        name = os.path.splitext(os.path.basename(path))[0]
        if not chosen:
            stream = next((p for p in read_playlist(path) if p.lower().startswith("http")), None)
            if stream is None:
                return None
            return {"station": self.save_station(None, name, stream)}
        with self.lock:
            for which in LISTS:
                part = [t for t in chosen if list_for(t) == which]
                if part:
                    self.lists[which], self.names[which] = part, name
        self._remember(path)
        self._tag(chosen[0]["id"])
        self.save()
        self.emit("playlist")
        return chosen[0]["id"]

    def save_playlist(self, path, which="local"):
        """Write one of the Now Playing lists as an .m3u file."""
        which = which if which in LISTS else "local"
        with self.lock:
            tracks = [dict(t) for t in self.lists[which]]
            self.names[which] = os.path.splitext(os.path.basename(path))[0]
        write_playlist(path, tracks)
        self._remember(os.path.abspath(path))
        self.save()
        self.emit("playlist")

    # ---- radio ----
    def station(self, station_id):
        with self.lock:
            return next((s for s in self.stations if s["id"] == station_id), None)

    def save_station(self, station_id, name, url):
        """Add a station, or change one. Returns its id."""
        url = url.strip()
        if not re.match(r"https?://", url, re.I):
            url = "http://" + url
        name = name.strip() or urllib.parse.urlparse(url).netloc
        with self.lock:
            station = next((s for s in self.stations
                            if s["id"] == station_id or (station_id is None and s["url"] == url)), None)
            if station is None:
                station = {"id": max([s["id"] for s in self.stations], default=0) + 1}
                self.stations.append(station)
            station.update(name=name, url=url)
        self.save()
        self.emit("playlist")
        return station["id"]

    def remove_station(self, station_id):
        with self.lock:
            self.stations = [s for s in self.stations if s["id"] != station_id]
        self.save()
        self.emit("playlist")

    # ---- what is playing ----
    def set_now(self, info):
        """The page's report: what it plays, where it is, whether it runs."""
        with self.lock:
            now = self.now
            now.update(
                id=info.get("id"), station=info.get("station"),
                mode=info.get("mode") if info.get("mode") in ("playing", "paused") else "stopped",
                position=float(info.get("position") or 0),
                duration=float(info.get("duration") or 0),
                title=str(info.get("title") or ""), at=time.monotonic(),
                started=info.get("started") is not False,   # a YouTube Music song: heard yet
            )
            track = self.by_id.get(now["id"])
            if track is not None and track.get("source"):
                track = None   # a YouTube Music song is never the resume point
            if now["station"] is not None:
                self.resume = {"station": now["station"]}
            elif track is not None:
                position = now["position"] if now["mode"] != "stopped" else 0
                self.resume = {"path": track["path"], "position": round(position, 1)}
            # otherwise the last file or station stays the resume point; the next start checks it is still listed
            due = (now["station"] is not None or track is not None) and (
                time.monotonic() - self._resume_saved > 15 or now["mode"] != "playing")
            if due:
                self._resume_saved = time.monotonic()
        if due:
            self.save()
        if self.on_now:
            self.on_now()

    def position(self):
        """Where playback is right now, in seconds."""
        with self.lock:
            now = self.now
            if now["mode"] != "playing":
                return now["position"]
            return now["position"] + time.monotonic() - now["at"]
