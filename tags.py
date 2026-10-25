"""Titles, covers and lengths read straight from music files.

Standard library only. Understands MP3 (ID3v2/v1), FLAC, Ogg Vorbis, Opus,
MP4/M4A and WAV. Covers are only pulled out when asked for, so scanning a big
library touches a few kilobytes per file.

    read(path)            -> {"title", "artist", "album", "year", "genre",
                              "track", "duration", "has_art"}   (those known)
    read(path, art=True)  -> the same, plus "art": (mime, bytes), the mime
                             one of PICTURE_TYPES
"""
import base64
import io
import os
import struct

TEXT_LIMIT = 1 << 16          # longest text frame worth reading
PACKET_LIMIT = 32 << 20       # longest Ogg comment packet (covers live there)
PICTURE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")   # the types a cover is sent as


def read(path, art=False):
    ext = os.path.splitext(path)[1].lower()
    reader = {
        ".mp3": _mp3, ".flac": _flac, ".ogg": _ogg, ".oga": _ogg, ".opus": _ogg,
        ".m4a": _mp4, ".mp4": _mp4, ".aac": _mp4, ".wav": _wav,
    }.get(ext)
    if reader is None:
        return {}
    try:
        with open(path, "rb") as fh:
            tags = reader(fh, os.path.getsize(path), art)
    except (OSError, ValueError, IndexError, struct.error, UnicodeError):
        return {}
    clean = {}
    for key, value in tags.items():
        if isinstance(value, str):
            value = value.strip().strip("\x00")
        if value:
            clean[key] = value
    if "year" in clean:
        clean["year"] = str(clean["year"])[:4]
    if "track" in clean:
        clean["track"] = _number(clean["track"])
    if "art" in clean:
        mime, data = clean["art"]
        clean["art"] = (picture_type(mime, data), data)
    return clean


