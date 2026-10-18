"""Spotify, read through Spotify's own web page: your library.

Spotify's Web API needs a client id and, for anything of your account, a
sign-in flow Ocean Mist would have to keep: its own client credentials, tokens
and callbacks. Ocean Mist asks for no such account. It does what a browser tab
does: it opens open.spotify.com, Spotify's web player, in a view of its own
that has no window, signed in with the cookies of the Firefox profile you
chose (firefox.py reads them), and reads the answers the page's own requests
get.

The page talks to Spotify through the same GraphQL requests its interface
makes. Ocean Mist puts a hook in the page before it loads that keeps each
request (its address, its headers and its body) and each answer in memory;
every page of your library is then that same request sent again with a
different offset, made from inside the page, with the page's own sign-in. The
view keeps everything in memory, blocks every request that would sign you out,
and is closed after ten minutes without a request. It only reads: nothing in
your Spotify account is changed, and nothing is written anywhere (a playlist
you save is the player's own file, and a Spotify song in it is saved as its
address and "Artist - Title", nothing else).

These are the requests Spotify's own page makes, not a published way in for a
program like this one, so Spotify can change them at any time. What may need
updating then is kept together below: the page's address, what the page is
asked (READY_JS, CALL_JS), what the hook keeps (HOOK_JS), the library requests
(LIBRARY) and the rules that keep it from signing you out (Page._rules).

    python3 spotify.py      opens Spotify's page with the sign-in of Firefox
                            and counts what your library holds (no titles)
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

SITE = "https://open.spotify.com/"
HOME = "/collection/tracks"      # the web player's page for Liked Songs
ALBUM = "/album/"                # ...a page of an album, then its id
ARTIST = "/artist/"              # ...a page of an artist, then its id
PAGE = 50                        # library items Spotify sends at a time
SONG_PAGE = 50                   # songs of a playlist (from its second page on)
SONG_FIRST = 25                  # ...and of its first page, as the page asks them
LIMIT = ytmusic.PLAYLIST_LIMIT   # songs read from one album or playlist
READY = 45                       # seconds Spotify's page gets to open and sign in
SETTLE = 10                      # ...to say it is signed in, once its shell is up
ASKED = 60                       # seconds a request gets
IDLE = 600                       # seconds without a request after which the page is closed
CHECK = 60                       # seconds between looks at whether the page has been idle
PICTURES = 64                    # covers kept in memory
WINDOW = (1600, 1000)            # the size the windowless view lays the page out at

OFFLINE = "cannot reach Spotify; check the internet connection"
SIGNED_OUT = "Spotify says you are signed out; sign in again in Firefox"
CHANGED = "Spotify changed something Ocean Mist depends on, so the player needs an update"
# after a few of its pages have opened in one view, Spotify's page shows a notice
# that this browser cannot play, in place of its player; a new view opens it again
REFUSED = "Spotify's page would not open in Ocean Mist; try again"
UNSAFE = "Ocean Mist could not keep Spotify's page from signing you out, so it did not open it"

PICTURE_HOSTS = re.compile(r"^https://(?:[a-z0-9-]+\.)*(?:scdn\.co|spotifycdn\.com|spotify\.com)/", re.I)
ITEM_ID = re.compile(r"[A-Za-z0-9]{22}\Z")   # a song's, album's, playlist's or artist's id
SONG_URL = "https://open.spotify.com/track/"   # how a saved playlist names a song

# what the library holds: the request Spotify answers it with, which of the
# library's rows belongs to the kind, and the filter the page's own chips ask
# that kind with. The request is asked with the filter when the page's own one
# has a `filters` variable; without it, or when the answer still mixes the
# kinds, the page sends every kind together, fifty at a time, and Ocean Mist
# keeps the ones it asked for, without a total
LIBRARY = {
    "songs": ("fetchPlaylistContents", "", ""),
    "playlists": ("libraryV3", "PlaylistResponseWrapper", "Playlists"),
    "albums": ("libraryV3", "AlbumResponseWrapper", "Albums"),
    "artists": ("libraryV3", "ArtistResponseWrapper", "Artists"),
}
PSEUDO = "LibraryPseudoPlaylistResponseWrapper"   # Liked Songs, Your Episodes in the playlists list
SCAN = 10                        # mixed pages read at most for one page of a kind
# what a request's address says when it signs you out, in the spellings Spotify might use
SIGN_OUT = ("log.?out", "sign.?out", "log.?off", "sign.?off", "unauthori[sz]e")


class SpotifyError(YTError):
    """What went wrong, worded for the status line. A YTError, so that what
    answers a YouTube Music error answers this too; `signed_out` when Spotify
    no longer accepts the sign-in; `shaped`, the optional variables that were
    set on a request Spotify answered with nothing (see Page.call)."""

    def __init__(self, message, signed_out=False, status=0, general=False):
        super().__init__(message, general=general, signed_out=signed_out)
        self.status = status
        self.shaped = ()


# ---- what the page is asked ----

# is Spotify's player up, and does it say you are signed in?
READY_JS = """(() => {
  try {
    const widget = !!document.querySelector('[data-testid="user-widget-link"]');
    const login = !!document.querySelector('[data-testid="login-button"]');
    const refused = !!document.querySelector('[data-testid="error-page"]');
    const doc = (window.__omOps && window.__omOps.doc) || '';
    return JSON.stringify({ host: location.host, doc, up: widget || login, signed: widget && !login, refused });
  } catch (e) { return JSON.stringify({ host: location.host, doc: '', up: false, signed: false, refused: false }); }
})()"""

# the hook, put in the page before it loads: it keeps each of the page's own
# requests to Spotify (address, headers, body) and each answer, in memory only,
# the newest of the sign-in headers the page sent, a mark of the document it is
# in, and the fetch it replaced, for Ocean Mist's own requests (which are not
# the page's, so they are not kept)
HOOK_JS = """
(() => {
  if (window.__omOps) return;
  const ops = window.__omOps = { requests: {}, responses: {}, latest: {},
                                 doc: Date.now().toString(36) + Math.random().toString(36).slice(2) };
  const api = (url) => /^https:\\/\\/(api\\.spotify\\.com|api-partner\\.spotify\\.com)\\//.test(String(url));
  const opOf = (url, body) => {
    try {
      const name = new URL(url, location.href).searchParams.get('operationName');
      if (name) return name;
    } catch (e) {}
    try {
      const said = typeof body === 'string' ? JSON.parse(body) : null;
      if (said && typeof said.operationName === 'string') return said.operationName;
    } catch (e) {}
    return '';
  };
  const headersOf = (headers) => {
    const out = {};
    try {
      if (!headers) return out;
      if (typeof Headers !== 'undefined' && headers instanceof Headers) {
        for (const pair of headers.entries()) out[pair[0]] = pair[1];
      } else if (Array.isArray(headers)) {
        for (const pair of headers) out[pair[0]] = pair[1];
      } else {
        for (const name of Object.keys(headers)) out[name] = String(headers[name]);
      }
    } catch (e) {}
    return out;
  };
  const remember = (name, url, headers, body) => {
    const kept = headersOf(headers);
    for (const header of Object.keys(kept)) {
      const lower = header.toLowerCase();
      if (lower === 'authorization' || lower === 'client-token') ops.latest[lower] = kept[header];
    }
    ops.requests[name] = { url: String(url), headers: kept, body: typeof body === 'string' ? body : '' };
  };
  const of = ops.fetch = window.fetch;
  window.fetch = function (input, init) {
    let url = '';
    try { url = typeof input === 'string' ? input : (input && input.url) || ''; } catch (e) {}
    if (!api(url)) return of.apply(this, arguments);
    let body = '';
    try { body = init && typeof init.body === 'string' ? init.body : ''; } catch (e) {}
    const name = opOf(url, body);
    if (name) remember(name, url, (init && init.headers) || (input && input.headers), body);
    const reply = of.apply(this, arguments);
    if (name) {
      reply.then((r) => {
        try { r.clone().json().then((j) => { ops.responses[name] = j; }).catch(() => {}); } catch (e) {}
      }).catch(() => {});
    }
    return reply;
  };
  const open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send;
  const setHeader = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__omUrl = String(url);
    return open.apply(this, arguments);
  };
  XMLHttpRequest.prototype.setRequestHeader = function (name, value) {
    (this.__omHeaders = this.__omHeaders || {})[String(name)] = String(value);
    return setHeader.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    const url = this.__omUrl || '';
    if (api(url)) {
      const name = opOf(url, body);
      if (name) {
        remember(name, url, this.__omHeaders, body);
        this.addEventListener('load', () => {
          try { ops.responses[name] = JSON.parse(this.responseText); } catch (e) {}
        });
      }
    }
    return send.apply(this, arguments);
  };
})()"""

# one of the page's own requests, sent again with other variables, as the body
# of an async function whose argument `req` is {op, variables, optional, wait}:
# `optional` are set only where the page's own request has them. It is sent
# with the fetch the hook replaced, so the hook does not keep it as the page's
# own, with the newest sign-in headers the page sent, and given up after `wait`
# seconds. It answers {ok, body, shaped: the optional ones set},
# {ok: false, status}, {ok: false, late}, or {ok: false, stale} while the page
# has not made that request yet
CALL_JS = """
const want = JSON.parse(req);
const ops = window.__omOps;
const base = ops && ops.requests[want.op];
if (!base || typeof ops.fetch !== 'function') return JSON.stringify({ ok: false, stale: true });
let body = base.body;
const shaped = [];
try {
  const said = body ? JSON.parse(body) : {};
  const had = said.variables && typeof said.variables === 'object' ? said.variables : {};
  const extra = {};
  for (const name of Object.keys(want.optional || {})) {
    if (Object.prototype.hasOwnProperty.call(had, name)) {
      extra[name] = want.optional[name];
      shaped.push(name);
    }
  }
  said.variables = Object.assign({}, had, want.variables || {}, extra);
  body = JSON.stringify(said);
} catch (e) {
  return JSON.stringify({ ok: false, failed: 'that request cannot be shaped' });
}
const headers = Object.assign({}, base.headers);
for (const name of Object.keys(headers)) {
  const lower = name.toLowerCase();
  if (ops.latest[lower]) headers[name] = ops.latest[lower];
}
const stop = new AbortController();
const timer = setTimeout(() => stop.abort(), (want.wait || 60) * 1000);
try {
  const reply = await ops.fetch.call(window, base.url,
    { method: 'POST', headers, body, credentials: 'include', signal: stop.signal });
  if (!reply.ok) return JSON.stringify({ ok: false, status: reply.status });
  const data = await reply.json();
  return JSON.stringify({ ok: true, body: data, shaped });
} catch (e) {
  if (stop.signal.aborted) return JSON.stringify({ ok: false, late: true });
  return JSON.stringify({ ok: false, failed: String((e && e.message) || e).slice(0, 100) });
} finally {
  clearTimeout(timer);
}
"""


# ---- the page ----

class Page:
    """Spotify's web player in a view with no window, signed in with `token`
    (firefox.SpotifyToken). Only the GTK main thread touches it; `call` answers
    through a function, once the page is up.

    The page is on one route at a time. A call waits, in order, until the page
    is up on its route; the page moves to the next route asked for only once
    nothing runs on the one it is on, and the calls that came for that route
    meanwhile wait for its next turn."""

    def __init__(self, token, site=SITE, folder=None):
        import gi
        gi.require_version("Soup", "3.0")
        gi.require_version("WebKit2", "4.1")
        gi.require_version("Gtk", "3.0")
        from gi.repository import GLib, Gtk, Soup, WebKit2
        self.GLib, self.Gtk, self.Soup, self.WebKit2 = GLib, Gtk, Soup, WebKit2
        self.site, self.state, self.error, self.route = site, "loading", None, None
        self.queue, self.running = [], {}   # calls not sent yet, in order; calls running
        self.doc = None      # the mark of the document the page was last up in
        self.going = 0       # counts the page's loads, so that a look at an earlier one stops
        self.begun = False       # the load _go asked for has started: what WebKit says before is of the one it replaced
        self.committed = False   # the load the page is waiting for has replaced the document
        self.released = False
        self.view = self.holder = self.expiry = None
        self.born = time.monotonic()
        self.deadline = self.born + READY
        self.loaded = self.seen = None
        self.token = token
        base = folder or os.environ.get("XDG_RUNTIME_DIR")   # the rules' compiled form: in memory, or nowhere
        if not base or not os.path.isdir(base):
            raise SpotifyError(UNSAFE)
        self.folder = tempfile.mkdtemp(prefix="ocean-mist-spotify-", dir=base)
        try:
            self._open()
        except BaseException:
            self.token = None
            self._release()
            raise

    def _open(self):
        GLib, WebKit2 = self.GLib, self.WebKit2
        self.context = WebKit2.WebContext.new_ephemeral()   # in memory: no cache, no site data on disk
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
        # the page lays itself out and shows what fits; the windowless view still
        # needs a size, or the library's own sidebar is never fetched
        self.holder = self.Gtk.OffscreenWindow()
        self.holder.set_default_size(*WINDOW)
        self.holder.add(self.view)
        self.holder.show_all()
        self.view.connect("web-process-terminated", self._crashed)
        self.view.connect("load-changed", self._load_changed)
        self.view.connect("load-failed", self._load_failed)
        self.view.connect("decide-policy", self._decide)
        self.view.connect("create", lambda *_: None)   # no windows of its own
        self.view.get_user_content_manager().add_script(WebKit2.UserScript.new(
            HOOK_JS, WebKit2.UserContentInjectedFrames.TOP_FRAME,
            WebKit2.UserScriptInjectionTime.START, None, None))
        store = WebKit2.UserContentFilterStore.new(self.folder)
        store.save("sign-out", GLib.Bytes.new(self._rules()), None, self._rules_saved, None)
        self.expiry = GLib.timeout_add_seconds(READY + 2, self._expire)   # whatever stalls, it ends

    @staticmethod
    def _rules():
        """Block every request whose address names a sign-out, on any site: if
        Spotify's page thinks the sign-in is bad it signs it out, and that
        would end the one in Firefox too."""
        return json.dumps([{"trigger": {"url-filter": f".*{word}.*"}, "action": {"type": "block"}}
                           for word in SIGN_OUT]).encode()

    def _rules_saved(self, store, result, _):
        if self.state != "loading":
            return
        try:
            self.view.get_user_content_manager().add_filter(store.save_finish(result))
        except Exception:   # without the block the page is not opened with your sign-in
            return self._fail(SpotifyError(UNSAFE))
        try:
            cookies = list(self.token.cookies)
            self.token = None   # the page holds them now
            self._add_cookies(cookies, lambda: self._go(next((job[1] for job in self.queue if job[1]), HOME)))
        except Exception:
            self._fail(SpotifyError("Ocean Mist could not give your sign-in to Spotify's page"))

    def _add_cookies(self, cookies, then):
        Soup = self.Soup
        if not cookies:
            return then()
        token = cookies[0]

        def added(manager, result, _):
            if self.state != "loading":
                return
            try:
                manager.add_cookie_finish(result)
            except Exception:
                return self._fail(SpotifyError("Ocean Mist could not give your sign-in to Spotify's page"))
            self._add_cookies(cookies[1:], then)
        left = int(token.expires - time.time()) if token.expires else -1
        cookie = Soup.Cookie.new(token.name, token.value, token.host, "/",
                                 min(max(1, left), 2 ** 31 - 1) if left != -1 else -1)
        cookie.set_secure(token.secure or self.site.startswith("https://"))
        cookie.set_http_only(token.http_only)
        cookie.set_same_site_policy({1: Soup.SameSitePolicy.LAX, 2: Soup.SameSitePolicy.STRICT}.get(
            token.same_site, Soup.SameSitePolicy.NONE))
        self.cookies.add_cookie(cookie, None, added, None)

    def _go(self, route):
        """Take the view to one of the page's own routes, and wait for it again."""
        self.state, self.route, self.loaded, self.seen, self.committed = "loading", route, None, None, False
        self.begun = False
        self.going += 1
        going = self.going
        self.deadline = time.monotonic() + READY
        self._stop_timer()
        self.expiry = self.GLib.timeout_add_seconds(READY + 2, self._expire)
        self.view.load_uri(self.site.rstrip("/") + route)
        self.GLib.timeout_add(400, lambda: self._poll(going))

    def _expire(self):
        self.expiry = None
        if self.state == "loading":
            self._fail(SpotifyError("Spotify's page did not open in time; try again"))
        return False

    def _load_changed(self, view, event):
        if event == self.WebKit2.LoadEvent.STARTED:
            self.begun = True
        if not self.begun:   # the load this one replaced, saying how it ended
            return
        if event == self.WebKit2.LoadEvent.COMMITTED:
            self.committed = True
        if event == self.WebKit2.LoadEvent.FINISHED and self.loaded is None:
            self.loaded = time.monotonic()

    def _load_failed(self, view, event, uri, error):
        if self.state != "loading" or not self.begun or self.loaded is not None:
            return False
        WebKit2 = self.WebKit2
        try:   # a load another one replaced (a redirect of the page's own, say) is no sign of being offline
            if error.matches(WebKit2.network_error_quark(), WebKit2.NetworkError.CANCELLED) \
                    or error.matches(WebKit2.policy_error_quark(), WebKit2.PolicyError.FRAME_LOAD_INTERRUPTED_BY_POLICY_CHANGE):
                return False
        except (AttributeError, TypeError):
            pass
        self._fail(SpotifyError(OFFLINE))
        return False

    def _decide(self, view, decision, kind):
        """Pages of Spotify's only: nothing else is opened, in this view or in a window of its own."""
        WebKit2 = self.WebKit2
        if kind == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
            decision.ignore()
            return True
        if kind == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            where = urllib.parse.urlparse(decision.get_navigation_action().get_request().get_uri() or "")
            if where.scheme in ("http", "https") and not (
                    (where.hostname or "").endswith(".spotify.com")
                    or where.hostname == urllib.parse.urlparse(self.site).hostname):
                decision.ignore()
                return True
        return False

    def _crashed(self, view, reason):
        if self.state != "dead":
            self._fail(SpotifyError("Spotify's page stopped; try again"))

    def _poll(self, going):
        """Wait for Spotify's player to be up, in the document the last load
        brought (not the one it replaces), and to say that it is signed in."""
        if self.state != "loading" or going != self.going:
            return False
        now = time.monotonic()
        if now > self.deadline:
            self._fail(SpotifyError("Spotify's page did not open in time; try again"))
            return False

        def asked(view, result, _):
            if self.state != "loading" or going != self.going:
                return
            try:
                said = json.loads(view.evaluate_javascript_finish(result).to_string())
            except Exception:
                said = {}
            if not isinstance(said, dict):
                said = {}
            moved = self.loaded and said.get("host") and said["host"] != urllib.parse.urlparse(self.site).netloc
            new = self.committed and isinstance(said.get("doc"), str) and said["doc"] and said["doc"] != self.doc
            if new and said.get("up") and said.get("signed"):
                self.doc = said["doc"]
                self._ready()
                return
            if new and said.get("refused"):   # Spotify's notice in place of its player: see REFUSED
                self._fail(SpotifyError(REFUSED))
                return
            if new and said.get("up"):
                self.seen = self.seen or now
            if moved or (self.seen and now - self.seen > SETTLE):   # the page asks for a sign-in
                self._fail(SpotifyError(SIGNED_OUT, signed_out=True))
                return
            self.GLib.timeout_add(400, lambda: self._poll(going))
        self.view.evaluate_javascript(READY_JS, -1, None, None, None, asked, None)
        return False

    def _ready(self):
        self.state = "ready"
        self._stop_timer()
        self._pump(fresh=True)

    def _stop_timer(self):
        if self.expiry is not None:
            self.GLib.source_remove(self.expiry)
            self.expiry = None

    def call(self, op, route, variables, finish, optional=None):
        """Ask the page's own request again with `variables` (the ones it last
        saw, with these on top) and `optional` (only those it already has);
        `finish(ok, (answer, the optional ones set) or SpotifyError)` is called
        from the main thread. `route` is the page the request lives on."""
        if self.state == "dead":
            return finish(False, self.error)
        self.queue.append((op, route, variables, optional, finish))
        self._pump()

    def _pump(self, fresh=False):
        """Send the calls for the route the page is up on; once nothing runs
        there, take the page to the route the first other call waits for. The
        calls that came for this route after one for another wait for its next
        turn, unless the page has just come up on it (`fresh`)."""
        if self.state != "ready":
            return
        away = [job for job in self.queue if job[1] and job[1] != self.route]
        if fresh or not away:
            here = [job for job in self.queue if not job[1] or job[1] == self.route]
            self.queue = away
            for job in here:
                self._send(job, time.monotonic() + ASKED)
        if away and not self.running and self.state == "ready":
            self._go(away[0][1])

    def _send(self, job, until):
        op, _, variables, optional, finish = job
        if self.state == "dead":
            return finish(False, self.error)
        key = object()
        self.running[key] = finish
        GLib = self.GLib
        request = json.dumps({"op": op, "variables": variables or {}, "optional": optional or {}, "wait": ASKED})
        arguments = GLib.Variant("a{sv}", {"req": GLib.Variant("s", request)})

        def done(view, result, _):
            wanted = self.running.pop(key, None)
            if wanted is None:   # the page ended meanwhile, and said so
                return
            try:
                raw = view.call_async_javascript_function_finish(result).to_string()
                said = json.loads(raw)
            except Exception:
                said = None
            if isinstance(said, dict) and said.get("stale") and time.monotonic() < until:
                self.running[key] = wanted

                def again():   # the page has not made that request yet: give it a moment
                    if self.running.pop(key, None) is None:   # the page ended meanwhile, and said so
                        return False
                    self._send(job, until)
                    return False
                self.GLib.timeout_add(400, again)
                return
            if said is None:
                wanted(False, SpotifyError("Spotify's page did not answer"))
            else:
                try:
                    answer = _answer_of(said)
                except SpotifyError as error:
                    if error.signed_out:   # Spotify no longer takes this sign-in: the next request reads Firefox's again
                        self._fail(error)
                    if isinstance(said, dict) and said.get("ok") and isinstance(said.get("shaped"), list):
                        error.shaped = tuple(said["shaped"])   # Spotify may be refusing what was set
                    wanted(False, error)
                else:
                    shaped = said.get("shaped") if isinstance(said.get("shaped"), list) else []
                    wanted(True, (answer, shaped))
            self._pump()
        self.view.call_async_javascript_function(CALL_JS, -1, arguments, None, None, None, done, None)

    def _fail(self, error):
        """The page is of no more use: every request waiting for it is answered with `error`."""
        if self.state == "dead":
            return
        self.state, self.error, self.token = "dead", error, None
        self._stop_timer()
        queue, running = self.queue, list(self.running.values())
        self.queue, self.running = [], {}
        for job in queue:
            job[-1](False, error)
        for finish in running:
            finish(False, error)
        self.GLib.idle_add(self._release)   # not from inside one of the view's own signals

    def close(self, now=False):
        """Stop using the page. `now`: free its view and folder at once, not at
        the main loop's next idle moment (the player is closing)."""
        self._fail(SpotifyError("Spotify's page was closed"))
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
        if self.holder is not None:
            try:
                self.holder.destroy()
            except Exception:
                pass
        shutil.rmtree(self.folder, ignore_errors=True)
        return False


