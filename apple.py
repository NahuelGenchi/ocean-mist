"""Apple Music, read through Apple's own web page: your library.

Apple lets only developers who pay Apple use its music API. Ocean Mist asks
for no such account. It does what a browser tab does: it opens
music.apple.com, Apple's web player, in a view of its own that has no window,
signed in with the one cookie (media-user-token) of the Firefox profile you
chose (firefox.py reads it), and asks the page's own MusicKit for your
library. The view keeps everything in memory, blocks every request that would
sign you out, and is closed after ten minutes without a request. Nothing is
written anywhere, and nothing in your Apple Music account is changed.

This view only reads. Apple's audio is protected by DRM, which this view
cannot play and Ocean Mist does not unlock: a song is played by the playback
engine (engine.py), a Chromium-based helper with Widevine that opens the same
page and plays it as Apple's own audio.

These are the requests Apple's own page makes, not a published way in for a
program like this one, so Apple can change them at any time. What may need
updating then is kept together below: the page's address, what the page is
asked (READY_JS, CALL_JS), the library requests (LIBRARY) and the rules that
keep it from signing you out (Page._rules).

    python3 apple.py      opens Apple's page with the Apple Music sign-in of
                          Firefox and counts what your library holds (no titles)
"""
import http.client
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import firefox
import ytmusic
from ytmusic import YTError

SITE = "https://music.apple.com/"
PAGE = 100                       # items Apple sends at a time
LIMIT = ytmusic.PLAYLIST_LIMIT   # songs read from one album or playlist
READY = 45                       # seconds Apple's page gets to open and sign in
SETTLE = 10                      # ...to say it is signed in, once its player is up
ASKED = 60                       # seconds a request gets
IDLE = 600                       # seconds without a request after which the page is closed
CHECK = 60                       # seconds between looks at whether the page has been idle
PICTURES = 64                    # covers kept in memory
MEMORY = 256                     # MB WebKit is told the page's process may use (see _open)

OFFLINE = "cannot reach Apple Music; check the internet connection"
SIGNED_OUT = "Apple Music says you are signed out; sign in again in Firefox"
CHANGED = "Apple Music changed something Ocean Mist depends on, so the player needs an update"
UNSAFE = "Ocean Mist could not keep Apple's page from signing you out, so it did not open it"

PICTURE_HOSTS = re.compile(r"^https://(?:[a-z0-9-]+\.)*(?:mzstatic\.com|blobstore\.apple\.com)/", re.I)
ITEM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,59}\Z")   # a song's, album's, playlist's or artist's id
SONG_URL = "https://music.apple.com/song/"        # how a saved playlist names a song

# what the library holds: where Apple keeps it, and how many items come at a time
LIBRARY = {
    "recent": ("/v1/me/library/recently-added", 25),
    "playlists": ("/v1/me/library/playlists", PAGE),
    "albums": ("/v1/me/library/albums", PAGE),
    "artists": ("/v1/me/library/artists", PAGE),
    "songs": ("/v1/me/library/songs", PAGE),
}
PAGES = {"playlist": "playlists", "album": "albums", "artist": "artists"}
TOP = frozenset(path for path, _ in LIBRARY.values())   # the lists of the library itself
# what a request's address says when it signs you out, in the spellings Apple might use
SIGN_OUT = ("log.?out", "sign.?out", "log.?off", "sign.?off", "unauthori[sz]e")


class AppleError(YTError):
    """What went wrong, worded for the status line. A YTError, so that what
    answers a YouTube Music error answers this too; `signed_out` when Apple
    Music no longer accepts the sign-in."""

    def __init__(self, message, signed_out=False, status=0, general=False):
        super().__init__(message, general=general, signed_out=signed_out)
        self.status = status


# ---- what the page is asked ----

# is Apple's player up, and does it say you are signed in?
READY_JS = """(() => {
  try {
    const mk = MusicKit.getInstance();
    return JSON.stringify({ host: location.host, api: !!(mk.api && typeof mk.api.music === 'function'), signed: !!mk.isAuthorized });
  } catch (e) { return JSON.stringify({ host: location.host, api: false, signed: false }); }
})()"""

