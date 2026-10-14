"""YouTube Music, spoken to directly: search, albums, artists, playlists,
similar songs, covers and the audio of a song. Standard library only, and
no account: nothing is signed in.

These are the requests YouTube Music's own apps make, not a published API,
so YouTube can change them at any time. What may need updating then is kept
together below: the web client's version (read from the site itself, with a
fallback), the client asked for a song's audio, and the search filters.

A song's audio comes as HLS: AAC in segments of about seven seconds. Each
segment is repacked here as a fragment of MP4, which the page feeds to the
browser's Media Source (WebKitGTK does not play HLS itself).

    python3 ytmusic.py search [all|songs|albums|artists|playlists] WORDS...
    python3 ytmusic.py album|artist|playlist BROWSE_ID
    python3 ytmusic.py radio|audio VIDEO_ID
"""
import gzip
import http.client
import json
import re
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SITE = "https://music.youtube.com"
API = SITE + "/youtubei/v1/"
PLAYER_API = "https://www.youtube.com/youtubei/v1/player?prettyPrint=false"
WATCH = SITE + "/watch?v="
BROWSER = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/605.1.15 "
           "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")
WEB_VERSION = "1.20261011.16.00"   # when the site does not say: its version on 2026-10-14
# asked for a song's audio. In October 2026 the visionOS app got whole songs
# as HLS with no PO token and no signature to work out; the direct links the
# Android VR app got stopped after the first minute of a song.
AUDIO_CLIENT = {
    "id": "101",
    "agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_3) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/26.0 Safari/605.1.15"),
    "client": {"clientName": "VISIONOS", "clientVersion": "1.02", "deviceMake": "Apple",
               "deviceModel": "RealityDevice17,1", "osName": "visionOS",
               "osVersion": "26.5.23O471", "hl": "en"},
}
AUDIO_CODECS = ("mp4a.40.2", "mp4a.40.5")   # AAC about 130 kbit/s, then HE-AAC about 50
# the "params" of the site's search filter chips; every search without a
# filter reads the current ones back from the chips it comes with
FILTERS = {
    "songs": "EgWKAQIIAWoSEAUQCRADEAoQDhAEEBAQFRAR",
    "videos": "EgWKAQIQAWoSEAUQCRADEAoQDhAEEBAQFRAR",
    "albums": "EgWKAQIYAWoSEAUQCRADEAoQDhAEEBAQFRAR",
    "artists": "EgWKAQIgAWoSEAUQCRADEAoQDhAEEBAQFRAR",
    "featured": "EgeKAQQoADgBahIQBRAJEAMQChAOEAQQEBAVEBE%3D",
    "community": "EgeKAQQoAEABahIQBRAJEAMQChAOEAQQEBAVEBE%3D",
}
CHIPS = {"Songs": "songs", "Videos": "videos", "Albums": "albums", "Artists": "artists",
         "Featured playlists": "featured", "Community playlists": "community"}
KINDS = ("all", "songs", "albums", "artists", "playlists")
COVER = 544                 # pixels: the cover box, the album art pane and the desktop's controls
PLAYLIST_LIMIT = 1000       # songs read from one playlist
PIECES = 32                 # repacked segments kept in memory (about 3.5 MB)
PICTURES = 64               # covers kept in memory
RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350)

OFFLINE = "cannot reach YouTube Music; check the internet connection"
CHANGED = "YouTube Music changed something Ocean Mist depends on, so the player needs an update"

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
BROWSE_ID = re.compile(r"^[A-Za-z0-9_-]{2,100}$")
TOKEN = re.compile(r"^[A-Za-z0-9_%=-]{8,4000}$")
PICTURE_HOSTS = re.compile(r"^https://(?:[a-z0-9-]+\.)*(?:googleusercontent|ggpht|ytimg)\.com/", re.I)
AUDIO_HOSTS = re.compile(r"^https://(?:[a-z0-9-]+\.)*(?:googlevideo|youtube)\.com/", re.I)
DURATION = re.compile(r"^(?:\d+:)?\d{1,2}:\d{2}$")
YEAR = re.compile(r"^(?:19|20)\d{2}$")
COUNT = re.compile(r"\d.*\b(?:views?|plays?|songs?|tracks?|subscribers?|audience|listeners?|episodes?)\b", re.I)
LABELS = {"Song", "Video", "Album", "Single", "EP", "Artist", "Playlist"}
NOT_MUSIC = {"Episode", "Podcast", "Profile", "Station", "Audiobook"}
PAGES = {"MUSIC_PAGE_TYPE_ALBUM": "album", "MUSIC_PAGE_TYPE_ARTIST": "artist",
         "MUSIC_PAGE_TYPE_PLAYLIST": "playlist"}
PEOPLE = ("MUSIC_PAGE_TYPE_ARTIST", "MUSIC_PAGE_TYPE_USER_CHANNEL")
SEPARATOR = {"text": " • "}


class YTError(Exception):
    """What went wrong, worded for the status line. `general` when every
    song would fail the same way, so the player stops instead of skipping."""

    def __init__(self, message, general=False, code=0):
        super().__init__(message)
        self.general, self.code = general, code


class Song:
    """A song's audio as YouTube streams it: AAC in segments of about seven
    seconds, at addresses good for about six hours."""

    def __init__(self, video, segments, lengths, expires, details):
        self.video, self.segments, self.lengths = video, segments, lengths
        self.expires, self.details = expires, details
        self.base = None       # the first segment's clock, in 90 kHz ticks
        self.config = None     # (AAC profile, sample rate, channels)

    def info(self):
        """What the page needs to play it: its length, where each segment
        starts (as the playlist says, to within a frame) and the type."""
        starts, at = [], 0.0
        for length in self.lengths:
            starts.append(round(at, 3))
            at += length
        return {"duration": round(at, 3), "starts": starts,
                "type": f'audio/mp4; codecs="mp4a.40.{self.config[0] + 1}"',
                "details": self.details}