def _answer_of(said):
    """What the call's own answer said, or a SpotifyError. Only a 401 means the
    sign-in is not accepted: a 403 is about what was asked (a page Spotify does
    not show in the account's country, say). An answer whose data holds nothing
    (GraphQL's errors in place of it, say) is not an empty library."""
    if not isinstance(said, dict):
        raise SpotifyError(CHANGED)
    if said.get("ok"):
        body = said.get("body")
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict) or all(value is None for value in data.values()):
            raise SpotifyError(CHANGED)
        return body
    status = said.get("status") if isinstance(said.get("status"), int) else 0
    if said.get("late"):
        raise SpotifyError("Spotify took too long to answer")
    if status == 401:
        raise SpotifyError(SIGNED_OUT, signed_out=True, status=status)
    if status == 403:
        raise SpotifyError("Spotify did not let Ocean Mist open that", status=403)
    if status == 404:
        raise SpotifyError("Spotify has no such page", status=404)
    if status == 429:
        raise SpotifyError("Spotify asked Ocean Mist to slow down; try again in a minute", status=429)
    raise SpotifyError(CHANGED if not status else f"Spotify did not answer (HTTP {status})", status=status)


# ---- the library, as rows for the page ----

def _at(value, *keys):
    """value[key][key]...: None where one is missing, or where what holds it is
    not an object (Spotify's answers carry null in place of many)."""
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _dict(value):
    return value if isinstance(value, dict) else {}


