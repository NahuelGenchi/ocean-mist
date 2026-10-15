"""The desktop's media controls (MPRIS): what makes the keyboard's play, next
and previous keys work, and puts the song in GNOME's notification panel and
on the lock screen."""
from gi.repository import Gio, GLib

PATH = "/org/mpris/MediaPlayer2"
ROOT = "org.mpris.MediaPlayer2"
PLAYER = "org.mpris.MediaPlayer2.Player"
NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"

XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek"><arg name="Offset" type="x" direction="in"/></method>
    <method name="SetPosition">
      <arg name="TrackId" type="o" direction="in"/>
      <arg name="Position" type="x" direction="in"/>
    </method>
    <method name="OpenUri"><arg name="Uri" type="s" direction="in"/></method>
    <signal name="Seeked"><arg name="Position" type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""
LOOP = {"off": "None", "all": "Playlist", "one": "Track"}


def bus_name(app_id):
    """The name on the bus follows the app id: org.mpris.MediaPlayer2.OceanMist
    for the player, and for a test instance, which runs under an app id of its
    own, a name of its own (io.github.nahuelgenchi.OceanMistTest.Check answers
    at org.mpris.MediaPlayer2.OceanMistTest.Check)."""
    return "org.mpris.MediaPlayer2." + app_id.removeprefix("io.github.nahuelgenchi.")