def video_id(link):
    """The song a YouTube or YouTube Music link points to, or None."""
    try:
        parts = urllib.parse.urlsplit(str(link).strip())
    except ValueError:
        return None
    host = parts.netloc.lower().rsplit("@", 1)[-1].split(":")[0]
    if host in ("music.youtube.com", "www.youtube.com", "youtube.com", "m.youtube.com"):
        found = urllib.parse.parse_qs(parts.query).get("v", [""])[0] if parts.path == "/watch" else ""
    elif host == "youtu.be":
        found = parts.path.strip("/")
    else:
        return None
    return found if VIDEO_ID.match(found) else None


# ---- reading the answers ----
# The answers are deep trees of "renderers". Everything below reads them
# defensively: a missing branch gives an empty value, never an exception.

def _dig(node, *path):
    for key in path:
        if isinstance(node, dict):
            node = node.get(key)
        elif isinstance(node, list) and isinstance(key, int) and -len(node) <= key < len(node):
            node = node[key]
        else:
            return None
    return node


def _runs(node):
    runs = _dig(node, "runs")
    return [run for run in runs if isinstance(run, dict)] if isinstance(runs, list) else []


def _text(node):
    return "".join(str(run.get("text", "")) for run in _runs(node)).strip()


def _browse(endpoint):
    """(browse id, page type) of a navigation endpoint."""
    browse = _dig(endpoint, "browseEndpoint") or {}
    page = _dig(browse, "browseEndpointContextSupportedConfigs",
                "browseEndpointContextMusicConfig", "pageType")
    return browse.get("browseId") or "", page or ""


def _watch(endpoint):
    """(video id, music video type) of a navigation endpoint."""
    watch = _dig(endpoint, "watchEndpoint") or {}
    kind = _dig(watch, "watchEndpointMusicSupportedConfigs", "watchEndpointMusicConfig",
                "musicVideoType")
    return watch.get("videoId") or "", kind or ""


def _cover(thumbnails, size=COVER):
    """The address of the biggest picture, asked for at the size wanted:
    Google's picture servers draw any size written into the address."""
    pictures = [t for t in thumbnails or [] if isinstance(t, dict) and t.get("url")]
    if not pictures:
        return ""
    url = max(pictures, key=lambda t: t.get("width") or 0)["url"]
    if url.startswith("//"):
        url = "https:" + url
    if not PICTURE_HOSTS.match(url):
        return ""
    if "ytimg.com" not in url:
        url = re.sub(r"=w\d+-h\d+(?=-|$)", f"=w{size}-h{size}", url)
        url = re.sub(r"=s\d+(?=-|$)", f"=s{size}", url)
    return url


def _seconds(text):
    total = 0
    for part in text.split(":"):
        total = total * 60 + int(part)
    return total


def _details(runs):
    """What a result's grey lines say: its type, artists, album, year,
    length, and counts like "35 songs"."""
    found = {"label": "", "artist": "", "artist_id": "", "album": "", "album_id": "",
             "year": "", "duration": 0, "info": []}
    groups, group = [], []
    for run in runs:
        if str(run.get("text", "")).strip() == "•":
            groups.append(group)
            group = []
        else:
            group.append(run)
    groups.append(group)
    loose = []
    for n, group in enumerate(groups):
        text = "".join(str(run.get("text", "")) for run in group).strip()
        if not text:
            continue
        links = [(str(run.get("text", "")).strip(), _browse(run.get("navigationEndpoint")))
                 for run in group]
        people = [(name, ids[0]) for name, ids in links if ids[1] in PEOPLE]
        album = next(((name, ids[0]) for name, ids in links
                      if ids[1] == "MUSIC_PAGE_TYPE_ALBUM"), None)
        if album:
            found["album"], found["album_id"] = album
        elif people and not found["artist"]:
            found["artist"], found["artist_id"] = text, people[0][1]   # "A, B & C" as written
        elif n == 0 and (text in LABELS or text in NOT_MUSIC):
            found["label"] = text
        elif DURATION.match(text) and not found["duration"]:
            found["duration"] = _seconds(text)
        elif YEAR.match(text) and not found["year"]:
            found["year"] = text
        else:
            loose.append(text)
    for text in loose:   # an artist or an author without a link: the first plain name
        if not found["artist"] and not COUNT.search(text):
            found["artist"] = text
        elif not text.lower().endswith(" plays"):
            found["info"].append(text)
    return found


def _item(kind, title, found, video="", browse_id="", thumbnails=None):
    item = {"kind": kind, "title": title, "artist": found["artist"],
            "artist_id": found["artist_id"], "album": found["album"],
            "album_id": found["album_id"], "year": found["year"],
            "duration": found["duration"], "label": found["label"],
            "info": " · ".join(found["info"]), "cover": _cover(thumbnails)}
    if video:
        item["video"] = video
    else:
        item["id"] = browse_id
    return item


def _kind(found, page, video, video_type):
    """song, video, album, artist or playlist; None for what is not music."""
    if found["label"] in NOT_MUSIC or video_type == "MUSIC_VIDEO_TYPE_PODCAST_EPISODE":
        return None
    if page:
        return PAGES.get(page)
    if video:
        return "video" if found["label"] == "Video" else "song"
    return None