# a call of the page's own MusicKit, as the body of an async function whose
# argument `req` is {path, query}; it answers {ok, body} or {ok: false, status}
CALL_JS = """
const { path, query } = JSON.parse(req);
try {
  const mk = MusicKit.getInstance();
  if (!mk.isAuthorized) return JSON.stringify({ ok: false, signed_out: true });
  const reply = await mk.api.music(path, query);
  const body = reply && reply.data !== undefined ? reply.data : reply;
  const first = body && Array.isArray(body.errors) ? body.errors[0] : null;
  if (first) return JSON.stringify({ ok: false, status: Number(first.status) || 0 });
  return JSON.stringify({ ok: true, body });
} catch (e) {
  const first = e && Array.isArray(e.errors) ? e.errors[0] : null;
  const status = Number((e && (e.status || e.httpStatus || e.statusCode)) || (first && first.status)) || 0;
  return JSON.stringify({ ok: false, status, failed: String((e && e.message) || e).slice(0, 100) });
}
"""


def _answer(raw, personal=True):
    """What the page said to a call: Apple's answer, or an AppleError. A 403 means
    you are signed out only for the library itself (`personal`); for one page of it, it
    means that page is not for Ocean Mist."""
    try:
        said = json.loads(raw)
    except (TypeError, ValueError):
        said = None
    if not isinstance(said, dict):
        raise AppleError(CHANGED)
    status = said.get("status") if isinstance(said.get("status"), int) else 0
    if said.get("signed_out") or status == 401 or (status == 403 and personal):
        raise AppleError(SIGNED_OUT, signed_out=True, status=status)
    if not said.get("ok"):
        if status == 403:
            raise AppleError("Apple Music did not let Ocean Mist open that", status=403)
        if status == 404:
            raise AppleError("Apple Music has no such page", status=404)
        if status == 429:
            raise AppleError("Apple Music asked Ocean Mist to slow down; try again in a minute", status=429)
        raise AppleError(f"Apple Music did not answer (HTTP {status})" if status else OFFLINE, status=status)
    if not isinstance(said.get("body"), dict):
        raise AppleError(CHANGED)
    return said["body"]


# ---- the page ----

