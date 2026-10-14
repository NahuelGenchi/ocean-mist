#!/usr/bin/env python3
"""Ocean Mist - a music player with the classic Windows Media Player 9 layout.

The interface is a local web page (ui/) shown in a frameless WebKit window.
This file is the host: it owns the window and a small HTTP server bound to
127.0.0.1 that feeds the page its files. library.py keeps the lists, tags.py
reads titles and covers, radio.py relays stations, ytmusic.py talks to
YouTube Music, mpris.py talks to the desktop's media controls.

    player.py [FILES OR FOLDERS...]     open the player and play them; a single
                                        song brings its folder along as the queue,
                                        a playlist file (.m3u) is loaded
    player.py --serve PORT              UI development: server only, no window
"""
import json
import mimetypes
import os
import queue
import secrets
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import radio
from library import (APP_ID, AUDIO_EXTENSIONS, PAGE_SETTINGS, PLAYLIST_EXTENSIONS,
                     Library, is_playlist, music_dir)
from ytmusic import YTError, YTMusic

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(HERE, "ui")
MIN_SIZE = (580, 460)
ZOOMS = (1, 1.25, 1.5, 1.75, 2)
SIMILAR = 25   # songs added at a time when similar songs carry on after the list


# -- http ---------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    library = None
    music = None  # YTMusic
    token = ""
    host = None  # object with window/dialog methods, or None in --serve mode

    def log_message(self, fmt, *args):
        if os.environ.get("OCEAN_MIST_DEBUG"):
            sys.stderr.write("http: " + fmt % args + "\n")

    def _route(self):
        path = urllib.parse.urlparse(self.path).path
        prefix = f"/{self.token}/"
        if not path.startswith(prefix):
            return None
        return urllib.parse.unquote(path[len(prefix):])

    def _send(self, status, body=b"", content_type="text/plain", extra=None):
        extra = dict(extra or {})
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", extra.pop("Cache-Control", "no-store"))
        for key, value in extra.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, status=200):
        self._send(status, json.dumps(data).encode(), "application/json")

    def _file(self, path, content_type=None):
        try:
            size = os.path.getsize(path)
            fh = open(path, "rb")
        except OSError:
            return self._send(404, b"not found")
        content_type = content_type or mimetypes.guess_type(path)[0] or "application/octet-stream"
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            first, _, last = rng[6:].split(",")[0].partition("-")
            try:
                if first:
                    start = int(first)
                    end = min(int(last), size - 1) if last else size - 1
                else:
                    start = max(0, size - int(last))
                status = 206
            except ValueError:
                start, end, status = 0, size - 1, 200
            if start > end or start >= size:
                fh.close()
                return self._send(416, extra={"Content-Range": f"bytes */{size}"})
        with fh:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", "no-store")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            fh.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = fh.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        route = self._route()
        if route is None:
            return self._send(403, b"forbidden")
        if route in ("", "index.html"):
            return self._file(os.path.join(UI_DIR, "index.html"), "text/html; charset=utf-8")
        if route.startswith("ui/"):
            target = os.path.normpath(os.path.join(UI_DIR, route[3:]))
            if not target.startswith(UI_DIR + os.sep):
                return self._send(403, b"forbidden")
            return self._file(target)
        if route.startswith("media/"):
            track = self._track(route[6:])
            return self._file(track["path"]) if track else self._send(404, b"gone")
        if route.startswith("art/"):
            track = self._track(route[4:])
            art = self.library.cover(track["id"]) if track else None
            if art is None and track and track.get("cover"):   # a YouTube Music song's, fetched once
                try:
                    art = self.library.keep_cover(track, self.music.picture(track["cover"]))
                except YTError:
                    art = None
            if isinstance(art, tuple):
                return self._send(200, art[1], art[0])
            return self._file(art) if art else self._send(404, b"no art")
        if route.startswith("radio/"):
            return self._radio(route[6:])
        if route.startswith("yt/"):
            return self._youtube(route[3:])
        if route == "api/state":
            return self._json(self.library.snapshot())
        if route == "api/library":
            return self._json(self.library.library_snapshot())
        if route == "api/events":
            return self._events()
        self._send(404, b"not found")

    def _track(self, raw):
        try:
            return self.library.track(int(raw))
        except ValueError:
            return None

    def _radio(self, raw):
        """Relay a station's stream; its song titles go out as events."""
        library = self.library
        try:
            station = library.station(int(raw))
        except ValueError:
            station = None
        if station is None:
            return self._send(404, b"no such station")
        if self.command == "HEAD":
            return self._send(200, content_type="audio/mpeg")
        try:
            upstream = radio.open_stream(station["url"])
        except radio.RadioError as error:
            library.emit("radio-error", id=station["id"], message=str(error))
            return self._send(502, str(error).encode())
        self.send_response(200)
        self.send_header("Content-Type", upstream.headers.get("Content-Type", "audio/mpeg"))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        radio.relay(upstream, self.wfile.write,
                    lambda title: library.emit("radio-title", id=station["id"], title=title))

    def _youtube(self, what):
        """YouTube Music for the page: searches and pages as JSON, a song as
        an initialization segment and numbered pieces of MP4, and covers."""
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        ask = lambda key: (query.get(key) or [""])[0]
        music = self.music
        try:
            if what == "search":
                return self._json(music.search(ask("q"), ask("kind")))
            if what == "more":
                return self._json(music.more(ask("token"), ask("kind")))
            if what in ("album", "artist", "playlist"):
                return self._json(getattr(music, what)(ask("id")))
            if what == "picture":
                kind, data = music.picture(ask("u"))
                return self._send(200, data, kind, {"Cache-Control": "max-age=86400"})
            if what.startswith("song/"):
                video, _, part = what[5:].partition("/")
                if not part:
                    song = music.song(video)
                    self.library.stream_found(video, song.details)
                    return self._json(song.info())
                if part == "init":
                    return self._send(200, music.init(video), "audio/mp4")
                if part.isascii() and part.isdigit():
                    return self._send(200, music.piece(video, int(part)), "audio/mp4")
        except YTError as error:
            return self._json({"error": str(error), "general": error.general}, 502)
        except (BrokenPipeError, ConnectionResetError):
            return None
        self._send(404, b"not found")

    def _similar(self, video, skip):
        """Songs like this one that are not in `skip` (video ids), for the
        Now Playing list; YTError when YouTube Music cannot say."""
        songs = [s for s in self.music.radio(video) if s["video"] not in skip]
        return songs[:SIMILAR]

    def _events(self):
        box = self.library.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(b": hello\n\n")
            self.wfile.flush()
            while True:
                try:
                    payload = box.get(timeout=20)
                    self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.library.unsubscribe(box)

    def do_POST(self):
        route = self._route()
        if route != "api/cmd":
            return self._send(403, b"forbidden")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            msg = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, b"bad request")
        cmd = msg.get("cmd")
        library = self.library
        result = {"ok": True}
        ids = [i for i in msg.get("ids", []) if isinstance(i, int)]
        if cmd == "settings":
            allowed = {k: v for k, v in msg.get("values", {}).items()
                       if k in PAGE_SETTINGS}
            library.settings.update(allowed)
            library.save()
            if library.on_now:
                library.on_now()
        elif cmd == "now":
            library.set_now(msg)
            upcoming = library.track(msg.get("next"))
            if upcoming and upcoming.get("video"):   # a YouTube Music song plays next: get it ready
                self.music.prefetch(upcoming["video"])
        elif cmd == "duration":
            library.set_duration(msg.get("id"), float(msg.get("seconds") or 0))
        elif cmd == "remove":
            library.remove(set(ids))
        elif cmd == "clear":
            library.clear(str(msg.get("list") or "local"))
        elif cmd == "move":
            library.move(set(ids), msg.get("before"))
        elif cmd == "add-ids":
            library.add_ids(ids)
        elif cmd == "play-set":
            first = library.play_set(ids, msg.get("start"))
            if first is not None:   # asked for from the library: the page stays there
                library.request_play({"id": first, "stay": True})
        elif cmd in ("playlist-tracks", "play-playlist"):
            path = str(msg.get("path") or "")
            if not (is_playlist(path) and os.path.isfile(path)):
                result = {"ok": False}
            elif cmd == "playlist-tracks":
                result["tracks"] = [library._public(t) for t in library.playlist_tracks(path)]
            else:
                target = library.open_playlist(path)
                if target is None:
                    result = {"ok": False}
                else:
                    if isinstance(target, int):
                        start = msg.get("start")
                        listed = library.list_of(start) is not None
                        target = {"id": start if listed else target, "stay": bool(msg.get("stay"))}
                    library.request_play(target)
        elif cmd in ("yt-play", "yt-add"):
            items = [i for i in msg.get("items", []) if isinstance(i, dict)]
            if cmd == "yt-play" and msg.get("similar") and items:   # the song, then songs like it
                try:
                    items += self._similar(str(items[0].get("video")), {items[0].get("video")})
                except YTError as error:
                    result["notice"] = str(error)
            chosen = library.add_streams(items, replace=cmd == "yt-play", name=msg.get("name"))
            result["ids"] = [t["id"] for t in chosen]
            if cmd == "yt-play" and chosen:   # asked for from Premium Services: the page stays there
                start = next((t["id"] for t in chosen if t["video"] == msg.get("start")),
                             chosen[0]["id"])
                library.request_play({"id": start, "stay": True})
        elif cmd == "yt-similar":   # the list ran out on a YouTube Music song: carry on
            track = library.track(msg.get("id"))
            if not track or not track.get("video"):
                result = {"ok": False, "error": "that is not a YouTube Music song"}
            else:
                try:
                    with library.lock:
                        listed = {t.get("video") for t in library.lists["youtube"]}
                    chosen = library.add_streams(self._similar(track["video"], listed))
                    result["first"] = chosen[0]["id"] if chosen else None
                except YTError as error:
                    result = {"ok": False, "error": str(error), "general": error.general}
        elif cmd == "station-save":
            if str(msg.get("url") or "").strip():
                result["id"] = library.save_station(
                    msg.get("id"), str(msg.get("name") or ""), str(msg["url"]))
        elif cmd == "station-remove":
            library.remove_station(msg.get("id"))
        elif cmd == "rescan":
            library.scan()
        elif cmd == "folder-remove":
            library.remove_folder(str(msg.get("path") or ""))
        elif self.host is not None:
            self.host.command(cmd, msg)
        self._json(result)