def _text(value, limit=300):
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _id(value):
    value = str(value) if isinstance(value, str) else ""
    if value.startswith("spotify:"):
        value = value.rsplit(":", 1)[-1]
    return value if ITEM_ID.match(value) else ""


def _year(value):
    found = re.match(r"(?:19|20)\d{2}", value if isinstance(value, str) else "")
    return found.group(0) if found else ""


def _seconds(millis):
    if isinstance(millis, bool) or not isinstance(millis, (int, float)):
        return 0
    return round(min(86400.0, max(0.0, millis / 1000)), 2)


def _cover(sources, size=ytmusic.COVER):
    """The address of a picture of the size wanted: Spotify's picture servers
    draw any size, and the sources carry theirs."""
    best, best_size = "", 0
    if not isinstance(sources, list):
        return ""
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get("url"), str):
            continue
        width = source.get("width") if isinstance(source.get("width"), int) else 0
        if not best or (width >= size and (best_size < size or width < best_size)) or (width and best_size < size and width > best_size):
            best, best_size = source["url"], width
    return best if PICTURE_HOSTS.match(best) else ""


def _first(items):
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]
    return {}


def _profile(res):
    """The name of the first artist of a row."""
    artists = res.get("artists") if isinstance(res, dict) else None
    first = _first(artists.get("items") if isinstance(artists, dict) else None)
    profile = first.get("profile") if isinstance(first.get("profile"), dict) else {}
    return _text(profile.get("name"))