class Page:
    """Apple's web player in a view with no window, signed in with `token`
    (firefox.AppleToken). Only the GTK main thread touches it; `call` answers
    through a function, once the page is up."""

    def __init__(self, token, site=SITE, folder=None):
        import gi
        gi.require_version("Soup", "3.0")
        gi.require_version("WebKit2", "4.1")
        from gi.repository import GLib, Soup, WebKit2
        self.GLib, self.Soup, self.WebKit2 = GLib, Soup, WebKit2
        self.site, self.state, self.error = site, "loading", None
        self.waiting, self.running = [], {}
        self.released = False
        self.view = self.expiry = None
        self.born = time.monotonic()
        self.deadline = self.born + READY
        self.loaded = self.seen = None
        self.token = token
        base = folder or os.environ.get("XDG_RUNTIME_DIR")   # the rules' compiled form: in memory, or nowhere
        if not base or not os.path.isdir(base):
            raise AppleError(UNSAFE)
        self.folder = tempfile.mkdtemp(prefix="ocean-mist-apple-", dir=base)
        try:
            self._open()
        except BaseException:
            self.token = None
            self._release()
            raise

    def _open(self):
        GLib, WebKit2 = self.GLib, self.WebKit2
        # in memory: no cache, no site data on disk. Past a third of MEMORY, WebKit lets go of what the page's
        # process holds and no longer needs, and of more past half (its garbage and compiled code too), at most
        # once a minute while it stays there; it never ends the page for it
        try:
            pressure = WebKit2.MemoryPressureSettings.new()
            pressure.set_memory_limit(MEMORY)
            pressure.set_poll_interval(60)
            self.context = WebKit2.WebContext(website_data_manager=WebKit2.WebsiteDataManager.new_ephemeral(),
                                              memory_pressure_settings=pressure)
        except (AttributeError, TypeError):   # a WebKitGTK without the setting (before 2.34)
            self.context = WebKit2.WebContext.new_ephemeral()
        manager = self.context.get_website_data_manager()
        for name in ("set_itp_enabled", "set_resource_load_statistics_enabled"):
            if hasattr(manager, name):
                getattr(manager, name)(False)
                break
        self.cookies = manager.get_cookie_manager()
        self.cookies.set_accept_policy(WebKit2.CookieAcceptPolicy.ALWAYS)
        settings = WebKit2.Settings()
        settings.set_user_agent(ytmusic.BROWSER)
        # the page is only a source of data: it needs no sound, video, pictures or graphics
        # card, and with them its web process crashed a few seconds after the page loaded
        for name in ("enable-media", "enable-mediasource", "enable-encrypted-media", "enable-webgl",
                     "enable-webaudio", "auto-load-images"):
            try:
                settings.set_property(name, False)
            except (TypeError, ValueError):
                pass
        try:
            settings.set_hardware_acceleration_policy(WebKit2.HardwareAccelerationPolicy.NEVER)
        except (AttributeError, TypeError):
            pass
        self.view = WebKit2.WebView(web_context=self.context, settings=settings)
        self.view.connect("web-process-terminated", self._crashed)
        self.view.connect("load-changed", self._load_changed)
        self.view.connect("load-failed", self._load_failed)
        self.view.connect("decide-policy", self._decide)
        self.view.connect("create", lambda *_: None)   # no windows of its own
        store = WebKit2.UserContentFilterStore.new(self.folder)
        store.save("sign-out", GLib.Bytes.new(self._rules()), None, self._rules_saved, None)
        self.expiry = GLib.timeout_add_seconds(READY + 2, self._expire)   # whatever stalls, it ends

    @staticmethod
    def _rules():
        """Block every request whose address names a sign-out, on any site: if
        Apple's page thinks the sign-in is bad it signs it out, and that would
        end the one in Firefox too."""
        return json.dumps([{"trigger": {"url-filter": f".*{word}.*"}, "action": {"type": "block"}}
                           for word in SIGN_OUT]).encode()

    def _rules_saved(self, store, result, _):
        if self.state != "loading":
            return
        try:
            self.view.get_user_content_manager().add_filter(store.save_finish(result))
        except Exception:   # without the block the page is not opened with your sign-in
            return self._fail(AppleError(UNSAFE))
        try:
            token, Soup = self.token, self.Soup
            left = int(token.expires - time.time()) if token.expires else -1
            cookie = Soup.Cookie.new(firefox.APPLE_KEY, token.value, token.host, "/",
                                     min(max(1, left), 2 ** 31 - 1) if left != -1 else -1)
            cookie.set_secure(token.secure or self.site.startswith("https://"))
            cookie.set_http_only(token.http_only)
            cookie.set_same_site_policy({1: Soup.SameSitePolicy.LAX, 2: Soup.SameSitePolicy.STRICT}.get(
                token.same_site, Soup.SameSitePolicy.NONE))
            self.token = None   # the page holds it now
            self.cookies.add_cookie(cookie, None, self._cookie_added, None)
        except Exception:
            self._fail(AppleError("Ocean Mist could not give your sign-in to Apple's page"))

    def _cookie_added(self, manager, result, _):
        if self.state != "loading":
            return
        try:
            manager.add_cookie_finish(result)
        except Exception:
            return self._fail(AppleError("Ocean Mist could not give your sign-in to Apple's page"))
        self.view.load_uri(self.site)
        self.GLib.timeout_add(400, self._poll)

    def _expire(self):
        self.expiry = None
        if self.state == "loading":
            self._fail(AppleError("Apple Music's page did not open in time; try again"))
        return False

    def _load_changed(self, view, event):
        if event == self.WebKit2.LoadEvent.FINISHED and self.loaded is None:
            self.loaded = time.monotonic()

    def _load_failed(self, view, event, uri, error):
        if self.state == "loading" and self.loaded is None:
            self._fail(AppleError(OFFLINE))
        return False

    def _decide(self, view, decision, kind):
        """Pages of Apple's only: nothing else is opened, in this view or in a window of its own."""
        WebKit2 = self.WebKit2
        if kind == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
            decision.ignore()
            return True
        if kind == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            where = urllib.parse.urlparse(decision.get_navigation_action().get_request().get_uri() or "")
            if where.scheme in ("http", "https") and not (
                    (where.hostname or "").endswith(".apple.com") or where.hostname == urllib.parse.urlparse(self.site).hostname):
                decision.ignore()
                return True
        return False

    def _crashed(self, view, reason):
        if self.state != "dead":
            self._fail(AppleError("Apple Music's page stopped; try again"))

    def _poll(self):
        """Wait for Apple's player to be up and to say that it is signed in."""
        if self.state != "loading":
            return False
        now = time.monotonic()
        if now > self.deadline:
            self._fail(AppleError("Apple Music's page did not open in time; try again"))
            return False

        def asked(view, result, _):
            if self.state != "loading":
                return
            try:
                said = json.loads(view.evaluate_javascript_finish(result).to_string())
            except Exception:
                said = {}
            moved = self.loaded and said.get("host") and said["host"] != urllib.parse.urlparse(self.site).netloc
            if said.get("api") and said.get("signed"):
                self._ready()
                return
            if said.get("api"):
                self.seen = self.seen or now
            if moved or (self.seen and now - self.seen > SETTLE):   # the page asks for a sign-in
                self._fail(AppleError(SIGNED_OUT, signed_out=True))
                return
            self.GLib.timeout_add(400, self._poll)
        self.view.evaluate_javascript(READY_JS, -1, None, None, None, asked, None)
        return False

    def _ready(self):
        self.state = "ready"
        self._stop_timer()
        waiting, self.waiting = self.waiting, []
        for job in waiting:
            self._send(*job)

    def _stop_timer(self):
        if self.expiry is not None:
            self.GLib.source_remove(self.expiry)
            self.expiry = None

    def call(self, path, query, finish):
        """Ask MusicKit for `path`; `finish(ok, answer or AppleError)` is called
        from the main thread."""
        if self.state == "dead":
            finish(False, self.error)
        elif self.state == "loading":
            self.waiting.append((path, query, finish))
        else:
            self._send(path, query, finish)

    def _send(self, path, query, finish):
        key = object()
        self.running[key] = finish
        GLib = self.GLib
        request = json.dumps({"path": path, "query": query or {}})
        arguments = GLib.Variant("a{sv}", {"req": GLib.Variant("s", request)})

        def done(view, result, _):
            wanted = self.running.pop(key, None)
            if wanted is None:   # the page ended meanwhile, and said so
                return
            try:
                raw = view.call_async_javascript_function_finish(result).to_string()
                answer = _answer(raw, path in TOP)
            except AppleError as error:
                if error.signed_out:   # Apple no longer takes this sign-in: the next request reads Firefox's again
                    self._fail(error)
                return wanted(False, error)
            except Exception:
                return wanted(False, AppleError("Apple Music's page did not answer"))
            wanted(True, answer)
        self.view.call_async_javascript_function(CALL_JS, -1, arguments, None, None, None, done, None)

    def _fail(self, error):
        """The page is of no more use: every request waiting for it is answered with `error`."""
        if self.state == "dead":
            return
        self.state, self.error, self.token = "dead", error, None
        self._stop_timer()
        waiting, running = self.waiting, list(self.running.values())
        self.waiting, self.running = [], {}
        for _, _, finish in waiting:
            finish(False, error)
        for finish in running:
            finish(False, error)
        self.GLib.idle_add(self._release)   # not from inside one of the view's own signals

    def close(self, now=False):
        """Stop using the page. `now`: free its view and folder at once, not at
        the main loop's next idle moment (the player is closing)."""
        self._fail(AppleError("Apple Music's page was closed"))
        if now:
            self._release()

    def _release(self):
        if self.released:
            return False
        self.released = True
        for step in ("terminate_web_process", "destroy"):
            try:
                getattr(self.view, step)()
            except Exception:
                pass
        shutil.rmtree(self.folder, ignore_errors=True)
        return False


