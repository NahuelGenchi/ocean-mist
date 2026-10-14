"""What the player knows: the Now Playing list, the media library, saved
playlists, radio stations and the settings. Everything lives in state.json;
the files themselves are only ever read."""
import hashlib
import json
import os
import queue
import re
import threading
import time
import urllib.parse

import tags

APP_ID = "io.github.nahuelgenchi.OceanMist"
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
AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".wav", ".wma",
}
PLAYLIST_EXTENSIONS = {".m3u", ".m3u8", ".pls"}
COVER_NAMES = ("cover", "folder", "front", "album", "albumart")

# settings the page may change; the rest (window size, zoom, folders) are the host's
PAGE_SETTINGS = ("volume", "muted", "vis", "shuffle", "repeat", "hue",
                 "eq", "eq_on", "eq_preset", "eq_open", "notify")
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


def read_playlist(path):
    """The entries of an .m3u/.m3u8/.pls file: file paths and stream addresses."""
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
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if pls:
            key, _, line = line.partition("=")
            if not key.strip().lower().startswith("file"):
                continue
            line = line.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"[a-z][a-z0-9+.-]*://", line, re.I):
            if not line.lower().startswith("file://"):
                entries.append(line)
                continue
            line = urllib.parse.unquote(urllib.parse.urlparse(line).path)
        target = os.path.normpath(os.path.join(folder, os.path.expanduser(line)))
        if not os.path.exists(target) and "\\" in line:   # written on Windows
            target = os.path.normpath(os.path.join(folder, line.replace("\\", "/")))
        entries.append(target)
    return entries


