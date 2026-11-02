"""YouTube Music (#85): one song that cannot be fetched is that song's failure,
not every song's, and rows YouTube greys out are left out of the list. Every
request is answered by a stand-in: nothing is asked of YouTube."""
import json
import unittest
from unittest import mock

import tests  # noqa: F401  (scratch folders first)

import ytmusic
from ytmusic import YTError, YTMusic

VIDEO = "Invented0_1"
MASTER = "https://manifest.googlevideo.com/invented/master.m3u8"   # invented addresses, never asked


def row(title="Invented Song", grey=False):
    """A musicResponsiveListItemRenderer for one song, as an album or playlist page has it."""
    found = {
        "flexColumns": [
            {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{
                "text": title, "navigationEndpoint": {"watchEndpoint": {"videoId": VIDEO}}}]}}},
            {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Invented Band"}]}}},
        ],
        "playlistItemData": {"videoId": VIDEO},
    }
    if grey:
        found["musicItemRendererDisplayPolicy"] = "MUSIC_ITEM_RENDERER_DISPLAY_POLICY_GREY_OUT"
    return found


class GreyedOut(unittest.TestCase):
    def test_a_playable_row_is_listed(self):
        item = ytmusic._list_item(row())
        self.assertEqual((item["title"], item["video"]), ("Invented Song", VIDEO))

    def test_a_greyed_out_row_is_left_out(self):
        self.assertIsNone(ytmusic._list_item(row(grey=True)))


class OneSong(unittest.TestCase):
    """_resolve with the player's answer saying OK, and the song's own playlist refused."""

    def resolve(self, refusal):
        answer = json.dumps({"playabilityStatus": {"status": "OK"}, "videoDetails": {"title": "Invented Song"},
                             "streamingData": {"hlsManifestUrl": MASTER}}).encode()

        def fake_open(url, data=None, headers=None, **kwargs):
            if url == ytmusic.PLAYER_API:
                return answer
            raise refusal

        music = YTMusic()
        with mock.patch.object(ytmusic, "_open", fake_open), \
                mock.patch.object(YTMusic, "_session", lambda self: ("1.0", "")):
            with self.assertRaises(YTError) as caught:
                music._resolve(VIDEO)
        return caught.exception

    def test_a_refused_playlist_is_this_songs_failure(self):
        for code in (403, 404, 410, 500):
            error = self.resolve(YTError(ytmusic._problem(code), general=True, code=code))
            self.assertFalse(error.general, code)
            self.assertIn(f"HTTP {code}", str(error))

    def test_slow_down_and_no_network_still_stop_every_song(self):
        self.assertTrue(self.resolve(YTError(ytmusic._problem(429), general=True, code=429)).general)
        self.assertTrue(self.resolve(YTError(ytmusic.OFFLINE, general=True)).general)


if __name__ == "__main__":
    unittest.main()