# ---- the library, as rows for the page ----

def _text(value, limit=300):
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _id(value):
    value = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    return value if ITEM_ID.match(value) else ""


def _year(value):
    found = re.match(r"(?:19|20)\d{2}", value if isinstance(value, str) else "")
    return found.group(0) if found else ""


def _seconds(millis):
    if isinstance(millis, bool) or not isinstance(millis, (int, float)):
        return 0
    return round(min(86400.0, max(0.0, millis / 1000)), 2)


def _cover(artwork, size=ytmusic.COVER):
    """The address of a picture of the size wanted: Apple's picture servers
    draw any size written into the address, whose {w}, {h}, {c} and {f} stand for it."""
    url = artwork.get("url") if isinstance(artwork, dict) else None
    if not isinstance(url, str):
        return ""
    for mark, value in (("{w}", size), ("{h}", size), ("{c}", "bb"), ("{f}", "jpg")):
        url = url.replace(mark, str(value))
    return url if PICTURE_HOSTS.match(url) else ""


def _item(kind, res, **more):
    a = res.get("attributes") if isinstance(res.get("attributes"), dict) else {}
    item = {"service": "apple", "kind": kind, "title": _text(a.get("name")), "artist": "", "album": "",
            "year": "", "duration": 0, "label": "", "info": "", "cover": _cover(a.get("artwork"))}
    item.update(more)
    return item, a


