"""Internet radio: finds the stream behind a station's address and relays it.

The page cannot play a station directly (audio from another site comes out
silent through the equalizer and the visualizations), so the local server
fetches the stream and hands it on, picking the song titles out on the way.
"""
import re
import urllib.parse
import urllib.request

AGENT = "OceanMist/1.0"
PLAYLIST_TYPES = {
    "audio/x-scpls", "audio/scpls", "application/pls+xml", "audio/x-mpegurl",
    "audio/mpegurl", "application/x-mpegurl", "application/vnd.apple.mpegurl",
}


class RadioError(Exception):
    pass


def _first_address(text):
    for line in text.splitlines():
        line = line.strip()
        if "=" in line and line.lower().startswith("file"):
            line = line.split("=", 1)[1].strip()
        if re.match(r"https?://", line, re.I):
            return line
    return None


def open_stream(url, hops=5):
    """An open HTTP response carrying audio; station playlists are followed."""
    for _ in range(hops):
        request = urllib.request.Request(url, headers={"Icy-MetaData": "1", "User-Agent": AGENT})
        try:
            response = urllib.request.urlopen(request, timeout=12)
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
    text = block.rstrip(b"\x00").decode("utf-8", "replace")
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
