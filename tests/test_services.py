"""The streaming readers when a service is busy or has changed (#91): YouTube
Music's audio clients taken in turn, signed requests that follow no redirect,
a 429 waited for and asked once more, a page with no player failing with
"needs an update", Spotify's queue of pages you opened, one read of a list for
two searches, and Firefox's cookie file read off the GTK thread. Every answer
comes from a stand-in, a local server on 127.0.0.1 or an object in place of
the service's page: nothing is asked of any service."""
import http.server
import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import tests  # noqa: F401  (scratch folders first)

import apple
import spotify
import ytmusic
from apple import AppleError, AppleMusic
from spotify import SpotifyError, SpotifyMusic
from ytmusic import YTError, YTMusic

VIDEO = "Invented0_1"
MANIFESTS = "https://manifest.googlevideo.com/invented/"   # invented addresses, never asked
MASTER = MANIFESTS + "master.m3u8"


# ---- YouTube Music: the audio clients ----

def client(name, agent):
    return {"id": "99", "agent": agent, "client": {"clientName": name, "clientVersion": "1.0", "hl": "en"}}


FIRST, SECOND = client("INVENTED_FIRST", "Invented First/1.0"), client("INVENTED_SECOND", "Invented Second/1.0")


def adts(payload=b"\0" * 10):
    """One ADTS frame of AAC LC, 44.1 kHz, stereo, without a checksum."""
    length = 7 + len(payload)
    return bytes((0xFF, 0xF1, 1 << 6 | 4 << 2, 2 << 6 | length >> 11, length >> 3 & 0xFF,
                  (length & 7) << 5 | 0x1F, 0xFC)) + payload


class YouTube:
    """Stands in for YouTube: the player's answer for each client, the song's playlists and its segments."""

    def __init__(self, answers):
        self.answers = answers   # client name -> the player's answer
        self.asked = []          # (what, User-Agent or client name)

    def open(self, url, data=None, headers=None, **kwargs):
        headers = headers or {}
        if url == ytmusic.PLAYER_API:
            name = json.loads(data)["context"]["client"]["clientName"]
            self.asked.append(("player", name))
            return json.dumps(self.answers[name]).encode()
        self.asked.append((url.rsplit("/", 1)[-1], headers.get("User-Agent")))
        if url == MASTER:
            return (b'#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",DEFAULT=YES,URI="audio.m3u8"\n'
                    b'#EXT-X-STREAM-INF:BANDWIDTH=1,CODECS="mp4a.40.2",AUDIO="a"\nvideo.m3u8\n')
        if url == MANIFESTS + "audio.m3u8":
            return b"#EXTM3U\n#EXTINF:7.0,\nsegment0.ts\n#EXTINF:3.0,\nsegment1.ts\n#EXT-X-ENDLIST\n"
        if url.startswith(MANIFESTS + "segment"):
            return adts() * 3
        raise YTError(ytmusic._problem(404), general=True, code=404)


OK = {"playabilityStatus": {"status": "OK"}, "videoDetails": {"title": "Invented Song", "author": "Invented Band"},
      "streamingData": {"hlsManifestUrl": MASTER}}
BOT = {"playabilityStatus": {"status": "LOGIN_REQUIRED", "reason": "Sign in to confirm you're not a bot"}}