def song(res):
    """A song of the library (or of a playlist), as a row; None if it names nothing."""
    if not isinstance(res, dict):
        return None
    play = res["attributes"].get("playParams") if isinstance(res.get("attributes"), dict) else None
    play = play if isinstance(play, dict) else {}
    library = _id(res.get("id"))
    ident = _id(play.get("catalogId")) or library   # the song's name everywhere: the catalogue's id, else the library's
    # ...and the library's too when it differs: the engine asks Apple for that one first, as it always resolves
    item, a = _item("song", res, apple=ident, apple_lib=library if library != ident else "")
    if not (ident and item["title"]):
        return None
    item.update(artist=_text(a.get("artistName")), album=_text(a.get("albumName")),
                year=_year(a.get("releaseDate")), duration=_seconds(a.get("durationInMillis")))
    return item


def album(res):
    ident = _id(res.get("id")) if isinstance(res, dict) else ""
    if not ident:
        return None
    item, a = _item("album", res, id=ident, label="Album")
    count = a.get("trackCount")
    item.update(artist=_text(a.get("artistName")), year=_year(a.get("releaseDate")),
                info=f"{count} {'song' if count == 1 else 'songs'}" if isinstance(count, int) and count > 0 else "")
    return item if item["title"] else None


def playlist(res):
    ident = _id(res.get("id")) if isinstance(res, dict) else ""
    if not ident:
        return None
    item, a = _item("playlist", res, id=ident, label="Playlist")
    return item if item["title"] else None


def artist(res):
    ident = _id(res.get("id")) if isinstance(res, dict) else ""
    if not ident:
        return None
    item, _ = _item("artist", res, id=ident, label="Artist")
    return item if item["title"] else None


def either(res):
    """A row of Recently added: an album or a playlist."""
    kind = res.get("type") if isinstance(res, dict) else None
    return (album if kind == "library-albums" else playlist if kind == "library-playlists" else lambda _: None)(res)


SHAPES = {"recent": either, "playlists": playlist, "albums": album, "artists": artist, "songs": song}


def song_id(link):
    """The Apple Music song a saved playlist's entry names, or None."""
    link = str(link)
    ident = link[len(SONG_URL):] if link.startswith(SONG_URL) else ""
    return ident if ITEM_ID.match(ident) else None


# ---- the library ----