def _row(kind, **fields):
    item = {"service": "spotify", "kind": kind, "title": "", "artist": "", "album": "", "year": "",
            "duration": 0, "label": "", "info": "", "cover": ""}
    item.update(fields)
    return item


def _songs_part(union):
    """The part of an album's answer that holds its songs: tracksV2, or tracks in the older shape."""
    for name in ("tracksV2", "tracks"):
        if isinstance(union.get(name), dict):
            return union[name]
    return {}


def song(res):
    """A song of a playlist (or of the library), as a row; None if it names nothing."""
    if not isinstance(res, dict):
        return None
    play = res.get("playability") if isinstance(res.get("playability"), dict) else {}
    if play.get("playable") is False:
        return None
    uri = res.get("uri") if isinstance(res.get("uri"), str) else ""
    ident = _id(uri) if uri.startswith("spotify:track:") else ""
    title = _text(res.get("name"))
    if not ident or not title:
        return None
    artist, album, year, millis, cover = "", "", "", 0, ""
    if isinstance(res.get("albumOfTrack"), dict):   # the modern shape, and the legacy one
        album_of = res["albumOfTrack"]
        artist = _profile(res)
        album = _text(album_of.get("name"))
        year = _year((album_of.get("date") or {}).get("isoString") if isinstance(album_of.get("date"), dict) else "")
        cover = _cover((album_of.get("coverArt") or {}).get("sources") if isinstance(album_of.get("coverArt"), dict) else None)
    elif not isinstance(res.get("identityTrait"), dict) and isinstance(res.get("artists"), dict):
        artist = _profile(res)   # a song of an album's page, which names no album of its own
    else:
        seen = res.get("identityTrait") if isinstance(res.get("identityTrait"), dict) else {}
        artist = _text(_first((seen.get("contributors") or {}).get("items", []) if isinstance(seen.get("contributors"), dict) else []).get("name"))
        parent = seen.get("contentHierarchyParent") if isinstance(seen.get("contentHierarchyParent"), dict) else {}
        album = _text((parent.get("identityTrait") or {}).get("name") if isinstance(parent.get("identityTrait"), dict) else "")
        year = _year(_at(parent, "publishingMetadataTrait", "firstPublishedAt", "isoString"))
        visual = res.get("visualIdentityTrait") if isinstance(res.get("visualIdentityTrait"), dict) else {}
        cover = _cover((visual.get("squareCoverImage") or {}).get("sources") if isinstance(visual.get("squareCoverImage"), dict) else None)
    if isinstance(res.get("trackDuration"), dict):
        millis = res["trackDuration"].get("totalMilliseconds")
    elif isinstance(res.get("duration"), dict):
        millis = res["duration"].get("totalMilliseconds")
    elif isinstance(res.get("consumptionExperienceTrait"), dict):
        seconds = _at(res, "consumptionExperienceTrait", "duration", "seconds")
        millis = seconds * 1000 if isinstance(seconds, (int, float)) else 0
    return _row("song", spotify=ident, title=title, artist=artist, album=album, year=year,
                duration=_seconds(millis), cover=cover)