def write_playlist(path, tracks):
    folder = os.path.dirname(os.path.abspath(path))
    lines = ["#EXTM3U"]
    for track in tracks:
        label = track["title"]
        if track["artist"]:
            label = f'{track["artist"]} - {label}'
        seconds = int(round(track["duration"])) if track["duration"] else -1
        lines.append(f"#EXTINF:{seconds},{label}")
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
        self.tracks = []          # Now Playing, in order
        self.list_name = ""       # the saved playlist it was loaded from
        self.collection = []      # what the library folders hold
        self.scanning = False
        self.art = {}             # track id -> cover file path, True if embedded
        self.stations = []        # {"id", "name", "url"}
        self.settings = {
            "volume": 0.5, "muted": False, "vis": 0, "shuffle": False,
            "repeat": False, "hue": 0, "eq": [0] * 10, "eq_on": False,
            "eq_preset": "Flat", "eq_open": False, "notify": False,
            "zoom": 1, "mini": False,
        }
        self.listeners = set()
        self.pending_play = None  # what to start once the page is listening
        self.now = {"id": None, "station": None, "mode": "stopped",
                    "position": 0.0, "duration": 0.0, "title": "", "at": 0.0}
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
        self.list_name = saved.get("list", "")
        self.add_paths(saved.get("playlist", []), announce=False)
        self.resume = self._resume_offer = saved.get("resume") or None

    def save(self):
        with self.lock:
            data = {"settings": dict(self.settings),
                    "playlist": [t["path"] for t in self.tracks],
                    "list": self.list_name, "stations": list(self.stations),
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
                if track and track in self.tracks:
                    resume = {"id": track["id"], "position": offer.get("position") or 0}
                elif any(s["id"] == offer.get("station") for s in self.stations):
                    resume = {"station": offer["station"]}
            return {"tracks": [self._public(t) for t in self.tracks],
                    "settings": dict(self.settings), "play": pending,
                    "resume": resume, "list": self.list_name,
                    "stations": list(self.stations),
                    "playlists": self.playlist_files(),
                    "folders": self.folders()}

    def library_snapshot(self):
        with self.lock:
            return {"tracks": [self._public(t) for t in self.collection],
                    "scanning": self.scanning}

    @staticmethod
    def _public(track):
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

    def track(self, track_id):
        with self.lock:
            return self.by_id.get(track_id)

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
        """Add files (folders are scanned); with replace they become the whole
        list. Returns the id of the first of them."""
        files = self._expand(paths)
        if not files:
            return None
        with self.lock:
            chosen = [self._entry(path) for path in files]
            if replace:
                self.tracks, self.list_name = chosen, ""
            else:
                listed = {t["id"] for t in self.tracks}
                self.tracks += [t for t in chosen if t["id"] not in listed]
        if announce:
            self.save()
            self.emit("playlist")
        return chosen[0]["id"]

    def open_paths(self, paths):
        """Open songs the way a file manager expects: they become the list,
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
        self.save()
        self.emit("playlist")
        return first

    def play_set(self, ids, start=None):
        """Make these library tracks the Now Playing list."""
        with self.lock:
            chosen = [self.by_id[i] for i in dict.fromkeys(ids) if i in self.by_id]
            if not chosen:
                return None
            self.tracks, self.list_name = chosen, ""
            first = start if any(t["id"] == start for t in chosen) else chosen[0]["id"]
        self.save()
        self.emit("playlist")
        return first

    def add_ids(self, ids):
        with self.lock:
            listed = {t["id"] for t in self.tracks}
            self.tracks += [self.by_id[i] for i in dict.fromkeys(ids)
                            if i in self.by_id and i not in listed]
        self.save()
        self.emit("playlist")

    def move(self, ids, before):
        """Move tracks in front of another one (or to the end)."""
        with self.lock:
            moving = [t for t in self.tracks if t["id"] in ids]
            rest = [t for t in self.tracks if t["id"] not in ids]
            at = next((n for n, t in enumerate(rest) if t["id"] == before), len(rest))
            self.tracks = rest[:at] + moving + rest[at:]
        self.save()
        self.emit("playlist")

    def remove(self, ids):
        with self.lock:
            self.tracks = [t for t in self.tracks if t["id"] not in ids]
        self.save()
        self.emit("playlist")

    def clear(self):
        with self.lock:
            self.tracks, self.list_name = [], ""
        self.save()
        self.emit("playlist")

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
        """A track's cover: (mime, bytes) from its tag, a file path, or None."""
        with self.lock:
            track = self.by_id.get(track_id)
            art = self.art.get(track_id)
            cached = self._covers.get(track["path"]) if track else None
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
        folder = os.path.join(CACHE_DIR, "covers")
        path = os.path.join(folder, hashlib.sha1(data).hexdigest()[:16] + ext)
        if not os.path.exists(path):
            try:
                os.makedirs(folder, exist_ok=True)
                with open(path, "wb") as fh:
                    fh.write(data)
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
        found = [{"name": os.path.splitext(os.path.basename(p))[0], "path": p}
                 for p in paths if os.path.isfile(p)]
        return sorted(found, key=lambda item: _natural(item["name"]))

    def _remember(self, path):
        with self.lock:
            if path not in self._found_lists and path not in self._remembered:
                self._remembered.append(path)

    def playlist_tracks(self, path):
        """The songs of a saved playlist that still exist, as tracks."""
        files = [p for p in read_playlist(path)
                 if "://" not in p and os.path.isfile(p) and _is_audio(p)]
        with self.lock:
            return [self._entry(p) for p in dict.fromkeys(files)]

    def open_playlist(self, path):
        """Load a saved playlist as the Now Playing list. A playlist that
        only names a stream becomes a radio station. Returns what to play."""
        path = os.path.abspath(path)
        chosen = self.playlist_tracks(path)
        name = os.path.splitext(os.path.basename(path))[0]
        if not chosen:
            stream = next((p for p in read_playlist(path) if p.lower().startswith("http")), None)
            if stream is None:
                return None
            return {"station": self.save_station(None, name, stream)}
        with self.lock:
            self.tracks, self.list_name = chosen, name
        self._remember(path)
        self._tag(chosen[0]["id"])
        self.save()
        self.emit("playlist")
        return chosen[0]["id"]

    def save_playlist(self, path):
        with self.lock:
            tracks = [dict(t) for t in self.tracks]
            self.list_name = os.path.splitext(os.path.basename(path))[0]
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
            )
            track = self.by_id.get(now["id"])
            if now["station"] is not None:
                self.resume = {"station": now["station"]}
            elif track is not None:
                position = now["position"] if now["mode"] != "stopped" else 0
                self.resume = {"path": track["path"], "position": round(position, 1)}
            else:
                self.resume = None
            due = time.monotonic() - self._resume_saved > 15 or now["mode"] != "playing"
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