class AppleMusic:
    """What the player asks of Apple Music. Methods are called from any
    thread but the GTK main one; they wait for the page's answer.

    `token_source()` gives the Apple Music sign-in as firefox.AppleToken, or
    raises AppleError; it is called when the page has to be opened.
    `schedule(fn)` runs `fn` on the GTK main thread."""

    def __init__(self, token_source, schedule, site=SITE, folder=None):
        self.token_source, self.schedule = token_source, schedule
        self.site, self.folder = site, folder
        self.page = None          # the Page; touched on the main thread only
        self.last = 0.0
        self.generation = 0       # changes when you sign out: a request begun before is refused

    # -- the page --
    def _ask(self, path, query=None):
        done, box = threading.Event(), {}
        generation = self.generation
        token = None if self.page is not None and self.page.state != "dead" else self.token_source()

        def finish(ok, value):
            box.update(ok=ok, value=value)
            done.set()

        def run():
            try:
                if generation != self.generation:   # you signed out meanwhile: no page is opened with the sign-in read before
                    raise AppleError(SIGNED_OUT, signed_out=True)
                self.last = time.monotonic()
                page = self.page
                if page is None or page.state == "dead":
                    page = self.page = Page(token or self.token_source(), self.site, self.folder)
                    self.watch(page)
                page.call(path, query, finish)
            except AppleError as error:
                finish(False, error)
            except Exception as error:   # a view that cannot be made, for one
                finish(False, AppleError(f"Ocean Mist could not open Apple's page ({type(error).__name__})"))
        self.schedule(run)
        if not done.wait(ASKED):
            raise AppleError("Apple Music took too long to answer")
        if box["ok"]:
            return box["value"]
        raise box["value"]

    def watch(self, page):
        """Close the page once it has had no request for IDLE seconds."""
        def check():
            if self.page is not page or page.state == "dead":
                return False
            if time.monotonic() - self.last > IDLE:
                self.close_now()
                return False
            return True
        page.GLib.timeout_add_seconds(CHECK, check)

    def close_now(self, now=False):
        page, self.page = self.page, None
        if page is not None:
            page.close(now)

    def close(self):
        """Close the page (from any thread): you signed out, or in again. A request begun before is refused."""
        self.generation += 1
        self.schedule(lambda: self.close_now())

    # -- the library --
    def library(self, kind, offset=0):
        """One page of the library: {"items": [...], "more": offset of the next page or None, "total": n}."""
        if kind not in LIBRARY:
            raise AppleError("that is not part of your Apple Music library")
        path, size = LIBRARY[kind]
        offset = max(0, min(int(offset), 1_000_000))
        body = self._ask(path, {"limit": size, "offset": offset})
        rows = [row for row in body.get("data") or [] if isinstance(row, dict)]
        meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        total = meta.get("total") if isinstance(meta.get("total"), int) else None
        return {"items": [item for item in map(SHAPES[kind], rows) if item],
                "more": offset + len(rows) if body.get("next") and rows else None, "total": total}

    def _all(self, path, shape, limit):
        """Every row of a list Apple sends in pages, up to `limit`: a missing list is an empty one."""
        items, offset = [], 0
        while len(items) < limit:
            try:
                body = self._ask(path, {"limit": PAGE, "offset": offset})
            except AppleError as error:
                if error.status == 404:
                    break
                raise
            rows = [row for row in body.get("data") or [] if isinstance(row, dict)]
            items += [item for item in map(shape, rows) if item]
            offset += len(rows)
            if not (body.get("next") and rows):
                break
        return items[:limit]

    def page_of(self, kind, ident):
        """A playlist, an album or an artist of the library, opened: its title and, for a
        playlist or an album, its songs; for an artist, its albums."""
        if kind not in PAGES or not ITEM_ID.match(str(ident)):
            raise AppleError("that is not a page of your Apple Music library")
        base = f"/v1/me/library/{PAGES[kind]}/{urllib.parse.quote(ident, safe='')}"
        head = self._ask(base).get("data") or []
        res = head[0] if head and isinstance(head[0], dict) else {}
        if kind == "artist":
            item = artist(dict(res, id=ident)) or {"title": ""}
            albums = self._all(base + "/albums", album, LIMIT)
            return {"service": "apple", "kind": "artist", "id": ident, "title": item["title"] or "Artist",
                    "artist": "", "year": "", "label": "Artist", "cover": "", "tracks": [],
                    "info": f"{len(albums)} {'album' if len(albums) == 1 else 'albums'}" if albums else "",
                    "groups": [{"title": "Albums", "items": albums}] if albums else []}
        item = (album if kind == "album" else playlist)(dict(res, id=ident)) or {"title": ""}
        tracks = self._all(base + "/tracks", song, LIMIT)
        cover = item.get("cover") or next((t["cover"] for t in tracks if t["cover"]), "")
        return {"service": "apple", "kind": kind, "id": ident, "title": item["title"] or kind.capitalize(),
                "artist": item.get("artist", ""), "year": item.get("year", ""), "label": item.get("label", ""),
                "cover": cover, "info": "", "tracks": tracks, "groups": []}