def playlist(res):
    """A playlist of the library, as a row."""
    ident = _id(res.get("uri")) if isinstance(res, dict) else ""
    title = _text(res.get("name")) if isinstance(res, dict) else ""
    if not ident or not title:
        return None
    images = _first((res.get("images") or {}).get("items", []) if isinstance(res.get("images"), dict) else [])
    return _row("playlist", id=ident, title=title, label="Playlist",
                cover=_cover(images.get("sources")))


def album(res):
    """An album of the library, as a row."""
    ident = _id(res.get("uri")) if isinstance(res, dict) else ""
    title = _text(res.get("name")) if isinstance(res, dict) else ""
    if not ident or not title:
        return None
    cover = _cover((res.get("coverArt") or {}).get("sources") if isinstance(res.get("coverArt"), dict) else None)
    tracks = res.get("tracks") if isinstance(res.get("tracks"), dict) else {}
    count = tracks.get("totalCount")
    return _row("album", id=ident, title=title, artist=_profile(res), label="Album",
                year=_year((res.get("date") or {}).get("isoString") if isinstance(res.get("date"), dict) else ""),
                cover=cover, info=f"{count} {'song' if count == 1 else 'songs'}" if isinstance(count, int) else "")


def artist(res):
    """An artist of the library, as a row."""
    ident = _id(res.get("uri")) if isinstance(res, dict) else ""
    title = _text(_at(res, "profile", "name"))
    if not ident or not title:
        return None
    visuals = res.get("visuals") if isinstance(res.get("visuals"), dict) else {}
    avatar = visuals.get("avatarImage") if isinstance(visuals.get("avatarImage"), dict) else {}
    return _row("artist", id=ident, title=title, label="Artist", cover=_cover(avatar.get("sources")))


