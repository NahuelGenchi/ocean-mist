"""Internet radio: finds the stream behind a station's address and relays it.

The page cannot play a station directly (audio from another site comes out
silent through the equalizer and the visualizations), so the local server
fetches the stream and hands it on, picking the song titles out on the way.
"""
import http.client
import re
import ssl
import urllib.parse
import urllib.request

AGENT = "OceanMist/1.0"
PLAYLIST_TYPES = {
    "audio/x-scpls", "audio/scpls", "application/pls+xml", "audio/x-mpegurl",
    "audio/mpegurl", "application/x-mpegurl", "application/vnd.apple.mpegurl",
}


class RadioError(Exception):
    pass


# A SHOUTcast v1 server answers "ICY 200 OK" where HTTP says "HTTP/1.0 200 OK", which http.client refuses.
# The station's answer is read through a file that reads that one line as HTTP/1.0's; the rest is HTTP's.
class _IcyFile:
    def __init__(self, fp):
        self._fp, self._first = fp, True

    def readline(self, *args):
        line = self._fp.readline(*args)
        if self._first:
            self._first = False
            if line.startswith(b"ICY "):
                line = b"HTTP/1.0 " + line[4:]
        return line

    def __getattr__(self, name):
        return getattr(self._fp, name)


class _IcyResponse(http.client.HTTPResponse):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fp = _IcyFile(self.fp)


class _IcyConnection(http.client.HTTPConnection):
    response_class = _IcyResponse


class _IcySecureConnection(http.client.HTTPSConnection):
    response_class = _IcyResponse


class _IcyHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_IcyConnection, req)


class _IcySecureHandler(urllib.request.HTTPSHandler):
    def __init__(self):
        self.tls = ssl.create_default_context()
        super().__init__(context=self.tls)

    def https_open(self, req):
        return self.do_open(_IcySecureConnection, req, context=self.tls)


_OPENER = urllib.request.build_opener(_IcyHandler, _IcySecureHandler)


def _first_address(text):
    """The first stream a station's .pls or .m3u names: an address, or a path beside the playlist's own,
    which open_stream joins to it. Other schemes, comments and the .pls file's other keys are passed over."""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        key, equals, value = line.partition("=")
        if equals and re.fullmatch(r"\w+", key.strip()):   # a .pls key: only FileN names a stream
            if not key.strip().lower().startswith("file"):
                continue
            line = value.strip()
        if re.match(r"https?://", line, re.I):
            return line
        if line and not re.match(r"[a-z][a-z0-9+.-]*:", line, re.I) and not re.search(r"\s", line):
            return line   # a relative address
    return None


def open_stream(url, hops=5):
    """An open HTTP response carrying audio; station playlists are followed."""
    for _ in range(hops):
        request = urllib.request.Request(url, headers={"Icy-MetaData": "1", "User-Agent": AGENT})
        try:
            response = _OPENER.open(request, timeout=12)
        except Exception as error:
            raise RadioError(f"cannot reach the station ({getattr(error, 'reason', error)})")
        kind = response.headers.get_content_type()
        listing = urllib.parse.urlparse(url).path.lower().endswith((".pls", ".m3u", ".m3u8"))
        if kind not in PLAYLIST_TYPES and not (listing and kind.startswith("text/")):
            if kind.startswith(("text/", "application/json")):
                response.close()
                raise RadioError("this address is a web page, not a stream")
            return response
        text = response.read(1 << 16).decode("utf-8", "replace")
        response.close()
        if "#EXT-X-" in text:
            raise RadioError("this kind of stream (HLS) is not supported")
        address = _first_address(text)
        if address is None:
            raise RadioError("the station's playlist names no stream")
        url = urllib.parse.urljoin(url, address)
    raise RadioError("the station keeps pointing somewhere else")


def stream_title(block):
    """The song title from an ICY metadata block."""
    block = block.rstrip(b"\x00")
    try:
        text = block.decode("utf-8")
    except UnicodeError:   # many stations still send Windows' Latin-1
        text = block.decode("cp1252", "replace")
    found = re.search(r"StreamTitle='(.*?)';", text, re.S)
    return found.group(1).strip() if found else None


def relay(response, write, on_title):
    """Copy the audio to write() until either side goes away. Metadata blocks
    are taken out of the stream and their titles passed to on_title()."""
    try:
        interval = int(response.headers.get("icy-metaint") or 0)
    except ValueError:
        interval = 0
    try:
        while True:
            left = interval or 4096
            while left > 0:
                chunk = response.read(min(4096, left))
                if not chunk:
                    return
                write(chunk)
                left -= len(chunk)
            if interval:
                size = response.read(1)
                if not size:
                    return
                block = response.read(size[0] * 16) if size[0] else b""
                title = stream_title(block) if block else None
                if title is not None:
                    on_title(title)
    except (OSError, ValueError):
        return
    finally:
        response.close()