def picture_type(mime, data):
    """A cover's type, from its first bytes, else the one its tag names when
    that is one of PICTURE_TYPES, else JPEG: the words of a tag (which may
    hold anything, a line break too) never go out as a header."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return mime if mime in PICTURE_TYPES else "image/jpeg"


def _number(value):
    digits = ""
    for char in str(value):
        if not char.isdigit():
            break
        digits += char
    return int(digits) if digits else 0


def _synchsafe(raw):
    value = 0
    for byte in raw:
        value = (value << 7) | (byte & 0x7F)
    return value


# -- MP3 ----------------------------------------------------------------------

ID3_NAMES = {
    b"TIT2": "title", b"TT2": "title", b"TPE1": "artist", b"TP1": "artist",
    b"TALB": "album", b"TAL": "album", b"TYER": "year", b"TDRC": "year",
    b"TYE": "year", b"TRCK": "track", b"TRK": "track", b"TCON": "genre",
    b"TCO": "genre",
}
ID3_GENRES = (
    "Blues", "Classic Rock", "Country", "Dance", "Disco", "Funk", "Grunge",
    "Hip-Hop", "Jazz", "Metal", "New Age", "Oldies", "Other", "Pop", "R&B",
    "Rap", "Reggae", "Rock", "Techno", "Industrial", "Alternative", "Ska",
    "Death Metal", "Pranks", "Soundtrack", "Euro-Techno", "Ambient", "Trip-Hop",
    "Vocal", "Jazz+Funk", "Fusion", "Trance", "Classical", "Instrumental",
    "Acid", "House", "Game", "Sound Clip", "Gospel", "Noise", "Alt. Rock",
    "Bass", "Soul", "Punk", "Space", "Meditative", "Instrumental Pop",
    "Instrumental Rock", "Ethnic", "Gothic", "Darkwave", "Techno-Industrial",
    "Electronic", "Pop-Folk", "Eurodance", "Dream", "Southern Rock", "Comedy",
    "Cult", "Gangsta", "Top 40", "Christian Rap", "Pop/Funk", "Jungle",
    "Native American", "Cabaret", "New Wave", "Psychedelic", "Rave",
    "Showtunes", "Trailer", "Lo-Fi", "Tribal", "Acid Punk", "Acid Jazz",
    "Polka", "Retro", "Musical", "Rock & Roll", "Hard Rock",
)


def _id3_text(body):
    if not body:
        return ""
    encoding = {0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}.get(body[0])
    if encoding is None:
        return ""
    return body[1:].decode(encoding, "replace").split("\x00")[0].strip()


def _id3_picture(body, legacy):
    """(mime, bytes) from an APIC (or v2.2 PIC) frame."""
    if len(body) < 6:
        return None
    wide = body[0] in (1, 2)
    if legacy:
        fmt = body[1:4].upper()
        mime = {b"PNG": "image/png"}.get(fmt, "image/jpeg")
        pos = 5
    else:
        end = body.index(b"\x00", 1)
        mime = body[1:end].decode("latin-1").lower() or "image/jpeg"
        if "/" not in mime:
            mime = "image/" + mime
        pos = end + 2
    if wide:
        while pos + 1 < len(body) and body[pos:pos + 2] != b"\x00\x00":
            pos += 2
        pos += 2
    else:
        pos = body.index(b"\x00", pos) + 1
    data = body[pos:]
    return (mime, data) if data else None


def _genre(text):
    """ID3 genres may be written as "(17)" or "17"."""
    raw = text.strip()
    if raw.startswith("(") and ")" in raw:
        inside, rest = raw[1:].split(")", 1)
        if rest.strip():
            return rest.strip()
        raw = inside
    if raw.isdigit() and int(raw) < len(ID3_GENRES):
        return ID3_GENRES[int(raw)]
    return text


def _id3v2(fh, want_art):
    """Parse the ID3v2 tag at the start of fh. Returns (tags, bytes it used)."""
    head = fh.read(10)
    if len(head) < 10 or head[:3] != b"ID3":
        fh.seek(0)
        return {}, 0
    version, flags = head[3], head[5]
    size = _synchsafe(head[6:10])
    end = 10 + size + (10 if version == 4 and flags & 0x10 else 0)
    tags = {}
    if version not in (2, 3, 4):
        return tags, end
    if version < 4 and flags & 0x80:   # whole tag unsynchronised: rare, undo in memory
        stream = io.BytesIO(fh.read(size).replace(b"\xff\x00", b"\xff"))
        limit = len(stream.getbuffer())
    else:
        stream, limit = fh, 10 + size
    if version >= 3 and flags & 0x40:
        raw = stream.read(4)
        extended = _synchsafe(raw) - 4 if version == 4 else int.from_bytes(raw, "big")
        stream.seek(max(0, extended), os.SEEK_CUR)
    header = 6 if version == 2 else 10
    while stream.tell() + header <= limit:
        frame = stream.read(header)
        if len(frame) < header:
            break
        if version == 2:
            frame_id, frame_size, frame_flags = frame[:3], int.from_bytes(frame[3:6], "big"), 0
        else:
            frame_id = frame[:4]
            frame_size = _synchsafe(frame[4:8]) if version == 4 else int.from_bytes(frame[4:8], "big")
            frame_flags = frame[9]
        if not frame_id.strip(b"\x00") or frame_size <= 0 or stream.tell() + frame_size > limit:
            break
        picture = frame_id in (b"APIC", b"PIC")
        wanted = (frame_id in ID3_NAMES and frame_size <= TEXT_LIMIT) or (
            picture and want_art and "art" not in tags)
        if not wanted:
            if picture:
                tags["has_art"] = True
            stream.seek(frame_size, os.SEEK_CUR)
            continue
        body = stream.read(frame_size)
        if version == 4:
            if frame_flags & 0x02:
                body = body.replace(b"\xff\x00", b"\xff")
            if frame_flags & 0x01:
                body = body[4:]
        if picture:
            found = _id3_picture(body, legacy=frame_id == b"PIC")
            if found:
                tags["art"], tags["has_art"] = found, True
        else:
            name, value = ID3_NAMES[frame_id], _id3_text(body)
            if value and name not in tags:
                tags[name] = _genre(value) if name == "genre" else value
    return tags, end


MP3_BITRATES = {
    (1, 1): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (1, 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (1, 3): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (2, 1): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (2, 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    (2, 3): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
MP3_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _mp3_frame(head):
    """(length, samples, rate, kbps, offset of a Xing header) or None."""
    if len(head) < 4 or head[0] != 0xFF or head[1] & 0xE0 != 0xE0:
        return None
    version, layer = (head[1] >> 3) & 3, 4 - ((head[1] >> 1) & 3)
    bitrate, rate, padding = head[2] >> 4, (head[2] >> 2) & 3, (head[2] >> 1) & 1
    if version == 1 or layer == 4 or bitrate in (0, 15) or rate == 3:
        return None
    kbps = MP3_BITRATES[(1 if version == 3 else 2, layer)][bitrate]
    hz = MP3_RATES[version][rate]
    mono = head[3] >> 6 == 3
    if layer == 1:
        samples, length = 384, (12000 * kbps // hz + padding) * 4
    else:
        samples = 1152 if layer == 2 or version == 3 else 576
        length = samples // 8 * 1000 * kbps // hz + padding
    xing = 4 + ((17 if mono else 32) if version == 3 else (9 if mono else 17))
    return length, samples, hz, kbps, xing


def _mp3_duration(fh, start, size):
    """Seconds, from the Xing/VBRI frame count or, failing that, the bitrate."""
    fh.seek(start)
    data = fh.read(1 << 17)
    pos = 0
    while True:
        pos = data.find(b"\xff", pos)
        if pos < 0 or pos + 4 > len(data):
            return 0
        frame = _mp3_frame(data[pos:pos + 4])
        if frame and frame[0] > 4:
            follow = pos + frame[0]
            # a real frame is followed by another one
            if follow + 4 > len(data) or _mp3_frame(data[follow:follow + 4]):
                break
        pos += 1
    length, samples, hz, kbps, xing = frame
    body = data[pos:pos + length]
    if body[xing:xing + 4] in (b"Xing", b"Info") and body[xing + 7] & 1:
        frames = int.from_bytes(body[xing + 8:xing + 12], "big")
        if frames:
            return frames * samples / hz
    if body[36:40] == b"VBRI":
        frames = int.from_bytes(body[50:54], "big")
        if frames:
            return frames * samples / hz
    # no frame count: measure the frames at hand and scale up to the whole file
    frames, at = 0, pos
    while at + 4 <= len(data):
        step = _mp3_frame(data[at:at + 4])
        if not step or step[0] <= 4:
            break
        frames, at = frames + 1, at + step[0]
    if not frames:
        return 0
    audio = size - start - pos
    if size >= 128:
        fh.seek(-128, os.SEEK_END)
        if fh.read(3) == b"TAG":
            audio -= 128
    return frames * samples / hz * audio / min(at - pos, audio)


def _mp3(fh, size, want_art):
    tags, start = _id3v2(fh, want_art)
    if not any(key in tags for key in ("title", "artist", "album")) and size >= 128:
        fh.seek(-128, os.SEEK_END)
        v1 = fh.read(128)
        if v1[:3] == b"TAG":
            def field(a, b):
                return v1[a:b].split(b"\x00")[0].decode("latin-1").strip()
            for key, value in (("title", field(3, 33)), ("artist", field(33, 63)),
                               ("album", field(63, 93)), ("year", field(93, 97))):
                tags.setdefault(key, value)
            if v1[127] < len(ID3_GENRES):
                tags.setdefault("genre", ID3_GENRES[v1[127]])
    tags["duration"] = _mp3_duration(fh, start, size)
    return tags


# -- FLAC, Ogg Vorbis, Opus ------------------------------------------------------

VORBIS_NAMES = {
    "TITLE": "title", "ARTIST": "artist", "ALBUM": "album", "DATE": "year",
    "YEAR": "year", "GENRE": "genre", "TRACKNUMBER": "track",
}


def _flac_picture(block):
    pos = 4
    mime_len = int.from_bytes(block[pos:pos + 4], "big")
    mime = block[pos + 4:pos + 4 + mime_len].decode("latin-1").lower() or "image/jpeg"
    pos += 4 + mime_len
    pos += 4 + int.from_bytes(block[pos:pos + 4], "big")   # description
    pos += 16                                               # width, height, depth, colours
    length = int.from_bytes(block[pos:pos + 4], "big")
    data = block[pos + 4:pos + 4 + length]
    return (mime, data) if data else None


def _vorbis_comments(data, tags, want_art):
    pos = 4 + int.from_bytes(data[:4], "little")            # vendor string
    count = int.from_bytes(data[pos:pos + 4], "little")
    pos += 4
    for _ in range(count):
        length = int.from_bytes(data[pos:pos + 4], "little")
        entry = data[pos + 4:pos + 4 + length]
        pos += 4 + length
        if pos > len(data):
            break
        key, _, value = entry.partition(b"=")
        key = key.decode("ascii", "replace").upper()
        if key in ("METADATA_BLOCK_PICTURE", "COVERART"):
            tags["has_art"] = True
            if want_art and "art" not in tags:
                raw = base64.b64decode(value, validate=False)
                found = _flac_picture(raw) if key == "METADATA_BLOCK_PICTURE" else ("image/jpeg", raw)
                if found:
                    tags["art"] = found
        elif key in VORBIS_NAMES and VORBIS_NAMES[key] not in tags:
            tags[VORBIS_NAMES[key]] = value.decode("utf-8", "replace")


def _flac(fh, size, want_art):
    _, start = _id3v2(fh, False)      # some FLACs carry an ID3 tag in front
    fh.seek(start)
    if fh.read(4) != b"fLaC":
        return {}
    tags = {}
    while True:
        head = fh.read(4)
        if len(head) < 4:
            break
        kind, length = head[0] & 0x7F, int.from_bytes(head[1:4], "big")
        if kind == 0:
            info = fh.read(length)
            rate = (info[10] << 12) | (info[11] << 4) | (info[12] >> 4)
            total = ((info[13] & 0x0F) << 32) | int.from_bytes(info[14:18], "big")
            if rate:
                tags["duration"] = total / rate
        elif kind == 4:
            _vorbis_comments(fh.read(length), tags, want_art)
        elif kind == 6:
            tags["has_art"] = True
            if want_art and "art" not in tags:
                found = _flac_picture(fh.read(length))
                if found:
                    tags["art"] = found
            else:
                fh.seek(length, os.SEEK_CUR)
        else:
            fh.seek(length, os.SEEK_CUR)
        if head[0] & 0x80:
            break
    return tags


def _ogg_packets(fh, wanted):
    """The first `wanted` packets of an Ogg stream (they may span pages)."""
    packets, current = [], b""
    while len(packets) < wanted:
        head = fh.read(27)
        if len(head) < 27 or head[:4] != b"OggS":
            break
        lacing = fh.read(head[26])
        for value in lacing:
            current += fh.read(value)
            if len(current) > PACKET_LIMIT:
                return packets
            if value < 255:
                packets.append(current)
                current = b""
                if len(packets) == wanted:
                    break
    return packets


def _ogg(fh, size, want_art):
    packets = _ogg_packets(fh, 2)
    if not packets:
        return {}
    tags, first = {}, packets[0]
    rate, skip = 0, 0
    if first[:8] == b"OpusHead":
        rate, skip = 48000, int.from_bytes(first[10:12], "little")
        marker = b"OpusTags"
    elif first[:7] == b"\x01vorbis":
        rate = int.from_bytes(first[12:16], "little")
        marker = b"\x03vorbis"
    else:
        return {}
    if len(packets) > 1 and packets[1].startswith(marker):
        _vorbis_comments(packets[1][len(marker):], tags, want_art)
    # the last page says how many samples the stream holds
    tail = min(size, 1 << 16)
    fh.seek(size - tail)
    data = fh.read(tail)
    last = data.rfind(b"OggS")
    if last >= 0 and last + 14 <= len(data) and rate:
        granule = int.from_bytes(data[last + 6:last + 14], "little")
        if 0 < granule < 1 << 62:
            tags["duration"] = max(0, granule - skip) / rate
    return tags


# -- MP4 / M4A ----------------------------------------------------------------

MP4_NAMES = {
    b"\xa9nam": "title", b"\xa9ART": "artist", b"aART": "artist",
    b"\xa9alb": "album", b"\xa9day": "year", b"\xa9gen": "genre",
}


def _atoms(fh, start, end):
    """(type, body start, body end) for each atom between start and end."""
    pos = start
    while pos + 8 <= end:
        fh.seek(pos)
        head = fh.read(8)
        if len(head) < 8:
            return
        length, kind, header = int.from_bytes(head[:4], "big"), head[4:8], 8
        if length == 1:
            length, header = int.from_bytes(fh.read(8), "big"), 16
        elif length == 0:
            length = end - pos
        if length < header or pos + length > end:
            return
        yield kind, pos + header, pos + length
        pos += length


def _mp4(fh, size, want_art):
    tags = {}
    moov = next(((a, b) for kind, a, b in _atoms(fh, 0, size) if kind == b"moov"), None)
    if moov is None:
        return tags
    for kind, start, end in _atoms(fh, *moov):
        if kind == b"mvhd":
            fh.seek(start)
            body = fh.read(min(end - start, 32))
            if body[0] == 1:
                scale, length = int.from_bytes(body[20:24], "big"), int.from_bytes(body[24:32], "big")
            else:
                scale, length = int.from_bytes(body[12:16], "big"), int.from_bytes(body[16:20], "big")
            if scale:
                tags["duration"] = length / scale
        elif kind == b"udta":
            for kind2, start2, end2 in _atoms(fh, start, end):
                if kind2 != b"meta":
                    continue
                for kind3, start3, end3 in _atoms(fh, start2 + 4, end2):
                    if kind3 == b"ilst":
                        _mp4_items(fh, start3, end3, tags, want_art)
    return tags


def _mp4_items(fh, start, end, tags, want_art):
    for name, item_start, item_end in _atoms(fh, start, end):
        cover = name == b"covr"
        if not (cover or name in MP4_NAMES or name == b"trkn"):
            continue
        for kind, data_start, data_end in _atoms(fh, item_start, item_end):
            if kind != b"data" or data_end - data_start < 8:
                continue
            if cover:
                tags["has_art"] = True
                if not want_art or "art" in tags:
                    break
            elif data_end - data_start > TEXT_LIMIT:
                break
            fh.seek(data_start)
            flags = int.from_bytes(fh.read(8)[:4], "big") & 0xFFFFFF
            payload = fh.read(data_end - data_start - 8)
            if cover:
                tags["art"] = ("image/png" if flags == 14 else "image/jpeg", payload)
            elif name == b"trkn":
                if len(payload) >= 4:
                    tags.setdefault("track", str(int.from_bytes(payload[2:4], "big")))
            else:
                tags.setdefault(MP4_NAMES[name], payload.decode("utf-8", "replace"))
            break


# -- WAV ----------------------------------------------------------------------

WAV_NAMES = {b"INAM": "title", b"IART": "artist", b"IPRD": "album",
             b"ICRD": "year", b"IGNR": "genre", b"ITRK": "track"}


def _wav(fh, size, want_art):
    head = fh.read(12)
    if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return {}
    tags, byte_rate, pos = {}, 0, 12
    while pos + 8 <= size:
        fh.seek(pos)
        chunk = fh.read(8)
        if len(chunk) < 8:
            break
        kind, length = chunk[:4], int.from_bytes(chunk[4:8], "little")
        if kind == b"fmt ":
            byte_rate = int.from_bytes(fh.read(16)[8:12], "little")
        elif kind == b"data" and byte_rate:
            tags["duration"] = min(length, size - pos - 8) / byte_rate
        elif kind == b"LIST" and length <= 1 << 20:
            body = fh.read(length)
            if body[:4] == b"INFO":
                at = 4
                while at + 8 <= len(body):
                    name, n = body[at:at + 4], int.from_bytes(body[at + 4:at + 8], "little")
                    if name in WAV_NAMES:
                        text = body[at + 8:at + 8 + n].split(b"\x00")[0]
                        tags.setdefault(WAV_NAMES[name], text.decode("utf-8", "replace"))
                    at += 8 + n + (n & 1)
        pos += 8 + length + (length & 1)
    return tags