def _list_item(renderer):
    """A row of a list: musicResponsiveListItemRenderer."""
    columns = [_runs(_dig(column, "musicResponsiveListItemFlexColumnRenderer", "text"))
               for column in renderer.get("flexColumns") or []]
    columns += [_runs(_dig(column, "musicResponsiveListItemFixedColumnRenderer", "text"))
                for column in renderer.get("fixedColumns") or []]
    if not columns:
        return None
    title = "".join(str(run.get("text", "")) for run in columns[0]).strip()
    video, video_type = _dig(renderer, "playlistItemData", "videoId") or "", ""
    play = _dig(renderer, "overlay", "musicItemThumbnailOverlayRenderer", "content",
                "musicPlayButtonRenderer", "playNavigationEndpoint")
    for endpoint in [run.get("navigationEndpoint") for run in columns[0]] + [play]:
        found_video, found_type = _watch(endpoint)
        video, video_type = video or found_video, video_type or found_type
    browse_id, page = _browse(renderer.get("navigationEndpoint"))
    found = _details([run for column in columns[1:] for run in column + [SEPARATOR]])
    kind = _kind(found, page, video, video_type)
    if kind is None or not title:
        return None
    item = _item(kind, title, found, video if kind in ("song", "video") else "", browse_id,
                 _dig(renderer, "thumbnail", "musicThumbnailRenderer", "thumbnail", "thumbnails"))
    number = _text(renderer.get("index"))
    if number.isdigit():
        item["track"] = int(number)
    playlist = _dig(play, "watchPlaylistEndpoint", "playlistId")
    if playlist and kind in ("album", "playlist"):
        item["playlist"] = playlist
    return item


def _two_row(renderer):
    """A tile of a carousel: musicTwoRowItemRenderer."""
    title = _text(renderer.get("title"))
    browse_id, page = _browse(renderer.get("navigationEndpoint"))
    video, video_type = _watch(renderer.get("navigationEndpoint"))
    found = _details(_runs(renderer.get("subtitle")))
    kind = _kind(found, page, video, video_type)
    if kind is None or not title:
        return None
    return _item(kind, title, found, video if kind in ("song", "video") else "", browse_id,
                 _dig(renderer, "thumbnailRenderer", "musicThumbnailRenderer", "thumbnail",
                      "thumbnails"))


def _panel_item(entry):
    """A song of a radio or up-next list: playlistPanelVideoRenderer."""
    renderer = entry.get("playlistPanelVideoRenderer") or _dig(
        entry, "playlistPanelVideoWrapperRenderer", "primaryRenderer", "playlistPanelVideoRenderer")
    if not isinstance(renderer, dict) or not VIDEO_ID.match(str(renderer.get("videoId") or "")):
        return None
    found = _details(_runs(renderer.get("longBylineText")))
    length = _text(renderer.get("lengthText"))
    if DURATION.match(length):
        found["duration"] = _seconds(length)
    title = _text(renderer.get("title"))
    _, video_type = _watch(renderer.get("navigationEndpoint"))
    if not title or video_type == "MUSIC_VIDEO_TYPE_PODCAST_EPISODE":
        return None
    return _item("song", title, found, renderer["videoId"], "",
                 _dig(renderer, "thumbnail", "thumbnails"))


def _items(contents):
    """The music in a shelf's contents, and the token for more of it."""
    items, token = [], None
    for entry in contents or []:
        if not isinstance(entry, dict):
            continue
        item = None
        if "musicResponsiveListItemRenderer" in entry:
            item = _list_item(entry["musicResponsiveListItemRenderer"])
        elif "musicTwoRowItemRenderer" in entry:
            item = _two_row(entry["musicTwoRowItemRenderer"])
        elif "continuationItemRenderer" in entry:
            token = _dig(entry, "continuationItemRenderer", "continuationEndpoint",
                         "continuationCommand", "token")
        if item:
            items.append(item)
    return items, token


def _shelf_token(shelf):
    return _dig(shelf, "continuations", 0, "nextContinuationData", "continuation")


def _sections(data):
    """The sections of a page, in either of its layouts."""
    found = []
    for path in (("contents", "twoColumnBrowseResultsRenderer", "secondaryContents"),
                 ("contents", "twoColumnBrowseResultsRenderer", "tabs", 0, "tabRenderer", "content"),
                 ("contents", "singleColumnBrowseResultsRenderer", "tabs", 0, "tabRenderer", "content")):
        found += _dig(data, *path, "sectionListRenderer", "contents") or []
    return [section for section in found if isinstance(section, dict)]


def _header(data):
    """The header of an album, artist or playlist page, whichever layout it has."""
    for section in _sections(data):
        if "musicResponsiveHeaderRenderer" in section:
            return section["musicResponsiveHeaderRenderer"]
        editable = section.get("musicEditablePlaylistDetailHeaderRenderer")
        if editable:
            return _dig(editable, "header", "musicResponsiveHeaderRenderer") or {}
    header = data.get("header") or {}
    for key in ("musicDetailHeaderRenderer", "musicImmersiveHeaderRenderer",
                "musicVisualHeaderRenderer"):
        if key in header:
            return header[key]
    return _dig(header, "musicEditablePlaylistDetailHeaderRenderer", "header",
                "musicDetailHeaderRenderer") or {}


def _header_cover(header):
    for path in (("thumbnail", "musicThumbnailRenderer", "thumbnail", "thumbnails"),
                 ("thumbnail", "croppedSquareThumbnailRenderer", "thumbnail", "thumbnails"),
                 ("foregroundThumbnail", "musicThumbnailRenderer", "thumbnail", "thumbnails")):
        found = _cover(_dig(header, *path))
        if found:
            return found
    return ""


def _find(node, key, depth=0):
    """The first value under `key` anywhere in a tree, for answers whose
    layout moves around more than it changes."""
    if depth > 16:
        return None
    if isinstance(node, dict):
        if key in node:
            return node[key]
        children = node.values()
    elif isinstance(node, list):
        children = node
    else:
        return None
    for child in children:
        found = _find(child, key, depth + 1)
        if found is not None:
            return found
    return None


def _why(status, reason):
    """playabilityStatus, worded for the status line."""
    low = reason.lower()
    if "bot" in low:
        return YTError("YouTube wants a sign-in to check that this is not a bot; "
                       "try again later", general=True)
    if "age" in low or status == "AGE_CHECK_REQUIRED":
        return YTError("this song is age-restricted, which needs a signed-in account")
    if "country" in low or "region" in low:
        return YTError("this song is not available in your country")
    if status == "LIVE_STREAM_OFFLINE":
        return YTError("this live stream is not on now")
    if status == "LOGIN_REQUIRED":
        return YTError("YouTube only plays this song to a signed-in account")
    if reason:
        return YTError("YouTube says: " + reason.rstrip("."))
    return YTError("YouTube does not let this song play")


