"""The block on sign-out requests in Apple's windowless view (apple.Page._rules): every address that names a
sign-out is blocked, but a search of your library whose own words do ("Log Off" is a song) is not (#86). The
rules are applied here the way WebKit applies a content rule list: in order, each `url-filter` a regular
expression searched in the whole address without regard to case, and `ignore-previous-rules` undoing the blocks
before it. (The same list, compiled by WebKitGTK 2.52.6 in a view, gave the same answers: see PR for #86.)"""
import json
import re
import unittest

import tests  # noqa: F401  (scratch folders first)

import apple
import spotify


def blocked(rules, address):
    block = False
    for rule in rules:
        if re.search(rule["trigger"]["url-filter"], address, re.I):
            block = {"block": True, "ignore-previous-rules": False}[rule["action"]["type"]]
    return block


SEARCH = "https://amp-api.music.apple.com/v1/me/library/search?types=library-songs&term="


class SignOutRules(unittest.TestCase):
    rules = json.loads(apple.Page._rules())

    def test_only_actions_webkit_knows(self):
        self.assertEqual({rule["action"]["type"] for rule in self.rules}, {"block", "ignore-previous-rules"})
        for rule in self.rules:   # what WebKit's url-filter takes: no alternation, no counted repeats
            self.assertNotRegex(rule["trigger"]["url-filter"], r"[|{}]")

    def test_a_search_for_words_that_name_a_sign_out_goes_through(self):
        for words in ("Log%20Off", "log+off", "Logoff", "Sign-Out", "sign%20out", "Unauthorized", "Night%20Drive"):
            self.assertFalse(blocked(self.rules, SEARCH + words), words)
        self.assertFalse(blocked(self.rules, "https://amp-api.music.apple.com/v1/me/library/songs?limit=100"))

    def test_every_other_sign_out_is_still_blocked(self):
        for address in ("https://music.apple.com/logout", "https://idmsa.apple.com/appleauth/auth/signout",
                        "https://buy.itunes.apple.com/WebObjects/MZFinance.woa/wa/logOff?x=1",
                        "https://music.apple.com/auth?action=logout",
                        "https://amp-api.music.apple.com/v1/me/account?unauthorized=1",
                        "https://amp-api.music.apple.com/v1/me/library/songs?term=logout",
                        "https://amp-api.music.apple.com/v1/me/library/search/logout?term=x",
                        "https://amp-api.music.apple.com/v1/me/library/searchlogout?term=x",
                        "https://logout.apple.com/v1/me/library/search?term=x",
                        "https://amp-api.music.apple.com/signout/v1/me/library/search?term=x"):
            self.assertTrue(blocked(self.rules, address), address)

    def test_spotify_blocks_every_one_as_before(self):
        rules = json.loads(spotify.Page._rules())   # its library sends no words of yours: nothing is let through
        self.assertEqual({rule["action"]["type"] for rule in rules}, {"block"})
        self.assertTrue(blocked(rules, "https://api-partner.spotify.com/pathfinder/v1/query?x=logout"))


if __name__ == "__main__":
    unittest.main()