_SHAPES = {"playlists": playlist, "albums": album, "artists": artist}


def _songs_of(body):
    """The rows of one playlist answer (its content), how many items it held and
    its total count: a row Ocean Mist cannot use (an episode, a song that cannot
    play) is dropped, so the count of the items is what paging goes by."""
    data = body.get("data") if isinstance(body, dict) else None
    playlist = data.get("playlistV2") if isinstance(data, dict) else None
    content = playlist.get("content") if isinstance(playlist, dict) else None
    items = content.get("items") if isinstance(content, dict) else None
    items = items if isinstance(items, list) else []
    total = content.get("totalCount") if isinstance(content, dict) and isinstance(content.get("totalCount"), int) else None
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        wrapped = item.get("itemV2") if isinstance(item.get("itemV2"), dict) else item.get("itemV3")
        res = wrapped.get("data") if isinstance(wrapped, dict) else None
        row = song(res)
        if row:
            rows.append(row)
    return rows, total, len(items)


def song_id(link):
    """The Spotify song a saved playlist's entry names, or None."""
    link = str(link)
    ident = link[len(SONG_URL):] if link.startswith(SONG_URL) else ""
    return ident if ITEM_ID.match(ident) else None


# ---- the library ----

class SpotifyMusic:
    """What the player asks of Spotify. Methods are called from any thread but
    the GTK main one; they wait for the page's answer.

    `token_source()` gives the Spotify sign-in as firefox.SpotifyToken, or
    raises SpotifyError; it is called when the page has to be opened.
    `schedule(fn)` runs `fn` on the GTK main thread."""

    def __init__(self, token_source, schedule, site=SITE, folder=None):
        self.token_source, self.schedule = token_source, schedule
        self.site, self.folder = site, folder
        self.page = None          # the Page; touched on the main thread only
        self.last = 0.0
        self.generation = 0       # changes when you sign out: a request begun before is refused
        self.unfiltered = False   # Spotify refused the library's filter: the mixed list is read from then on
        self.pseudo = {}          # per kind: the rows of its first page that are not the kind's own (Liked Songs)

    # -- the page --
    def _ask(self, op, route, variables=None, optional=None):
        """Spotify's answer to the page's own request `op`, asked again on `route`
        with `variables`; with `optional` (see Page.call), (answer, the optional
        ones set). A view Spotify's page refused (see REFUSED) is replaced once."""
        try:
            return self._ask_once(op, route, variables, optional)
        except SpotifyError as error:
            if str(error) != REFUSED:
                raise
            return self._ask_once(op, route, variables, optional)

    def _ask_once(self, op, route, variables, optional):
        done, box = threading.Event(), {}
        generation = self.generation
        token = None if self.page is not None and self.page.state != "dead" else self.token_source()

        def finish(ok, value):
            box.update(ok=ok, value=value)
            done.set()

        def run():
            try:
                if generation != self.generation:   # you signed out meanwhile: no page is opened with the sign-in read before
                    raise SpotifyError(SIGNED_OUT, signed_out=True)
                self.last = time.monotonic()
                page = self.page
                if page is None or page.state == "dead":
                    page = self.page = Page(token or self.token_source(), self.site, self.folder)
                    self.watch(page)
                page.call(op, route, variables, finish, optional)
            except SpotifyError as error:
                finish(False, error)
            except Exception as error:   # a view that cannot be made, for one
                finish(False, SpotifyError(f"Ocean Mist could not open Spotify's page ({type(error).__name__})"))
        self.schedule(run)
        if not done.wait(ASKED * 3):
            raise SpotifyError("Spotify took too long to answer")
        if box["ok"]:
            answer, shaped = box["value"]
            return (answer, shaped) if optional is not None else answer
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
        """One page of the library: {"items": [...], "more": offset of the next
        page or None, "total": n, or None when only a mixed list could be read}."""
        if kind not in LIBRARY:
            raise SpotifyError("that is not part of your Spotify library")
        try:
            return self._library(kind, max(0, min(int(offset), 1_000_000)))
        except (AttributeError, KeyError, TypeError):   # an answer of a shape the parsing below did not expect
            raise SpotifyError(CHANGED) from None

    def _library(self, kind, offset):
        op, wrapper, chip = LIBRARY[kind]
        if kind == "songs":
            # the page asks for its first page of Liked Songs with one request
            # and later ones with another; Ocean Mist asks the same way
            if offset == 0:
                body = self._ask("fetchPlaylist", HOME, {"offset": 0, "limit": SONG_FIRST})
                size = SONG_FIRST
            else:
                body = self._ask(op, HOME, {"offset": offset, "limit": SONG_PAGE})
                size = SONG_PAGE
            rows, total, seen = _songs_of(body)
            more = None if seen < size or not seen or (isinstance(total, int) and offset + seen >= total) \
                else offset + seen
            return {"items": rows, "more": more, "total": total}
        shape, others = _SHAPES[kind], {LIBRARY[k][1] for k in _SHAPES if k != kind}
        rows = []
        for _ in range(SCAN):
            # the page's own request has no filter (its library opens on everything), but Spotify's takes
            # the one its chips send, which gives the kind's own list and count
            filtered = not self.unfiltered
            asked = {"offset": offset, "limit": PAGE}
            try:
                body = self._ask(op, HOME, dict(asked, filters=[chip]) if filtered else asked)
            except SpotifyError as error:
                if not filtered or str(error) != CHANGED:
                    raise
                # Spotify answered the filtered list with errors in place of it: the mixed list, for the
                # rest of the session (the rows kept by their kind, with no total)
                self.unfiltered, filtered = True, False
                body = self._ask(op, HOME, asked)
            page = _dict(_at(body, "data", "me", "libraryV3"))
            items = page.get("items") if isinstance(page.get("items"), list) else []
            total = page.get("totalCount") if isinstance(page.get("totalCount"), int) else None
            mixed, pseudo = False, 0
            for item in items:
                kept = _at(item, "item", "__typename")
                if kept != wrapper:
                    mixed = mixed or kept in others
                    pseudo += kept == PSEUDO   # Liked Songs (and Your Episodes), which the count includes
                    continue
                row = shape(_at(item, "item", "data"))
                if row:
                    rows.append(row)
            more = None if len(items) < PAGE or not items or (total is not None and offset + len(items) >= total) \
                else offset + len(items)
            if offset == 0:
                self.pseudo[kind] = pseudo
            # the kind's own list: asked with its filter, and no other kind came back
            own = filtered and not mixed
            if own or rows or more is None:
                break
            offset = more   # a mixed page with none of the kind: read on, so the kind does not look empty
        if own and total is not None:
            total = max(len(rows), total - self.pseudo.get(kind, 0))
        return {"items": rows, "more": more, "total": total if own else None}

    def page_of(self, kind, ident):
        """A playlist, an album or an artist, opened: its title and, for a playlist or an
        album, its songs; for an artist, its popular songs and its albums."""
        if kind not in ("playlist", "album", "artist") or not ITEM_ID.match(str(ident)):
            raise SpotifyError("that is not a page of your Spotify library")
        try:
            if kind == "album":
                return self._album_page(ident)
            if kind == "artist":
                return self._artist_page(ident)
            return self._playlist_page(ident)
        except (AttributeError, KeyError, TypeError):   # an answer of a shape the parsing below did not expect
            raise SpotifyError(CHANGED) from None

    def _album_page(self, ident):
        uri, route = f"spotify:album:{ident}", ALBUM + ident
        head = self._ask("getAlbum", route, {"uri": uri, "offset": 0, "limit": PAGE})
        data = head.get("data") if isinstance(head, dict) else None
        union = _dict(_at(data, "albumUnion"))
        tracks, offset = [], 0
        while len(tracks) < LIMIT:
            if offset:   # the first page has been asked already
                body = self._ask("getAlbum", route, {"uri": uri, "offset": offset, "limit": PAGE})
                data = body.get("data") if isinstance(body, dict) else None
                union = _dict(_at(data, "albumUnion"))
            page = _songs_part(union)
            items = page.get("items") if isinstance(page.get("items"), list) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                res = item.get("track") if isinstance(item.get("track"), dict) else item
                row = song(res)
                if row:
                    tracks.append(row)
            offset += PAGE
            if len(items) < PAGE:
                break
        title = _text(union.get("name"))
        artist = _profile(union)
        year = _year((union.get("date") or {}).get("isoString") if isinstance(union.get("date"), dict) else "")
        cover = _cover((union.get("coverArt") or {}).get("sources") if isinstance(union.get("coverArt"), dict) else None)
        if not cover:
            cover = next((t["cover"] for t in tracks if t["cover"]), "")
        for row in tracks:   # an album's songs do not name the album
            row["album"], row["year"] = row["album"] or title, row["year"] or year
            row["cover"], row["artist"] = row["cover"] or cover, row["artist"] or artist
        meta = _songs_part(union)
        total = meta.get("totalCount") if isinstance(meta.get("totalCount"), int) else None
        return {"service": "spotify", "kind": "album", "id": ident, "title": title or "Album",
                "artist": artist, "year": year,
                "label": "Album", "cover": cover, "tracks": tracks[:LIMIT], "groups": [],
                "info": f"{total} {'song' if total == 1 else 'songs'}" if isinstance(total, int) else ""}

    def _artist_page(self, ident):
        body = self._ask("queryArtistOverview", ARTIST + ident, {"uri": f"spotify:artist:{ident}"})
        union = _dict(_at(body, "data", "artistUnion"))
        title = _text((union.get("profile") or {}).get("name")) if isinstance(union.get("profile"), dict) else ""
        visuals = union.get("visuals") if isinstance(union.get("visuals"), dict) else {}
        avatar = visuals.get("avatarImage") if isinstance(visuals.get("avatarImage"), dict) else {}
        cover = _cover(avatar.get("sources"))
        discography = union.get("discography") if isinstance(union.get("discography"), dict) else {}
        top = []
        tops = _at(discography, "topTracks", "items")
        for item in tops if isinstance(tops, list) else []:
            res = item.get("track") if isinstance(item, dict) and isinstance(item.get("track"), dict) else item
            row = song(res)
            if row:
                top.append(row)
        albums = []
        groups = discography.get("albums") if isinstance(discography.get("albums"), dict) else {}
        groups = groups.get("items") if isinstance(groups.get("items"), list) else []
        for item in groups:
            row = None
            releases = (item.get("releases") or {}).get("items") if isinstance(item, dict) and isinstance(item.get("releases"), dict) else None
            if isinstance(releases, list) and releases:
                row = album(releases[0])
            elif isinstance(item, dict):
                row = album(item)
            if row:
                albums.append(row)
        if not albums:   # the page's own fallback list of an artist's releases
            releases = discography.get("popularReleasesAlbums") if isinstance(discography.get("popularReleasesAlbums"), dict) else {}
            for item in releases.get("items", []) if isinstance(releases.get("items"), list) else []:
                row = album(item)
                if row:
                    albums.append(row)
        groups = ([{"title": "Popular", "items": top}] if top else []) \
            + ([{"title": "Albums", "items": albums}] if albums else [])
        return {"service": "spotify", "kind": "artist", "id": ident, "title": title or "Artist",
                "artist": "", "year": "", "label": "Artist", "cover": cover, "tracks": [], "groups": groups,
                "info": f"{len(albums)} {'album' if len(albums) == 1 else 'albums'}" if albums else ""}

    def _playlist_page(self, ident):
        uri = f"spotify:playlist:{ident}"
        # a playlist's songs are the same request Liked Songs' are, with the
        # playlist's own address; the first page is asked the way the page asks
        # its own, and a second request is the fallback for an older shape
        op, size = "fetchPlaylistContents", SONG_PAGE
        try:
            body = self._ask(op, HOME, {"uri": uri, "offset": 0, "limit": size})
        except SpotifyError as error:
            if error.signed_out:
                raise
            op, size = "fetchPlaylist", SONG_FIRST
            body = self._ask(op, HOME, {"uri": uri, "offset": 0, "limit": size})
        tracks, offset = [], 0
        playlist = {}
        while len(tracks) < LIMIT:
            if offset:   # the first page has been asked already
                body = self._ask(op, HOME, {"uri": uri, "offset": offset, "limit": size})
            data = body.get("data") if isinstance(body, dict) else None
            playlist = _dict(_at(data, "playlistV2"))
            rows, _, seen = _songs_of(body)
            tracks += rows
            offset += size
            if seen < size:
                break
        images = _first((playlist.get("images") or {}).get("items", []) if isinstance(playlist.get("images"), dict) else [])
        cover = _cover(images.get("sources")) or next((t["cover"] for t in tracks if t["cover"]), "")
        return {"service": "spotify", "kind": "playlist", "id": ident, "title": _text(playlist.get("name")) or "Playlist",
                "artist": "", "year": "", "label": "Playlist", "cover": cover, "tracks": tracks[:LIMIT],
                "groups": [], "info": ""}