def _problem(code):
    if code == 429:
        return "YouTube Music asks the player to slow down; try again in a few minutes"
    if code in (400, 404):
        return CHANGED
    if code >= 500:
        return "YouTube Music is having trouble right now; try again later"
    return f"YouTube Music refused the request (HTTP {code})"


def _open(url, data=None, headers=None, timeout=15, limit=32 << 20):
    """The body of an answer, unpacked; YTError when there is none."""
    request = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(limit)
            packed = response.headers.get("Content-Encoding") == "gzip"
            size = response.headers.get("Content-Length") or ""
    except urllib.error.HTTPError as error:
        raise YTError(_problem(error.code), general=True, code=error.code) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError):
        raise YTError(OFFLINE, general=True) from None
    if re.fullmatch(r"\d{1,12}", size) and len(body) < int(size) <= limit:   # cut short on the way
        raise YTError(OFFLINE, general=True)
    if packed:
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError):
            raise YTError(OFFLINE, general=True) from None
    return body


# ---- HLS ----

def _hls_audio(master, address):
    """The audio-only rendition of an HLS master playlist, in the codec the
    player prefers (see AUDIO_CODECS): its address, or None."""
    codecs, renditions = {}, []
    for line in master.splitlines():
        if line.startswith("#EXT-X-STREAM-INF:"):   # the codecs are only named here
            group, found = re.search(r'AUDIO="([^"]+)"', line), re.search(r'CODECS="([^"]+)"', line)
            audio = [c.strip() for c in found.group(1).split(",")
                     if c.strip().startswith("mp4a.")] if found else []
            if group and audio:
                codecs.setdefault(group.group(1), audio[0])
        elif line.startswith("#EXT-X-MEDIA:") and "TYPE=AUDIO" in line:
            group, uri = re.search(r'GROUP-ID="([^"]+)"', line), re.search(r'URI="([^"]+)"', line)
            if group and uri:
                renditions.append((group.group(1), "DEFAULT=YES" in line, uri.group(1)))
    rank = {codec: n for n, codec in enumerate(AUDIO_CODECS)}
    usable = [(rank[codecs[group]], not default, uri) for group, default, uri in renditions
              if codecs.get(group) in rank]
    if not usable:
        return None
    found = urllib.parse.urljoin(address, min(usable)[2])
    return found if AUDIO_HOSTS.match(found) else None


def _hls_segments(playlist, address):
    """(segment addresses, their lengths in seconds, whether the list is
    complete) of an HLS media playlist, or None."""
    segments, lengths, length = [], [], None
    for line in playlist.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            try:
                length = float(line[8:].split(",")[0])
            except ValueError:
                return None
        elif line and not line.startswith("#"):
            found = urllib.parse.urljoin(address, line)
            if length is None or not AUDIO_HOSTS.match(found):
                return None
            segments.append(found)
            lengths.append(length)
            length = None
    return (segments, lengths, "#EXT-X-ENDLIST" in playlist) if segments else None


# ---- repacking: an HLS audio segment to a fragment of MP4 ----
# A segment is an ID3 tag (with the segment's start on a 90 kHz clock) and
# then ADTS frames: AAC with a small header each. The page's Media Source
# takes MP4, so the frames go into an mdat box, described by a moof box,
# after an initialization segment (ftyp and moov) that names the codec.