class _Stay(urllib.request.HTTPRedirectHandler):
    """A picture server that sends the request elsewhere is not followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not PICTURE_HOSTS.match(newurl):
            raise urllib.error.HTTPError(newurl, code, "that is not an Apple Music picture", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_Stay)
_pictures, _pictures_lock = {}, threading.Lock()


def picture(url, cache=_pictures):
    """A cover's (type, bytes). Only Apple's own picture servers are asked."""
    if not PICTURE_HOSTS.match(str(url)):
        raise AppleError("that is not an Apple Music picture")
    with _pictures_lock:
        found = cache.pop(url, None)
        if found:
            cache[url] = found
            return found
    request = urllib.request.Request(url, headers={"User-Agent": ytmusic.BROWSER})
    try:
        with _opener.open(request, timeout=15) as response:
            kind = response.headers.get_content_type()
            data, until = [], time.monotonic() + 30
            size = 0
            while size < 8 << 20:
                chunk = response.read(64 << 10)
                if not chunk:
                    break
                data.append(chunk)
                size += len(chunk)
                if time.monotonic() > until:
                    raise TimeoutError
            data = b"".join(data)
    except urllib.error.HTTPError as error:
        raise AppleError(f"Apple sent no picture (HTTP {error.code})") from None
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError):
        raise AppleError(OFFLINE) from None
    if not kind.startswith("image/") or not data:
        raise AppleError("Apple sent no picture")
    with _pictures_lock:
        cache[url] = (kind, data)
        while len(cache) > PICTURES:
            del cache[next(iter(cache))]
    return kind, data


def main(argv):
    """Open Apple's page with the Firefox sign-in and count the library."""
    import firefox
    from gi.repository import GLib
    found = firefox.apple_signed_in()
    if not found:
        print("no Firefox profile has an Apple Music sign-in; sign in at", firefox.APPLE_SIGN_IN)
        return 1
    folder = argv[1] if len(argv) > 1 else found[0][0]
    if folder not in dict(found):
        print("that Firefox profile has no Apple Music sign-in")
        return 1

    def token():
        token = firefox.apple_token(folder)
        if token is None:
            raise AppleError(SIGNED_OUT, signed_out=True)
        return token

    apple = AppleMusic(token, lambda fn: GLib.idle_add(lambda: fn() or False))
    loop, result = GLib.MainLoop(), {"code": 0}

    def work():
        began = time.monotonic()
        try:
            for kind in LIBRARY:
                got = apple.library(kind)
                print(f"{kind}: {len(got['items'])} of {got['total']}, more: {got['more'] is not None}"
                      f" ({time.monotonic() - began:.1f} s)", flush=True)
        except AppleError as error:
            print("failed:", error, "(signed out)" if error.signed_out else "")
            result["code"] = 1
        finally:
            apple.close()
            GLib.timeout_add(500, loop.quit)
    threading.Thread(target=work, daemon=True).start()
    loop.run()
    return result["code"]


if __name__ == "__main__":
    sys.exit(main(sys.argv))