class AudioClients(unittest.TestCase):
    def resolve(self, music, youtube):
        with mock.patch.object(ytmusic, "AUDIO_CLIENTS", (FIRST, SECOND)), \
                mock.patch.object(ytmusic, "_open", youtube.open), \
                mock.patch.object(YTMusic, "_session", lambda self: ("1.0", "")):
            return music._resolve(VIDEO)

    def test_a_refused_client_gives_way_to_the_next_which_is_asked_first_after_that(self):
        music, youtube = YTMusic(), YouTube({"INVENTED_FIRST": BOT, "INVENTED_SECOND": OK})
        song = self.resolve(music, youtube)
        self.assertEqual(youtube.asked[:2], [("player", "INVENTED_FIRST"), ("player", "INVENTED_SECOND")])
        # the song's playlists and segments are asked for as the client that was given it
        self.assertEqual({agent for what, agent in youtube.asked[2:]}, {SECOND["agent"]})
        self.assertEqual((song.agent, song.config[1], len(song.segments)), (SECOND["agent"], 44100, 2))
        self.assertEqual(music.audio_from, 1)
        youtube.asked.clear()
        music.songs.clear()
        self.resolve(music, youtube)
        self.assertEqual(youtube.asked[0], ("player", "INVENTED_SECOND"))
        with mock.patch.object(ytmusic, "_open", youtube.open):
            music._segment(song, 1)
        self.assertEqual(youtube.asked[-1], ("segment1.ts", SECOND["agent"]))

    def test_every_client_refused_says_what_the_first_was_told(self):
        youtube = YouTube({"INVENTED_FIRST": BOT, "INVENTED_SECOND": dict(OK, streamingData={})})
        with self.assertRaises(YTError) as caught:
            self.resolve(YTMusic(), youtube)
        self.assertIn("not a bot", str(caught.exception))
        self.assertTrue(caught.exception.general)

    def test_a_refusal_of_the_song_itself_is_not_tried_on_another_client(self):
        for answer in ({"playabilityStatus": {"status": "LOGIN_REQUIRED", "reason": "This video is age-restricted"}},
                       {"playabilityStatus": {"status": "UNPLAYABLE", "reason": "Not available in your country"}},
                       {"playabilityStatus": {"status": "ERROR", "reason": "Video unavailable"}}):
            youtube = YouTube({"INVENTED_FIRST": answer, "INVENTED_SECOND": OK})
            with self.assertRaises(YTError):
                self.resolve(YTMusic(), youtube)
            self.assertEqual(youtube.asked, [("player", "INVENTED_FIRST")], answer)

    def test_slow_down_is_not_tried_on_another_client(self):
        def busy(url, data=None, headers=None, **kwargs):
            raise YTError(ytmusic._problem(429), general=True, code=429)
        music, asked = YTMusic(), []
        with mock.patch.object(ytmusic, "AUDIO_CLIENTS", (FIRST, SECOND)), \
                mock.patch.object(ytmusic, "_open", lambda *a, **k: asked.append(a[0]) or busy(*a, **k)), \
                mock.patch.object(YTMusic, "_session", lambda self: ("1.0", "")):
            with self.assertRaises(YTError) as caught:
                music._resolve(VIDEO)
        self.assertEqual((caught.exception.code, len(asked)), (429, 1))

    def test_the_command_line_names_the_client_and_no_address(self):
        with open(os.path.join(tests.ROOT, "ytmusic.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn('"client": AUDIO_CLIENTS[music.audio_from]["client"]["clientName"]', source)


# ---- YouTube Music: redirects and slowing down, against local servers ----

class Hits(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        server = self.server
        server.hits.append((self.path, self.headers.get("Cookie")))
        status, headers = server.answers.pop(0) if server.answers else (200, {})
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        body = b"invented answer"
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET   # (YouTube Music's own requests are POSTs)


def local_server(answers=()):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hits)
    server.hits, server.answers = [], list(answers)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class LocalServers(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()

    def serve(self, answers=()):
        server = local_server(answers)
        self.servers.append(server)
        return server, f"http://127.0.0.1:{server.server_address[1]}"


class Redirects(LocalServers):
    def test_a_signed_request_is_not_sent_on_with_its_cookie(self):
        elsewhere, there = self.serve()
        signed, here = self.serve([(302, {"Location": there + "/elsewhere"})])
        with self.assertRaises(YTError) as caught:
            ytmusic._open(here + "/signed", headers={"Cookie": "invented=1"}, follow=False)
        self.assertEqual(caught.exception.code, 302)
        self.assertEqual(signed.hits, [("/signed", "invented=1")])
        self.assertEqual(elsewhere.hits, [])

    def test_an_anonymous_request_still_follows_one(self):
        elsewhere, there = self.serve()
        _, here = self.serve([(302, {"Location": there + "/elsewhere"})])
        self.assertEqual(ytmusic._open(here + "/anonymous"), b"invented answer")
        self.assertEqual([path for path, _ in elsewhere.hits], ["/elsewhere"])

    def test_signed_in_a_redirect_means_signed_out_and_the_cookie_stays(self):
        elsewhere, there = self.serve()
        _, here = self.serve([(302, {"Location": there + "/elsewhere"})] * 2)

        class Session:
            def headers(self, site):
                return {"Cookie": "invented=1"}
        music = YTMusic()
        with mock.patch.object(ytmusic, "API", here + "/api/"), \
                mock.patch.object(YTMusic, "_session", lambda self: ("1.0", "")):
            with self.assertRaises(YTError) as caught:
                music._call("browse", {}, session=Session())
            self.assertTrue(caught.exception.signed_out)
            self.assertEqual(elsewhere.hits, [])
            with self.assertRaises(YTError) as caught:   # anonymous: followed, and the answer is not YouTube's
                music._call("browse", {})
            self.assertFalse(caught.exception.signed_out)
            self.assertEqual(elsewhere.hits, [("/elsewhere", None)])

    def test_a_picture_is_followed_only_to_youtubes_own_picture_servers(self):
        pictures = ytmusic._Pictures()
        request = urllib.request.Request("https://lh3.googleusercontent.com/invented")
        for away in ("http://lh3.googleusercontent.com/invented", "https://example.com/invented",
                     "http://127.0.0.1/invented"):
            with self.assertRaises(urllib.error.HTTPError, msg=away):
                pictures.redirect_request(request, None, 302, "Found", {}, away)
        self.assertEqual(pictures.redirect_request(request, None, 302, "Found", {},
                                                   "https://i.ytimg.com/vi/x/hqdefault.jpg").full_url,
                         "https://i.ytimg.com/vi/x/hqdefault.jpg")


class SlowDown(LocalServers):
    def test_a_short_wait_is_waited_and_asked_once_more(self):
        server, here = self.serve([(429, {"Retry-After": "1"})])
        began = time.monotonic()
        self.assertEqual(ytmusic._open(here + "/busy"), b"invented answer")
        self.assertGreaterEqual(time.monotonic() - began, 1)
        self.assertEqual(len(server.hits), 2)

    def test_asked_twice_it_stops(self):
        server, here = self.serve([(429, {"Retry-After": "0"}), (429, {"Retry-After": "0"})])
        with self.assertRaises(YTError) as caught:
            ytmusic._open(here + "/busy")
        self.assertEqual((caught.exception.code, len(server.hits)), (429, 2))

    def test_a_long_wait_is_not_waited(self):
        server, here = self.serve([(429, {"Retry-After": "60"})])
        began = time.monotonic()
        with self.assertRaises(YTError) as caught:
            ytmusic._open(here + "/busy")
        self.assertLess(time.monotonic() - began, 5)
        self.assertEqual((caught.exception.code, len(server.hits)), (429, 1))

    def test_other_refusals_are_not_asked_again(self):
        server, here = self.serve([(503, {"Retry-After": "0"})])
        with self.assertRaises(YTError):
            ytmusic._open(here + "/down")
        self.assertEqual(len(server.hits), 1)


class HomePage(unittest.TestCase):
    def test_without_the_home_page_the_known_version_is_used(self):
        music = YTMusic()
        with mock.patch.object(ytmusic, "_open", mock.Mock(side_effect=YTError(ytmusic.OFFLINE, general=True))):
            self.assertEqual(music._session(), (ytmusic.WEB_VERSION, ""))

    def test_one_thread_reads_it_and_the_others_wait_for_it(self):
        music, read = YTMusic(), []

        def page(url, **kwargs):
            read.append(url)
            time.sleep(0.3)
            return b'"INNERTUBE_CLIENT_VERSION":"1.23","VISITOR_DATA":"Invented"'
        found = []
        with mock.patch.object(ytmusic, "_open", page):
            threads = [threading.Thread(target=lambda: found.append(music._session())) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(len(read), 1)
        self.assertEqual(found, [("1.23", "Invented")] * 4)


# ---- Spotify: the queue of pages, and one read for two searches ----

def spotify_page():
    """A Page without its view: only the queue, as it is while the page is loading."""
    page = object.__new__(spotify.Page)
    page.state, page.queue, page.running, page.route = "loading", [], {}, None
    return page


class SpotifyQueue(unittest.TestCase):
    def test_a_page_you_opened_gives_way_to_the_next_while_it_waits(self):
        page, told = spotify_page(), {}

        def finish(name):
            return lambda ok, value: told.setdefault(name, value)
        album = lambda n: spotify.ALBUM + f"Invented{n}"
        for n in range(1, 6):   # five quick clicks
            page.call("getAlbum", album(n), {}, finish(n), opened=True)
        self.assertEqual([job[1] for job in page.queue], [album(5)])
        self.assertEqual(sorted(told), [1, 2, 3, 4])
        self.assertEqual({error.reason for error in told.values()}, {"passed"})

    def test_a_page_played_or_added_or_the_library_is_never_dropped(self):
        page, told = spotify_page(), []
        page.call("fetchLibraryTracks", spotify.HOME, {}, lambda ok, value: told.append(value))
        page.call("getAlbum", spotify.ALBUM + "Played", {}, lambda ok, value: told.append(value))
        page.call("getAlbum", spotify.ALBUM + "Opened", {}, lambda ok, value: told.append(value), opened=True)
        page.call("queryArtistOverview", spotify.ARTIST + "Opened", {}, lambda ok, value: told.append(value), opened=True)
        self.assertEqual([job[1] for job in page.queue],
                         [spotify.HOME, spotify.ALBUM + "Played", spotify.ARTIST + "Opened"])
        self.assertEqual(len(told), 1)

    def test_the_page_the_view_is_on_its_way_to_is_answered_all_the_same(self):
        page, told = spotify_page(), []
        page.route = spotify.ALBUM + "Loading"   # (its load has begun: answering it costs nothing more)
        page.call("getAlbum", spotify.ALBUM + "Loading", {}, lambda ok, value: told.append(value), opened=True)
        page.call("getAlbum", spotify.ALBUM + "Passed", {}, lambda ok, value: told.append(value), opened=True)
        page.call("getAlbum", spotify.ALBUM + "Last", {}, lambda ok, value: told.append(value), opened=True)
        self.assertEqual([job[1] for job in page.queue], [spotify.ALBUM + "Loading", spotify.ALBUM + "Last"])
        self.assertEqual([error.reason for error in told], ["passed"])

    def test_more_of_the_same_page_is_kept(self):
        page = spotify_page()
        for offset in (0, 50):
            page.call("getAlbum", spotify.ALBUM + "Same", {"offset": offset}, lambda ok, value: None, opened=True)
        self.assertEqual(len(page.queue), 2)


def rows(start, count):
    return [{"title": f"Invented {n}", "artist": "Invented Band", "album": "Invented Album"}
            for n in range(start, start + count)]


class SpotifyWholeList(unittest.TestCase):
    def setUp(self):
        self.music = SpotifyMusic(token_source=None, schedule=None)
        self.asked, self.answers = [], []
        patcher = mock.patch.object(spotify, "PAUSE", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

        def library(kind, offset=0):
            self.asked.append(offset)
            time.sleep(0.05)
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        self.music.library = library

    def busy(self):
        return SpotifyError("Spotify asked Ocean Mist to slow down; try again in a minute", status=429)

    def test_two_searches_at_once_read_the_list_once(self):
        self.answers += [{"items": rows(0, 50), "more": 50}, {"items": rows(50, 10), "more": None}]
        found = []
        threads = [threading.Thread(target=lambda: found.append(self.music.search("albums", "invented")))
                   for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.asked, [0, 50])
        self.assertEqual([(r["total"], r["whole"]) for r in found], [(60, True)] * 2)

    def test_asked_to_slow_down_it_asks_once_more(self):
        self.answers += [{"items": rows(0, 50), "more": 50}, self.busy(), {"items": rows(50, 10), "more": None}]
        self.assertEqual(self.music.search("albums", "invented")["total"], 60)
        self.assertEqual(self.asked, [0, 50, 50])

    def test_asked_twice_the_rows_read_stay_and_are_not_kept(self):
        self.answers += [{"items": rows(0, 50), "more": 50}, self.busy(), self.busy()]
        found = self.music.search("albums", "invented")
        self.assertEqual((found["total"], found["read"], found["whole"]), (50, 50, False))
        self.answers += [{"items": rows(0, 50), "more": None}]   # the next search reads it again
        self.assertEqual(self.music.search("albums", "invented")["whole"], True)

    def test_asked_twice_with_nothing_read_it_says_so(self):
        self.answers += [self.busy(), self.busy()]
        with self.assertRaises(SpotifyError) as caught:
            self.music.search("albums", "invented")
        self.assertEqual(caught.exception.status, 429)

    def test_a_page_with_no_player_does_not_switch_the_library_filter_off(self):
        music = SpotifyMusic(token_source=None, schedule=None)
        answers = [SpotifyError(f"{spotify.CHANGED} (no-player)", reason="no-player")]
        music._ask = lambda *args, **kwargs: (_ for _ in ()).throw(answers.pop(0))
        with self.assertRaises(SpotifyError):
            music.library("albums")
        self.assertFalse(music.unfiltered)


# ---- Apple Music: long reads ----

def apple_song(n):
    return {"id": f"i.{n:06d}", "type": "library-songs",
            "attributes": {"name": f"Invented {n}", "artistName": "Invented Band", "albumName": "Invented Album",
                           "playParams": {"catalogId": f"{n:09d}"}, "durationInMillis": 200000}}


class AppleLongRead(unittest.TestCase):
    def setUp(self):
        self.music = AppleMusic(token_source=None, schedule=None)
        self.asked, self.answers = [], []
        patcher = mock.patch.object(apple, "PAUSE", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

        def ask(path, query=None):
            self.asked.append((path, (query or {}).get("offset")))
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        self.music._ask = ask

    def busy(self):
        return AppleError("Apple Music asked Ocean Mist to slow down; try again in a minute", status=429)

    def head(self):
        return {"data": [{"id": "p.Invented", "attributes": {"name": "Invented Playlist"}}]}

    def test_asked_to_slow_down_it_asks_once_more(self):
        self.answers += [self.head(), {"data": [apple_song(n) for n in range(100)], "next": "more"}, self.busy(),
                         {"data": [apple_song(n) for n in range(100, 120)]}]
        page = self.music.page_of("playlist", "p.Invented")
        self.assertEqual((len(page["tracks"]), page["notice"]), (120, ""))
        self.assertEqual([offset for _, offset in self.asked[1:]], [0, 100, 100])

    def test_asked_twice_the_songs_read_stay_with_a_notice(self):
        self.answers += [self.head(), {"data": [apple_song(n) for n in range(100)], "next": "more"}, self.busy(),
                         self.busy()]
        page = self.music.page_of("playlist", "p.Invented")
        self.assertEqual(len(page["tracks"]), 100)
        self.assertEqual(page["notice"], apple.SLOWED)

    def test_asked_twice_with_nothing_read_it_says_so(self):
        self.answers += [self.head(), self.busy(), self.busy()]
        with self.assertRaises(AppleError) as caught:
            self.music.page_of("playlist", "p.Invented")
        self.assertEqual(caught.exception.status, 429)

    def test_the_page_reads_a_page_cut_short_again(self):
        with open(os.path.join(tests.ROOT, "ui", "premium.js"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("!yt.pages[node.value].notice", source)
        self.assertIn("page.notice];", source)


# ---- a page with no player: "needs an update", in MISSING seconds ----

class Answer:
    def __init__(self, said):
        self.said = said

    def to_string(self):
        return json.dumps(self.said)


class View:
    """The view's part of a look at the page: what READY_JS says."""

    def __init__(self, said):
        self.said = said

    def evaluate_javascript(self, script, length, world, source, cancel, callback, data):
        callback(self, Answer(self.said), data)

    def evaluate_javascript_finish(self, result):
        return result


class Clock:
    """GLib's part: the next look is noted, not made."""

    def __init__(self):
        self.later = []

    def timeout_add(self, ms, fn, *args):
        self.later.append(fn)

    def idle_add(self, fn, *args):
        pass

    def source_remove(self, source):
        pass


def looking(page, said, loaded):
    page.view, page.GLib, page.expiry = View(said), Clock(), None
    page.state, page.error, page.token = "loading", None, None
    page.deadline = time.monotonic() + 60
    page.loaded, page.seen = time.monotonic() - loaded, None
    return page


class NoPlayer(unittest.TestCase):
    def apple(self, said, loaded):
        page = looking(object.__new__(apple.Page), said, loaded)
        page.site, page.waiting, page.running = "https://music.apple.localhost/", [], {}
        page._poll()
        return page

    def spotify(self, said, loaded):
        page = looking(object.__new__(spotify.Page), said, loaded)
        page.site, page.queue, page.running = "https://spotify.localhost/", [], {}
        page.going, page.committed, page.doc = 1, True, "before"
        page._poll(1)
        return page

    def test_apple_without_musickit_once_loaded_needs_an_update(self):
        page = self.apple({"host": "music.apple.localhost", "api": False, "signed": False}, apple.MISSING + 1)
        self.assertEqual((page.state, page.error.reason), ("dead", "no-musickit"))
        self.assertIn("needs an update", str(page.error))

    def test_apple_is_waited_for_until_then_and_signed_out_is_still_signed_out(self):
        page = self.apple({"host": "music.apple.localhost", "api": False, "signed": False}, apple.MISSING - 5)
        self.assertEqual((page.state, len(page.GLib.later)), ("loading", 1))
        page = self.apple({"host": "music.apple.localhost", "api": True, "signed": False}, apple.MISSING + 1)
        self.assertEqual(page.state, "loading")   # the player is up: signed out is told after SETTLE
        page = self.apple({"host": "idmsa.apple.localhost", "api": False, "signed": False}, apple.MISSING + 1)
        self.assertTrue(page.error.signed_out)   # sent to a sign-in page

    def test_spotify_without_its_markers_once_loaded_needs_an_update(self):
        page = self.spotify({"host": "spotify.localhost", "doc": "new", "up": False}, spotify.MISSING + 1)
        self.assertEqual((page.state, page.error.reason), ("dead", "no-player"))
        self.assertIn("needs an update", str(page.error))

    def test_spotify_is_waited_for_until_then(self):
        page = self.spotify({"host": "spotify.localhost", "doc": "new", "up": False}, spotify.MISSING - 5)
        self.assertEqual((page.state, len(page.GLib.later)), ("loading", 1))
        page = self.spotify({"host": "spotify.localhost", "doc": "new", "up": True}, spotify.MISSING + 1)
        self.assertEqual(page.state, "loading")
        page = self.spotify({"host": "accounts.spotify.localhost", "doc": "new", "up": False}, spotify.MISSING + 1)
        self.assertTrue(page.error.signed_out)


# ---- Firefox's cookie file, read off the GTK thread ----

class GtkThread:
    """Runs what is scheduled on a thread of its own, as the GTK main loop would; `before` runs first, once."""

    def __init__(self, before=None):
        self.before, self.threads = before, set()

    def __call__(self, fn):
        def run():
            self.threads.add(threading.get_ident())
            if self.before:
                before, self.before = self.before, None
                before()
            fn()
        threading.Thread(target=run, daemon=True).start()


class Opened:
    """A library page in place of apple.Page or spotify.Page: it answers every call at once."""
    made = []

    def __init__(self, token, *args):
        self.token, self.state = token, "ready"
        Opened.made.append(self)

    @staticmethod
    def new_context():
        return object()

    def call(self, *args):
        finish = args[2] if len(args) == 3 else args[3]   # Apple's (path, query, finish); Spotify's (op, route, variables, finish, ...)
        finish(True, {"data": {"invented": 1}} if len(args) == 3 else ({"data": {"invented": 1}}, []))


class CookiesOffTheGtkThread(unittest.TestCase):
    def check(self, module, reader, ask):
        read_on = []

        def token_source():
            read_on.append(threading.get_ident())
            return "invented sign-in"
        Opened.made = []
        gone = Opened("an earlier sign-in")

        def page_goes():   # the page ends between the request's look at it and the GTK thread's
            gone.state = "dead"
        gtk = GtkThread(before=page_goes)
        music = reader(token_source, gtk)
        music.page, music.watch = gone, lambda page: None
        with mock.patch.object(module, "Page", Opened):
            ask(music)
        self.assertEqual(read_on, [threading.get_ident()])   # on the request's own thread, once
        self.assertFalse(set(read_on) & gtk.threads)
        self.assertEqual([page.token for page in Opened.made[1:]], ["invented sign-in"])

    def test_apple(self):
        self.check(apple, lambda source, schedule: AppleMusic(source, schedule),
                   lambda music: music._ask("/v1/me/library/songs"))

    def test_spotify(self):
        self.check(spotify, lambda source, schedule: SpotifyMusic(source, schedule),
                   lambda music: music._ask("fetchLibraryTracks", spotify.HOME))


if __name__ == "__main__":
    unittest.main()