def _unpack(data):
    """(start in 90 kHz ticks or None, (profile, rate, channels), AAC frames)
    of a segment; ValueError when it is not AAC."""
    pts, at = None, 0
    if data[:3] == b"ID3":
        if len(data) < 10:
            raise ValueError("a damaged tag")
        end = 10 + ((data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14
                    | (data[8] & 0x7F) << 7 | data[9] & 0x7F)
        if end > len(data):
            raise ValueError("a damaged tag")
        at = 10
        while at + 10 <= end and data[at:at + 4].strip(b"\0"):
            size = struct.unpack(">I", data[at + 4:at + 8])[0]
            owner, _, value = data[at + 10:at + 10 + size].partition(b"\0")
            if (data[at:at + 4] == b"PRIV" and len(value) >= 8
                    and owner == b"com.apple.streaming.transportStreamTimestamp"):
                pts = struct.unpack(">Q", value[:8])[0] & ((1 << 33) - 1)
            at += 10 + size
        at = end
    config, frames = None, []
    while at + 7 <= len(data):
        head = data[at:at + 7]
        if head[0] != 0xFF or head[1] & 0xF6 != 0xF0:
            raise ValueError("not ADTS")
        length = (head[3] & 3) << 11 | head[4] << 3 | head[5] >> 5
        skip = 7 if head[1] & 1 else 9   # 9 with a checksum
        if length <= skip or at + length > len(data) or head[6] & 3 or head[2] >> 2 & 0xF >= 13:
            raise ValueError("a damaged frame")
        found = (head[2] >> 6, RATES[head[2] >> 2 & 0xF], (head[2] & 1) << 2 | head[3] >> 6)
        if config and found != config:
            raise ValueError("the format changed")
        config = found
        frames.append(data[at + skip:at + length])
        at += length
    if not frames:
        raise ValueError("no audio")
    return pts, config, frames


def _box(kind, *parts):
    body = b"".join(parts)
    return struct.pack(">I4s", 8 + len(body), kind) + body


def _full(kind, version, flags, *parts):
    return _box(kind, struct.pack(">I", version << 24 | flags), *parts)


def _init_segment(config):
    """ftyp and moov for one AAC track, timed in samples."""
    profile, rate, channels = config
    specific = ((profile + 1) << 11 | RATES.index(rate) << 7 | channels << 3).to_bytes(2, "big")

    def descriptor(tag, *parts):
        body = b"".join(parts)
        return bytes((tag, len(body))) + body

    esds = _full(b"esds", 0, 0, descriptor(
        3, struct.pack(">HB", 1, 0),
        descriptor(4, struct.pack(">BB3sII", 0x40, 0x15, bytes(3), 0, 0), descriptor(5, specific)),
        descriptor(6, b"\2")))
    mp4a = _box(b"mp4a", bytes(6), struct.pack(">H", 1), bytes(8),
                struct.pack(">HHHHI", channels, 16, 0, 0, rate << 16), esds)
    matrix = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    table = _box(b"stbl", _full(b"stsd", 0, 0, struct.pack(">I", 1), mp4a),
                 _full(b"stts", 0, 0, bytes(4)), _full(b"stsc", 0, 0, bytes(4)),
                 _full(b"stsz", 0, 0, bytes(8)), _full(b"stco", 0, 0, bytes(4)))
    media = _box(b"mdia", _full(b"mdhd", 0, 0, struct.pack(">IIIIHH", 0, 0, rate, 0, 0x55C4, 0)),
                 _full(b"hdlr", 0, 0, bytes(4), b"soun", bytes(12), b"SoundHandler\0"),
                 _box(b"minf", _full(b"smhd", 0, 0, bytes(4)),
                      _box(b"dinf", _full(b"dref", 0, 0, struct.pack(">I", 1), _full(b"url ", 0, 1))),
                      table))
    track = _box(b"trak", _full(b"tkhd", 0, 3, struct.pack(">IIIII", 0, 0, 1, 0, 0), bytes(8),
                                struct.pack(">hhhH", 0, 0, 0x100, 0), matrix, bytes(8)), media)
    movie = _box(b"moov", _full(b"mvhd", 0, 0, struct.pack(">IIIIIH", 0, 0, 1000, 0, 0x10000, 0x100),
                                bytes(10), matrix, bytes(24), struct.pack(">I", 2)),
                 track, _box(b"mvex", _full(b"trex", 0, 0, struct.pack(">IIIII", 1, 1, 1024, 0, 0))))
    return _box(b"ftyp", b"iso6", bytes(4), b"iso6mp41") + movie


def _fragment(sequence, start, frames):
    """moof and mdat for AAC frames whose first one plays at `start` samples."""
    sizes = struct.pack(f">{len(frames)}I", *map(len, frames))

    def moof(offset):
        return _box(b"moof", _full(b"mfhd", 0, 0, struct.pack(">I", sequence)),
                    _box(b"traf", _full(b"tfhd", 0, 0x20000, struct.pack(">I", 1)),
                         _full(b"tfdt", 1, 0, struct.pack(">Q", start)),
                         _full(b"trun", 0, 0x201, struct.pack(">Ii", len(frames), offset), sizes)))

    header = moof(0)
    return moof(len(header) + 8) + _box(b"mdat", *frames)


class YTMusic:
    """One per player. Safe to use from the server's threads at once."""

    def __init__(self):
        self.lock = threading.Lock()
        self.web = None           # (client version, visitor id, when to read them again)
        self.songs = {}           # video id -> Song
        self.gates = {}           # video id -> lock, so a song is looked up once at a time
        self.pieces = {}          # (video id, segment) -> repacked, the most recent last
        self.pictures = {}        # address -> (type, bytes), the most recent last
        self.ahead = set()        # video ids being looked up ahead
        self.missed = {}          # video id -> when looking it up ahead failed

    # ---- the web client ----
    def _session(self):
        """The web client's version and an anonymous visitor id, the way
        music.youtube.com hands them to a browser (read again after a day, or
        after five minutes when the page came without a visitor id)."""
        with self.lock:
            if self.web and time.time() < self.web[2]:
                return self.web[:2]
        page = _open(SITE + "/", headers={
            "User-Agent": BROWSER, "Accept-Language": "en-US,en;q=0.8",
            "Accept-Encoding": "gzip", "Cookie": "SOCS=CAI",   # skips the cookie banner
        }).decode("utf-8", "replace")
        version = re.search(r'"INNERTUBE_CLIENT_VERSION":"([0-9.]+)"', page)
        visitor = re.search(r'"VISITOR_DATA":"([A-Za-z0-9_%=-]+)"', page)
        web = (version.group(1) if version else WEB_VERSION,
               visitor.group(1) if visitor else "", time.time() + (86400 if visitor else 300))
        with self.lock:
            self.web = web
        return web[:2]

    def _call(self, endpoint, body, query=""):
        version, visitor = self._session()
        client = {"clientName": "WEB_REMIX", "clientVersion": version, "hl": "en"}
        headers = {"User-Agent": BROWSER, "Content-Type": "application/json",
                   "Accept-Encoding": "gzip", "Origin": SITE, "Referer": SITE + "/",
                   "X-YouTube-Client-Name": "67", "X-YouTube-Client-Version": version}
        if visitor:
            client["visitorData"] = headers["X-Goog-Visitor-Id"] = visitor
        payload = json.dumps(dict(body, context={"client": client})).encode()
        try:
            data = json.loads(_open(f"{API}{endpoint}?prettyPrint=false{query}", payload, headers))
        except ValueError:
            raise YTError(CHANGED, general=True) from None
        if not isinstance(data, dict):
            raise YTError(CHANGED, general=True)
        return data

    # ---- searching ----
    def search(self, words, kind="songs"):
        """What YouTube Music finds: {"items": [...], "more": token or None}.
        kind is all, songs, albums, artists or playlists."""
        words = " ".join(str(words).split())[:200]
        if kind not in KINDS:
            kind = "songs"
        if not words:
            return {"items": [], "more": None}
        if kind == "playlists":   # YouTube Music's own playlists first, then the listeners'
            featured = self._search(words, FILTERS["featured"])
            community = self._search(words, FILTERS["community"])
            key = lambda item: item.get("id") or item.get("video")
            seen = {key(item) for item in featured["items"]}
            featured["items"] += [i for i in community["items"] if key(i) not in seen]
            featured["more"] = community["more"]
            return featured
        return self._search(words, None if kind == "all" else FILTERS[kind])

    def _search(self, words, params):
        body = {"query": words}
        if params:
            body["params"] = params
        data = self._call("search", body)
        tabs = _dig(data, "contents", "tabbedSearchResultsRenderer", "tabs")
        sections = _dig(tabs, 0, "tabRenderer", "content", "sectionListRenderer")
        if not isinstance(sections, dict):
            raise YTError(CHANGED, general=True)
        for chip in _dig(sections, "header", "chipCloudRenderer", "chips") or []:
            chip = _dig(chip, "chipCloudChipRenderer") or {}
            name = CHIPS.get(_text(chip.get("text")))
            params = _dig(chip, "navigationEndpoint", "searchEndpoint", "params")
            if name and isinstance(params, str) and TOKEN.match(params):
                FILTERS[name] = params
        items, more = [], None
        for section in sections.get("contents") or []:
            if not isinstance(section, dict):
                continue
            shelf = section.get("musicShelfRenderer")
            if shelf:
                found, token = _items(shelf.get("contents"))
                items += found
                more = more or token or _shelf_token(shelf)
            card = section.get("musicCardShelfRenderer")
            if card:
                items += self._top(card)
            items += _items(_dig(section, "itemSectionRenderer", "contents"))[0]
        return {"items": items, "more": more}

    @staticmethod
    def _top(card):
        """The big "top result" of a search without a filter, and the songs under it."""
        endpoint = _dig(card, "title", "runs", 0, "navigationEndpoint") or card.get("onTap")
        browse_id, page = _browse(endpoint)
        video, video_type = _watch(endpoint)
        found = _details(_runs(card.get("subtitle")))
        kind = _kind(found, page, video, video_type)
        title = _text(card.get("title"))
        top = []
        if kind and title:
            top.append(_item(kind, title, found,
                             video if kind in ("song", "video") else "", browse_id,
                             _dig(card, "thumbnail", "musicThumbnailRenderer", "thumbnail",
                                  "thumbnails")))
        songs = _items(card.get("contents"))[0]
        for song in songs:   # the songs under an artist or an album leave out what it says
            if not top or not song.get("video"):
                continue
            if kind == "artist" and not song["artist"]:
                song["artist"], song["artist_id"] = title, browse_id
            elif kind == "album":
                if not song["album"]:
                    song["album"], song["album_id"] = title, browse_id
                if not song["artist"]:
                    song["artist"], song["artist_id"] = found["artist"], found["artist_id"]
        return top + songs

    def more(self, token, kind="songs"):
        """The next page of a search."""
        if not TOKEN.match(str(token)):
            return {"items": [], "more": None}
        quoted = urllib.parse.quote(urllib.parse.unquote(token), safe="")
        data = self._call("search", {}, f"&ctoken={quoted}&continuation={quoted}&type=next")
        shelf = _dig(data, "continuationContents", "musicShelfContinuation")
        if isinstance(shelf, dict):
            items, token = _items(shelf.get("contents"))
            return {"items": items, "more": token or _shelf_token(shelf)}
        items, token = _items(_dig(data, "onResponseReceivedActions", 0,
                                   "appendContinuationItemsAction", "continuationItems"))
        return {"items": items, "more": token}

    # ---- pages ----
    def _page(self, browse_id):
        if not BROWSE_ID.match(str(browse_id)):
            raise YTError("that is not a YouTube Music page")
        data = self._call("browse", {"browseId": browse_id})
        header = _header(data)
        if not header and not _sections(data):
            raise YTError(CHANGED, general=True)
        return data, header

    def album(self, browse_id):
        """An album: its header and its songs."""
        data, header = self._page(browse_id)
        title = _text(header.get("title"))
        found = _details(_runs(header.get("subtitle")) + [SEPARATOR]
                         + _runs(header.get("straplineTextOne")))
        cover = _header_cover(header)
        tracks = []
        for section in _sections(data):
            shelf = section.get("musicShelfRenderer")
            if shelf:
                tracks = [t for t in _items(shelf.get("contents"))[0] if t.get("video")]
                break
        for track in tracks:   # the album's songs leave out what the album says
            track.update(album=track["album"] or title, album_id=track["album_id"] or browse_id,
                         year=track["year"] or found["year"], cover=track["cover"] or cover)
            if not track["artist"]:
                track["artist"], track["artist_id"] = found["artist"], found["artist_id"]
        return {"kind": "album", "id": browse_id, "title": title, "artist": found["artist"],
                "artist_id": found["artist_id"], "label": found["label"], "year": found["year"],
                "info": _text(header.get("secondSubtitle")), "cover": cover,
                "tracks": tracks, "groups": []}

    def playlist(self, browse_id):
        """A playlist and its songs (the first thousand of a long one)."""
        browse_id = str(browse_id)
        if not browse_id.startswith("VL"):
            browse_id = "VL" + browse_id
        data, header = self._page(browse_id)
        found = _details(_runs(header.get("subtitle")) + [SEPARATOR]
                         + _runs(header.get("straplineTextOne")))
        author = found["artist"] or _dig(header, "facepile", "avatarStackViewModel", "text",
                                         "content") or ""
        tracks, token = [], None
        for section in _sections(data):
            shelf = section.get("musicPlaylistShelfRenderer") or section.get("musicShelfRenderer")
            if shelf:
                tracks, token = _items(shelf.get("contents"))
                token = token or _shelf_token(shelf)
                break
        while token and len(tracks) < PLAYLIST_LIMIT:
            more, token = self._continue(token)
            if not more:
                break
            tracks += more
        return {"kind": "playlist", "id": browse_id, "title": _text(header.get("title")),
                "artist": str(author), "artist_id": found["artist_id"], "label": "Playlist",
                "year": found["year"], "info": _text(header.get("secondSubtitle")),
                "cover": _header_cover(header),
                "tracks": [t for t in tracks if t.get("video")][:PLAYLIST_LIMIT], "groups": []}

    def _continue(self, token):
        """More songs of a long playlist."""
        if not TOKEN.match(str(token)):
            return [], None
        data = self._call("browse", {"continuation": urllib.parse.unquote(token)})
        items, more = _items(_dig(data, "onResponseReceivedActions", 0,
                                  "appendContinuationItemsAction", "continuationItems"))
        shelf = _dig(data, "continuationContents", "musicPlaylistShelfContinuation")
        if isinstance(shelf, dict):
            items, more = _items(shelf.get("contents"))
            more = more or _shelf_token(shelf)
        return items, more

    def artist(self, browse_id):
        """An artist: top songs, then albums, singles, playlists and similar
        artists, as YouTube Music groups them."""
        data, header = self._page(browse_id)
        name = _text(header.get("title"))
        tracks, groups = [], []
        for section in _sections(data):
            shelf = section.get("musicShelfRenderer")
            if shelf and not tracks:
                tracks = [t for t in _items(shelf.get("contents"))[0] if t.get("video")]
                continue
            carousel = section.get("musicCarouselShelfRenderer")
            if not carousel:
                continue
            heading = _text(_dig(carousel, "header", "musicCarouselShelfBasicHeaderRenderer", "title"))
            items = [i for i in _items(carousel.get("contents"))[0] if not i.get("video")]
            for item in items:
                if item["kind"] == "album" and not item["artist"]:
                    item["artist"], item["artist_id"] = name, browse_id
            if heading and items:   # videos and live performances stay out: they are songs again
                groups.append({"title": heading, "items": items})
        return {"kind": "artist", "id": browse_id, "title": name, "artist": "", "artist_id": "",
                "label": "Artist", "year": "", "info": _text(header.get("monthlyListenerCount")),
                "cover": _header_cover(header), "tracks": tracks, "groups": groups}

    def radio(self, video):
        """Songs like this one: YouTube Music's radio for it (about fifty)."""
        if not VIDEO_ID.match(str(video)):
            return []
        data = self._call("next", {"videoId": video, "playlistId": "RDAMVM" + video,
                                   "isAudioOnly": True, "enablePersistentPlaylistPanel": True,
                                   "tunerSettingValue": "AUTOMIX_SETTING_NORMAL"})
        panel = _find(data, "playlistPanelRenderer")
        if not isinstance(panel, dict):
            if _find(data, "singleColumnMusicWatchNextResultsRenderer"):
                return []   # an answer, without a radio: the song is gone
            raise YTError(CHANGED, general=True)
        songs = [_panel_item(entry) for entry in panel.get("contents") or []
                 if isinstance(entry, dict)]
        return [song for song in songs if song and song["video"] != video]

    # ---- a song's audio ----
    def song(self, video, fresh=False):
        """A song's audio. What was found is kept until shortly before its
        addresses run out; `fresh` asks again (after YouTube refused one)."""
        if not VIDEO_ID.match(str(video)):
            raise YTError("that is not a YouTube Music song")
        with self.lock:
            gate = self.gates.setdefault(video, threading.Lock())
        with gate:   # whoever asked second gets what the first one found
            with self.lock:
                known = self.songs.get(video)
            if known and known.expires - time.time() > 600 and not fresh:
                return known
            found = self._resolve(video)
            with self.lock:
                self.songs.pop(video, None)
                self.songs[video] = found
                for old in list(self.songs)[:-100]:
                    del self.songs[old]
                    self.gates.pop(old, None)
            return found

    def _resolve(self, video):
        try:
            visitor = self._session()[1]
        except YTError:
            visitor = ""
        client = dict(AUDIO_CLIENT["client"])
        agent = {"User-Agent": AUDIO_CLIENT["agent"]}
        headers = dict(agent, **{"Content-Type": "application/json", "Accept-Encoding": "gzip",
                                 "Origin": "https://www.youtube.com",
                                 "X-YouTube-Client-Name": AUDIO_CLIENT["id"],
                                 "X-YouTube-Client-Version": client["clientVersion"]})
        if visitor:   # without one, YouTube wants a sign-in
            client["visitorData"] = headers["X-Goog-Visitor-Id"] = visitor
        body = json.dumps({"context": {"client": client}, "videoId": video,
                           "contentCheckOk": True, "racyCheckOk": True}).encode()
        try:
            data = json.loads(_open(PLAYER_API, body, headers))
        except ValueError:
            raise YTError(CHANGED, general=True) from None
        status = str(_dig(data, "playabilityStatus", "status") or "")
        if status != "OK":
            raise _why(status, str(_dig(data, "playabilityStatus", "reason") or ""))
        details = data.get("videoDetails") or {}
        if details.get("isLive") or details.get("isUpcoming"):
            raise YTError("Ocean Mist cannot play live streams yet")
        address = _dig(data, "streamingData", "hlsManifestUrl")
        if not isinstance(address, str) or not AUDIO_HOSTS.match(address):
            raise YTError("YouTube sent this song in a way the player cannot read yet")
        address = _hls_audio(_open(address, headers=agent).decode("utf-8", "replace"), address)
        found = address and _hls_segments(_open(address, headers=agent).decode("utf-8", "replace"),
                                          address)
        if not found:
            raise YTError("YouTube sent this song in a way the player cannot read yet")
        segments, lengths, complete = found
        if not complete:
            raise YTError("Ocean Mist cannot play live streams yet")
        try:
            lasts = int(_dig(data, "streamingData", "expiresInSeconds") or 21540)
        except ValueError:
            lasts = 21540
        author = str(details.get("author") or "")
        song = Song(video, segments, lengths, time.time() + lasts, {
            "title": str(details.get("title") or ""),
            "artist": author[:-8] if author.endswith(" - Topic") else author,
            "duration": round(sum(lengths), 2),
            "cover": _cover(_dig(details, "thumbnail", "thumbnails"))
                     or f"https://i.ytimg.com/vi/{video}/hqdefault.jpg"})
        # the first segment says how the song is coded and when its clock starts
        for attempt in (1, 2):
            try:
                song.base, song.config, frames = _unpack(
                    _open(segments[0], headers=agent, timeout=20, limit=8 << 20))
                break
            except ValueError:
                raise YTError(CHANGED, general=True) from None
            except YTError as error:
                if error.code:
                    raise YTError(f"YouTube did not send this song (HTTP {error.code})") from None
                if attempt == 2:   # the connection broke twice
                    raise
        self._keep((video, 0), _fragment(1, 0, frames))
        return song

    def _segment(self, song, n):
        """Segment n of a song as YouTube sends it. A broken connection is tried
        once more; when its address ran out (or the connection moved to another
        network) the song is asked for again once."""
        for attempt in (1, 2):
            try:
                return _open(song.segments[n], headers={"User-Agent": AUDIO_CLIENT["agent"]},
                             timeout=20, limit=8 << 20)
            except YTError as error:
                if not error.code:
                    if attempt == 2:
                        raise
                    continue
                if error.code not in (403, 404, 410) or attempt == 2:
                    raise YTError(f"YouTube stopped sending this song (HTTP {error.code})") from None
            fresh = self.song(song.video, fresh=True)
            if len(fresh.segments) != len(song.segments) or fresh.config != song.config:
                raise YTError("YouTube changed this song's audio while it played")
            # the same audio; its clock may start elsewhere, which the pieces follow
            song.segments, song.expires, song.base = fresh.segments, fresh.expires, fresh.base
        return b""

    def init(self, video):
        """The initialization segment that goes before a song's fragments."""
        return _init_segment(self.song(video).config)

    def piece(self, video, n):
        """Segment n of a song, repacked as a fragment of MP4."""
        song = self.song(video)
        if not 0 <= n < len(song.segments):
            raise YTError("that part of the song does not exist")
        with self.lock:
            found = self.pieces.pop((video, n), None)
            if found is not None:
                self.pieces[(video, n)] = found
                return found
        try:
            pts, config, frames = _unpack(self._segment(song, n))
        except ValueError:
            raise YTError(CHANGED, general=True) from None
        if config != song.config:
            raise YTError("YouTube changed this song's audio while it played")
        if pts is None or song.base is None:   # no clock: where the playlist puts it
            start = round(sum(song.lengths[:n]) * config[1])
        else:
            start = round((pts - song.base) % (1 << 33) * config[1] / 90000)
        found = _fragment(n + 1, start, frames)
        self._keep((video, n), found)
        return found

    def _keep(self, key, piece):
        with self.lock:
            self.pieces[key] = piece
            while len(self.pieces) > PIECES:
                del self.pieces[next(iter(self.pieces))]

    def prefetch(self, video):
        """Get the song that plays next ready (its addresses and its first
        segment), in a thread of its own. One that failed is not looked up
        ahead again for ten minutes."""
        if not VIDEO_ID.match(str(video)):
            return
        with self.lock:
            known = self.songs.get(video)
            if (video in self.ahead or (known and known.expires - time.time() > 600)
                    or time.time() - self.missed.get(video, 0) < 600):
                return
            self.ahead.add(video)

        def run():
            try:
                self.song(video)
            except YTError:
                with self.lock:
                    self.missed[video] = time.time()
                    for old in list(self.missed)[:-100]:
                        del self.missed[old]
            finally:
                with self.lock:
                    self.ahead.discard(video)

        threading.Thread(target=run, daemon=True).start()

    # ---- covers ----
    def picture(self, url):
        """A cover's (type, bytes). Only YouTube's own picture servers are asked."""
        if not PICTURE_HOSTS.match(str(url)):
            raise YTError("that is not a YouTube Music picture")
        with self.lock:
            found = self.pictures.pop(url, None)
            if found:
                self.pictures[url] = found
                return found
        request = urllib.request.Request(url, headers={"User-Agent": BROWSER})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                kind = response.headers.get_content_type()
                data = response.read(8 << 20)
        except urllib.error.HTTPError as error:
            raise YTError(f"YouTube sent no picture (HTTP {error.code})") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError):
            raise YTError(OFFLINE, general=True) from None
        if not kind.startswith("image/") or not data:
            raise YTError("YouTube sent no picture")
        with self.lock:
            self.pictures[url] = (kind, data)
            while len(self.pictures) > PICTURES:
                del self.pictures[next(iter(self.pictures))]
        return kind, data


def main(argv):
    """A look at the answers from the command line, for working on this file."""
    music = YTMusic()
    command, *rest = argv or ["help"]
    try:
        if command == "search" and rest:
            kind = rest.pop(0) if rest[0] in KINDS and len(rest) > 1 else "songs"
            result = music.search(" ".join(rest), kind)
        elif command in ("album", "artist", "playlist", "radio") and len(rest) == 1:
            result = getattr(music, command)(rest[0])
        elif command == "audio" and len(rest) == 1:
            song = music.song(rest[0])
            info = song.info()
            last = len(song.segments) - 1
            result = {"type": info["type"], "seconds": info["duration"],
                      "segments": len(song.segments), "rate": song.config[1],
                      "first piece": len(music.piece(rest[0], 0)),
                      "last piece": len(music.piece(rest[0], last)),
                      "minutes left": round((song.expires - time.time()) / 60),
                      "details": song.details}
        else:
            print(__doc__.split("\n\n")[-1])
            return 2
    except YTError as error:
        print("error:", error, file=sys.stderr)
        return 1
    print(json.dumps(result, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