def start_server(library, token, port=0, host=None, music=None):
    Handler.library, Handler.token, Handler.host = library, token, host
    Handler.music = music or YTMusic()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# -- window -------------------------------------------------------------------

def run_app(argv):
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("WebKit2", "4.1")
    from gi.repository import Gdk, Gio, GLib, Gtk, WebKit2

    from mpris import BUS_NAME, Mpris

    EDGES = {
        "n": Gdk.WindowEdge.NORTH, "s": Gdk.WindowEdge.SOUTH,
        "w": Gdk.WindowEdge.WEST, "e": Gdk.WindowEdge.EAST,
        "nw": Gdk.WindowEdge.NORTH_WEST, "ne": Gdk.WindowEdge.NORTH_EAST,
        "sw": Gdk.WindowEdge.SOUTH_WEST, "se": Gdk.WindowEdge.SOUTH_EAST,
    }

    class Host:
        """Runs page commands that need the window, on the GTK thread."""

        def __init__(self, app):
            self.app = app

        def command(self, cmd, msg):
            GLib.idle_add(self._run, cmd, msg)

        def _run(self, cmd, msg):
            app, window = self.app, self.app.window
            if window is None:
                return False
            if cmd == "regions":
                app.regions = msg
                app.apply_shape()
            elif cmd == "close":
                window.close()
            elif cmd == "minimize":
                window.iconify()
            elif cmd == "maximize" and not app.mini:
                (window.unmaximize if window.is_maximized() else window.maximize)()
            elif cmd == "fullscreen" and not app.mini:
                (window.unfullscreen if app.fullscreen else window.fullscreen)()
            elif cmd in ("open-files", "open-folder"):
                app.choose("folder" if cmd == "open-folder" else "files",
                           play=bool(msg.get("play", True)))
            elif cmd == "open-playlist":
                app.choose("playlist")
            elif cmd == "save-playlist":
                app.choose("save", which=str(msg.get("list") or "local"))
            elif cmd == "add-folder":
                app.choose("library")
            elif cmd == "zoom":
                app.set_zoom(msg.get("value"))
            elif cmd == "mini":
                app.set_mini(bool(msg.get("on")), msg.get("width"), msg.get("height"))
            return False

    class Player(Gtk.Application):
        def __init__(self):
            super().__init__(
                application_id=APP_ID,
                flags=Gio.ApplicationFlags.HANDLES_OPEN,
            )
            self.window = None
            self.fullscreen = False
            self.regions = {}
            self.library = Library()
            self.token = secrets.token_urlsafe(16)
            self.server = None
            self.mpris = None
            self.zoom = 1.0
            self.mini = False
            self.full_size = None     # the window's size outside compact mode
            self.music = YTMusic()
            self._announced = None
            self._covers = set()      # YouTube Music songs whose cover was asked for
            self._waiting = set()     # ...and is still on its way

        def do_startup(self):
            Gtk.Application.do_startup(self)
            library = self.library
            library.load()
            self.zoom = self._valid_zoom(library.settings.get("zoom"))
            size = library.settings.get("mini_size")
            if library.settings.get("mini") and size:   # closed in compact mode: open in it
                self.mini, self.mini_size = True, tuple(size)
                self.full_size = list(library.settings.get("size", (760, 538)))
            self.server = start_server(library, self.token, host=Host(self), music=self.music)
            library.on_now = lambda: GLib.idle_add(self._now_changed)
            try:
                self.mpris = Mpris(
                    self.get_dbus_connection(), APP_ID, library, self.control,
                    lambda: self.window and self.window.present(),
                    lambda: self.window.close() if self.window else self.quit(),
                    BUS_NAME + ("Test" if "OCEAN_MIST_APP_ID" in os.environ else ""))
            except Exception as error:   # no session bus: no media keys, that is all
                if os.environ.get("OCEAN_MIST_DEBUG"):
                    print("mpris:", error, file=sys.stderr)
            library.scan()

        def do_activate(self):
            if self.window is None:
                self._build()
            self.window.present()

        def do_open(self, files, n_files, hint):
            paths = [f.get_path() for f in files if f.get_path()]
            first = self.library.open_paths(paths)
            self.do_activate()
            if first is not None:
                self.library.request_play(first)

        def control(self, action, value=None):
            """What the desktop's media controls ask for, passed to the page."""
            self.library.emit("control", action=action, value=value)

        def _now_changed(self):
            self._fetch_cover()
            if self.mpris is not None:
                self.mpris.update()
            self._announce()
            return False

        def _fetch_cover(self):
            """A YouTube Music song's cover for the desktop's media controls
            and the notification, fetched once in a thread; they are told
            again when it is in."""
            library = self.library
            with library.lock:
                track = library.by_id.get(library.now["id"])
                track = dict(track) if track else None
            if (not track or not track.get("cover") or track["id"] in self._covers
                    or library.cover(track["id"])):
                return
            self._covers.add(track["id"])
            self._waiting.add(track["id"])

            def run():
                try:
                    library.keep_cover(track, self.music.picture(track["cover"]))
                except YTError:
                    pass
                GLib.idle_add(lambda: self._waiting.discard(track["id"]) or self._now_changed())

            threading.Thread(target=run, daemon=True).start()

        def _announce(self):
            """A desktop notification when another song starts."""
            library = self.library
            with library.lock:
                now = dict(library.now)
                track = library.by_id.get(now["id"])
                station = library.station(now["station"]) if now["station"] is not None else None
                wanted = bool(library.settings.get("notify"))
            if now["mode"] != "playing" or (track is None and station is None):
                return
            if not now["started"]:
                return   # a YouTube Music song is announced once it is heard, so one that cannot play is not
            if track is not None and track["id"] in self._waiting:
                return   # its cover is on the way; this runs again when it is in
            key = (now["id"], now["station"], now["title"])
            if key == self._announced:
                return
            self._announced = key
            if not wanted or (self.window is not None and self.window.is_active()):
                return
            if station is not None:
                title, body = now["title"] or station["name"], station["name"]
            else:
                title = track["title"]
                body = " — ".join(part for part in (track["artist"], track["album"]) if part)
            note = Gio.Notification.new(title)
            if body:
                note.set_body(body)
            cover = library.cover_file(track["id"]) if track is not None and track["art"] else None
            note.set_icon(Gio.FileIcon.new(Gio.File.new_for_path(cover)) if cover
                          else Gio.ThemedIcon.new(APP_ID))
            self.send_notification("song", note)

        # window
        @staticmethod
        def _valid_zoom(value):
            try:
                return min(ZOOMS, key=lambda zoom: abs(zoom - float(value)))
            except (TypeError, ValueError):
                return 1.0

        def _build(self):
            window = Gtk.ApplicationWindow(application=self, title="Ocean Mist")
            window.set_default_size(*self.library.settings.get("size", (760, 538)))
            window.set_decorated(False)
            window.set_icon_name(APP_ID)
            visual = window.get_screen().get_rgba_visual()
            if visual is not None:
                window.set_visual(visual)
            window.set_app_paintable(True)

            settings = WebKit2.Settings()
            settings.set_media_playback_requires_user_gesture(False)
            settings.set_enable_developer_extras(bool(os.environ.get("OCEAN_MIST_DEBUG")))
            settings.set_enable_smooth_scrolling(False)
            # the default policy only lets silent media start by itself, and
            # a song opened from Files has to start without a click
            policies = WebKit2.WebsitePolicies(autoplay=WebKit2.AutoplayPolicy.ALLOW)
            # an ephemeral context: WebKit keeps its cache and site data in memory
            # and makes no folders on disk, so YouTube Music covers shown in the
            # page are gone when the player closes (an ephemeral view alone still
            # sets up the default context's folders)
            context = WebKit2.WebContext.new_ephemeral()
            view = WebKit2.WebView(web_context=context, settings=settings, website_policies=policies)
            view.set_background_color(Gdk.RGBA(0, 0, 0, 0))
            view.set_zoom_level(self.zoom)
            view.connect("context-menu", lambda *a: not os.environ.get("OCEAN_MIST_DEBUG"))
            view.connect("button-press-event", self._on_press)
            view.connect("motion-notify-event", self._on_motion)
            view.drag_dest_unset()
            window.add(view)

            window.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
            window.drag_dest_add_uri_targets()
            window.connect("drag-data-received", self._on_drop)
            window.connect("destroy", self._on_destroy)
            window.connect("window-state-event", self._on_state)

            port = self.server.server_address[1]
            view.load_uri(f"http://127.0.0.1:{port}/{self.token}/")
            self.window, self.view = window, view
            self._limit()
            window.show_all()

        def _limit(self):
            """The smallest the window may get (compact mode has one size)."""
            window = self.window
            if self.mini:
                size = [int(round(side * self.zoom)) for side in self.mini_size]
                window.set_size_request(*size)
                window.set_resizable(False)
                window.resize(*size)
            else:
                window.set_resizable(True)
                window.set_size_request(*(int(side * self.zoom) for side in MIN_SIZE))

        def _room(self):
            """The size of the screen the window is on."""
            gdk_window = self.window.get_window()
            if gdk_window is None:
                return 100000, 100000
            area = gdk_window.get_display().get_monitor_at_window(gdk_window).get_workarea()
            return area.width, area.height

        def set_zoom(self, value):
            """Make the whole player bigger; the window grows with it."""
            zoom, old = self._valid_zoom(value), self.zoom
            if zoom == old:
                return
            self.zoom = self.library.settings["zoom"] = zoom
            width, height = self.window.get_size()
            if self.full_size:
                self.full_size = [int(side * zoom / old) for side in self.full_size]
            self.view.set_zoom_level(zoom)
            self._limit()
            if not (self.mini or self.window.is_maximized() or self.fullscreen):
                room = self._room()
                self.window.resize(min(room[0], int(round(width * zoom / old))),
                                   min(room[1], int(round(height * zoom / old))))
            self.library.save()
            self.apply_shape()

        def set_mini(self, on, width=None, height=None):
            """Compact mode: a small fixed window with only the controls."""
            window = self.window
            if on == self.mini or (on and not (width and height)):
                return
            if on:
                if self.fullscreen:
                    window.unfullscreen()
                if window.is_maximized():
                    window.unmaximize()
                else:
                    self.full_size = list(window.get_size())
                self.mini_size = (float(width), float(height))
                self.library.settings["mini_size"] = list(self.mini_size)
            self.mini = self.library.settings["mini"] = on
            self._limit()
            if not on:
                size = self.full_size or [int(side * self.zoom) for side in (760, 538)]
                window.resize(*size)
            self.library.save()

        def _on_state(self, window, event):
            state = event.new_window_state
            full = bool(state & Gdk.WindowState.FULLSCREEN)
            flat = full or bool(state & Gdk.WindowState.MAXIMIZED)
            self.fullscreen = full
            self.view.evaluate_javascript(
                f"document.body.classList.toggle('maximized', {str(flat).lower()});"
                f"document.body.classList.toggle('fullscreen', {str(full).lower()});"
                "window.dispatchEvent(new Event('resize'))",
                -1, None, None, None, None)
            return False

        def _on_destroy(self, *_):
            library = self.library
            if self.mini:
                if self.full_size:
                    library.settings["size"] = list(self.full_size)
            elif not (self.window.is_maximized() or self.fullscreen):
                library.settings["size"] = list(self.window.get_size())
            with library.lock:   # the last report may be a few seconds old
                track = library.by_id.get(library.now["id"])
                if (track and library.resume and library.resume.get("path") == track["path"]
                        and library.now["mode"] == "playing"):
                    library.resume["position"] = round(library.position(), 1)
            library.save()
            library.forget_stream_covers()
            self.withdraw_notification("song")
            self.window = None

        def apply_shape(self):
            """Let clicks on the frame's transparent corners fall through."""
            gdk_window = self.window.get_window()
            rects = self.regions.get("shape") or []
            if gdk_window is None:
                return
            if self.fullscreen or not rects:
                rects = None
            _input_shape(gdk_window, rects, self.zoom)

        def _edge_at(self, x, y):
            if self.window.is_maximized() or self.fullscreen or self.mini:
                return None
            grip = self.regions.get("grip")
            if grip and _inside(grip, x, y):
                return Gdk.WindowEdge.SOUTH_EAST
            for rect in self.regions.get("edges", []):
                if _inside(rect, x, y):
                    return EDGES.get(rect.get("edge"))
            return None

        def _on_motion(self, view, event):
            edge = self._edge_at(event.x / self.zoom, event.y / self.zoom)
            name = {
                Gdk.WindowEdge.SOUTH_EAST: "se-resize", Gdk.WindowEdge.SOUTH_WEST: "sw-resize",
                Gdk.WindowEdge.NORTH_EAST: "ne-resize", Gdk.WindowEdge.NORTH_WEST: "nw-resize",
                Gdk.WindowEdge.WEST: "w-resize", Gdk.WindowEdge.EAST: "e-resize",
                Gdk.WindowEdge.NORTH: "n-resize", Gdk.WindowEdge.SOUTH: "s-resize",
            }.get(edge)
            if name != getattr(self, "_cursor", None):
                self._cursor = name
                gdk_window = self.window.get_window()
                cursor = Gdk.Cursor.new_from_name(view.get_display(), name) if name else None
                if gdk_window is not None and (name or cursor is None):
                    gdk_window.set_cursor(cursor)
            return False

        def _on_press(self, view, event):
            if event.button != 1:
                return False
            x, y = event.x / self.zoom, event.y / self.zoom   # the page's own units
            edge = self._edge_at(x, y)
            if edge is not None:
                self.window.begin_resize_drag(
                    edge, event.button, int(event.x_root), int(event.y_root), event.time
                )
                return True
            drag = any(_inside(r, x, y) for r in self.regions.get("drag", []))
            blocked = any(_inside(r, x, y) for r in self.regions.get("nodrag", []))
            if drag and not blocked:
                if event.type == Gdk.EventType.DOUBLE_BUTTON_PRESS:
                    if self.mini:
                        pass
                    elif self.window.is_maximized():
                        self.window.unmaximize()
                    else:
                        self.window.maximize()
                else:
                    self.window.begin_move_drag(
                        event.button, int(event.x_root), int(event.y_root), event.time
                    )
                return True
            return False

        def _on_drop(self, window, context, x, y, selection, info, time):
            paths = []
            for uri in selection.get_uris() or []:
                path = Gio.File.new_for_uri(uri).get_path()
                if path:
                    paths.append(path)
            if len(paths) == 1 and is_playlist(paths[0]):
                first = self.library.open_paths(paths)
            else:
                first = self.library.add_paths(paths)
            if first is not None:
                self.library.request_play(first)
            Gtk.drag_finish(context, True, False, time)

        def choose(self, kind, play=True, which="local"):
            """A file dialog: music to open ("files", "folder"), a playlist to
            load ("playlist") or write ("save", from the Now Playing list
            `which`), a folder for the library."""
            library = self.library
            title, action = {
                "files": ("Open music files", Gtk.FileChooserAction.OPEN),
                "folder": ("Open folder", Gtk.FileChooserAction.SELECT_FOLDER),
                "playlist": ("Open playlist", Gtk.FileChooserAction.OPEN),
                "save": ("Save playlist", Gtk.FileChooserAction.SAVE),
                "library": ("Add folder to the library", Gtk.FileChooserAction.SELECT_FOLDER),
            }[kind]
            dialog = Gtk.FileChooserNative.new(
                title, self.window, action, "_Save" if kind == "save" else "_Open", "_Cancel")
            dialog.set_select_multiple(kind in ("files", "folder"))
            if kind in ("files", "playlist", "save"):
                wanted = Gtk.FileFilter()
                wanted.set_name("Music" if kind == "files" else "Playlists")
                extensions = AUDIO_EXTENSIONS if kind == "files" else PLAYLIST_EXTENSIONS
                for ext in sorted(extensions):
                    wanted.add_pattern("*" + ext)
                    wanted.add_pattern("*" + ext.upper())
                dialog.add_filter(wanted)
            start = library.settings.get("last_folder")
            if kind in ("playlist", "save"):
                lists = os.path.join(music_dir(), "Playlists")
                start = lists if os.path.isdir(lists) else music_dir()
            if start and os.path.isdir(start):
                dialog.set_current_folder(start)
            if kind == "save":
                dialog.set_do_overwrite_confirmation(True)
                name = (library.names.get(which) or "Playlist").replace("/", "-")   # "Similar to AC/DC"
                dialog.set_current_name(name + ".m3u")

            def done(dlg, response):
                paths = dlg.get_filenames() if response == Gtk.ResponseType.ACCEPT else []
                dlg.destroy()
                self._dialog = None
                if paths:
                    self.chosen(kind, paths, play, which)

            dialog.connect("response", done)
            self._dialog = dialog
            dialog.show()

        def chosen(self, kind, paths, play=True, which="local"):
            """What to do with the files a dialog came back with."""
            library = self.library
            if kind in ("files", "folder"):
                library.settings["last_folder"] = (
                    paths[0] if kind == "folder" else os.path.dirname(paths[0]))
                # the menu's Open replaces the list like opening from
                # Files does; the library's Add buttons only add
                opener = library.open_paths if play else library.add_paths
                first = opener(paths)
                if first is not None and play:
                    library.request_play(first)
            elif kind == "playlist":
                first = library.open_playlist(paths[0])
                if first is None:
                    library.emit("notice", text="That playlist has no songs that can be found")
                else:
                    library.request_play(first)
            elif kind == "save":
                path = paths[0]
                if os.path.splitext(path)[1].lower() not in (".m3u", ".m3u8"):
                    path += ".m3u"
                try:
                    library.save_playlist(path, which)
                    library.emit("notice", text="Playlist saved: " + os.path.basename(path))
                except OSError as error:
                    library.emit("notice", text=f"Could not save the playlist ({error.strerror})")
            else:
                library.add_folder(paths[0])

    def _inside(rect, x, y):
        return (rect["x"] <= x < rect["x"] + rect["w"]
                and rect["y"] <= y < rect["y"] + rect["h"])

    def _input_shape(gdk_window, rects, zoom=1.0):
        """gdk_window_input_shape_combine_region through ctypes: building a
        cairo region from Python would need pycairo, which is not a given."""
        try:
            import ctypes
            import ctypes.util
            import math

            class Rect(ctypes.Structure):
                _fields_ = [(n, ctypes.c_int) for n in ("x", "y", "width", "height")]

            cairo = ctypes.CDLL(ctypes.util.find_library("cairo") or "libcairo.so.2")
            gdk = ctypes.CDLL(ctypes.util.find_library("gdk-3") or "libgdk-3.so.0")
            cairo.cairo_region_create.restype = ctypes.c_void_p
            cairo.cairo_region_union_rectangle.argtypes = [ctypes.c_void_p, ctypes.POINTER(Rect)]
            cairo.cairo_region_destroy.argtypes = [ctypes.c_void_p]
            gdk.gdk_window_input_shape_combine_region.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
            pointer = hash(gdk_window)
            if rects is None:
                gdk.gdk_window_input_shape_combine_region(pointer, None, 0, 0)
                return
            region = cairo.cairo_region_create()
            for r in rects:   # page units to window pixels
                left, top = math.floor(r["x"] * zoom), math.floor(r["y"] * zoom)
                box = Rect(left, top, math.ceil((r["x"] + r["w"]) * zoom) - left,
                           math.ceil((r["y"] + r["h"]) * zoom) - top)
                cairo.cairo_region_union_rectangle(region, ctypes.byref(box))
            gdk.gdk_window_input_shape_combine_region(pointer, region, 0, 0)
            cairo.cairo_region_destroy(region)
            if os.environ.get("OCEAN_MIST_DEBUG"):
                print(f"input shape: {len(rects)} rectangles", file=sys.stderr)
        except Exception as error:  # cosmetic only, the player works without it
            if os.environ.get("OCEAN_MIST_DEBUG"):
                print("input shape:", error, file=sys.stderr)

    GLib.set_prgname(APP_ID)
    return Player().run(argv)


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--serve":
        library = Library()
        library.load()
        library.add_paths(sys.argv[3:])
        library.scan()
        server = start_server(library, "dev", port=int(sys.argv[2]), music=YTMusic())
        print(f"http://127.0.0.1:{server.server_address[1]}/dev/", flush=True)
        threading.Event().wait()
        return 0
    return run_app(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