class Mpris:
    """Publishes the player on the session bus. `control(action, value)` is
    called with what the desktop asks for; `raise_window` and `quit` too."""

    def __init__(self, connection, app_id, library, control, raise_window, quit):
        self.connection, self.library, self.app_id = connection, library, app_id
        self.control, self.raise_window, self.quit = control, raise_window, quit
        self.sent = {}
        self.last_position = 0.0
        node = Gio.DBusNodeInfo.new_for_xml(XML)
        for interface in node.interfaces:
            connection.register_object(PATH, interface, self._call, self._get, self._set)
        Gio.bus_own_name_on_connection(
            connection, bus_name(app_id), Gio.BusNameOwnerFlags.NONE, None, None)

    # ---- what the desktop reads ----
    def _state(self):
        library = self.library
        with library.lock:
            now, settings = dict(library.now), dict(library.settings)
            track = library.by_id.get(now["id"])
            track = dict(track) if track else None
            station = library.station(now["station"]) if now["station"] is not None else None
            which = library.list_of(now["id"])   # the list playback goes through
            count = (len(library.stations) if station is not None
                     else len(library.lists[which]) if which
                     else sum(len(tracks) for tracks in library.lists.values()))
        metadata = {"mpris:trackid": GLib.Variant("o", NO_TRACK)}
        if station is not None:
            metadata = {
                "mpris:trackid": GLib.Variant("o", f"{PATH}/station/{station['id']}"),
                "xesam:title": GLib.Variant("s", now["title"] or station["name"]),
                "xesam:artist": GLib.Variant("as", [station["name"]]),
            }
        elif track is not None:
            metadata = {
                "mpris:trackid": GLib.Variant("o", f"{PATH}/track/{track['id']}"),
                "xesam:title": GLib.Variant("s", track["title"]),
                "xesam:url": GLib.Variant("s", track["path"] if track.get("source")   # a link already
                                          else GLib.filename_to_uri(track["path"], None)),
            }
            length = now["duration"] or track["duration"]
            if length:
                metadata["mpris:length"] = GLib.Variant("x", int(length * 1e6))
            if track["artist"]:
                metadata["xesam:artist"] = GLib.Variant("as", [track["artist"]])
            if track["album"]:
                metadata["xesam:album"] = GLib.Variant("s", track["album"])
            if track["track"]:
                metadata["xesam:trackNumber"] = GLib.Variant("i", track["track"])
            cover = library.cover_file(track["id"]) if track["art"] else None
            if cover:
                metadata["mpris:artUrl"] = GLib.Variant("s", GLib.filename_to_uri(cover, None))
        repeat = settings.get("repeat")
        repeat = repeat if repeat in ("one", "all") else ("all" if repeat is True else "off")
        loaded = track is not None or station is not None
        return {
            "PlaybackStatus": GLib.Variant("s", now["mode"].capitalize()),
            "LoopStatus": GLib.Variant("s", LOOP[repeat]),
            "Rate": GLib.Variant("d", 1.0),
            "Shuffle": GLib.Variant("b", bool(settings.get("shuffle"))),
            "Metadata": GLib.Variant("a{sv}", metadata),
            "Volume": GLib.Variant("d", 0.0 if settings.get("muted") else float(settings.get("volume", 0.5))),
            "MinimumRate": GLib.Variant("d", 1.0),
            "MaximumRate": GLib.Variant("d", 1.0),
            "CanGoNext": GLib.Variant("b", count > 1),
            "CanGoPrevious": GLib.Variant("b", count > 0),
            "CanPlay": GLib.Variant("b", count > 0 or loaded),
            "CanPause": GLib.Variant("b", loaded),
            "CanSeek": GLib.Variant("b", track is not None),
            "CanControl": GLib.Variant("b", True),
        }

    def _root(self):
        return {
            "CanQuit": GLib.Variant("b", True),
            "CanRaise": GLib.Variant("b", True),
            "HasTrackList": GLib.Variant("b", False),
            "Identity": GLib.Variant("s", "Ocean Mist"),
            "DesktopEntry": GLib.Variant("s", self.app_id),
            "SupportedUriSchemes": GLib.Variant("as", ["file"]),
            "SupportedMimeTypes": GLib.Variant("as", [
                "audio/mpeg", "audio/flac", "audio/ogg", "audio/opus",
                "audio/mp4", "audio/x-wav"]),
        }

    def _get(self, connection, sender, path, interface, name):
        if interface == ROOT:
            return self._root().get(name)
        if name == "Position":
            return GLib.Variant("x", int(self.library.position() * 1e6))
        return self._state().get(name)

    def _set(self, connection, sender, path, interface, name, value):
        if name == "Volume":
            self.control("volume", max(0.0, min(1.0, value.get_double())))
        elif name == "Shuffle":
            self.control("shuffle", value.get_boolean())
        elif name == "LoopStatus":
            wanted = {v: k for k, v in LOOP.items()}.get(value.get_string(), "off")
            self.control("repeat", wanted)
        return True

    def _call(self, connection, sender, path, interface, method, params, invocation):
        if interface == ROOT:
            (self.raise_window if method == "Raise" else self.quit)()
        elif method in ("Next", "Previous", "Pause", "PlayPause", "Stop", "Play"):
            self.control(method.lower(), None)
        elif method == "Seek":
            self.control("seek", params.unpack()[0] / 1e6)
        elif method == "SetPosition":
            self.control("position", params.unpack()[1] / 1e6)
        invocation.return_value(None)

    # ---- telling the desktop what changed ----
    def update(self):
        """Call on the GTK thread whenever playback or the settings change."""
        state = self._state()
        changed = {k: v for k, v in state.items()
                   if k not in self.sent or not self.sent[k].equal(v)}
        self.sent = state
        if changed:
            self._emit("org.freedesktop.DBus.Properties", "PropertiesChanged",
                       GLib.Variant("(sa{sv}as)", (PLAYER, changed, [])))
        # a jump in the position that the clock does not explain is a seek
        position = self.library.position()
        mode = self.library.now["mode"]
        expected = self.last_position + (GLib.get_monotonic_time() / 1e6 - getattr(self, "_at", 0)
                                         if getattr(self, "_mode", "") == "playing" else 0)
        if "Metadata" not in changed and abs(position - expected) > 1.5:
            self._emit(PLAYER, "Seeked", GLib.Variant("(x)", (int(position * 1e6),)))
        self.last_position, self._at, self._mode = position, GLib.get_monotonic_time() / 1e6, mode
        return False

    def _emit(self, interface, signal, payload):
        try:
            self.connection.emit_signal(None, PATH, interface, signal, payload)
        except GLib.Error:
            pass