class _Stay(urllib.request.HTTPRedirectHandler):
    """A picture server that sends the request elsewhere is not followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not PICTURE_HOSTS.match(newurl):
            raise urllib.error.HTTPError(newurl, code, "that is not a Spotify picture", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_Stay)
_pictures, _pictures_lock = {}, threading.Lock()


def picture(url, cache=_pictures):
    """A cover's (type, bytes). Only Spotify's own picture servers are asked."""
    if not PICTURE_HOSTS.match(str(url)):
        raise SpotifyError("that is not a Spotify picture")
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
        raise SpotifyError(f"Spotify sent no picture (HTTP {error.code})") from None
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError):
        raise SpotifyError(OFFLINE) from None
    if not kind.startswith("image/") or not data:
        raise SpotifyError("Spotify sent no picture")
    with _pictures_lock:
        cache[url] = (kind, data)
        while len(cache) > PICTURES:
            del cache[next(iter(cache))]
    return kind, data


def main(argv):
    """Open Spotify's page with the Firefox sign-in and count the library."""
    import firefox
    from gi.repository import GLib
    found = firefox.spotify_signed_in()
    if not found:
        print("no Firefox profile has a Spotify sign-in; sign in at", firefox.SPOTIFY_SIGN_IN)
        return 1
    folder = argv[1] if len(argv) > 1 else found[0][0]
    if folder not in dict(found):
        print("that Firefox profile has no Spotify sign-in")
        return 1

    def token():
        token = firefox.spotify_token(folder)
        if token is None:
            raise SpotifyError(SIGNED_OUT, signed_out=True)
        return token

    spotify = SpotifyMusic(token, lambda fn: GLib.idle_add(lambda: fn() or False))
    loop, result = GLib.MainLoop(), {"code": 0}

    def work():
        began = time.monotonic()
        try:
            for kind in LIBRARY:
                got = spotify.library(kind)
                print(f"{kind}: {len(got['items'])} of {got['total']}, more: {got['more'] is not None}"
                      f" ({time.monotonic() - began:.1f} s)", flush=True)
        except SpotifyError as error:
            print("failed:", error, "(signed out)" if error.signed_out else "")
            result["code"] = 1
        finally:
            spotify.close()
            GLib.timeout_add(500, loop.quit)
    threading.Thread(target=work, daemon=True).start()
    loop.run()
    return result["code"]


if __name__ == "__main__":
    sys.exit(main(sys.argv))
