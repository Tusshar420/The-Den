"""The risk read of a command shown on an approval card."""

import unittest

import risk


def home_scan(command):
    return "home-scan" in risk.assess("Bash", command, None)["tags"]


class HomeScan(unittest.TestCase):
    def test_searches_of_the_whole_home_folder_or_disk_are_flagged(self):
        for c in ("find ~ -maxdepth 6 -name x", "cd /tmp && (ls ~/.npm 2>/dev/null | head -3; find ~ -name pw)",
                  "du -sh $HOME", "grep -rn foo ~/Library", "rg token /Users/work", "find / -name x",
                  "ls -lR ~", 'find "$HOME" -type f', "mdfind -onlyin ~ x"):
            self.assertTrue(home_scan(c), c)
            self.assertEqual(risk.assess("Bash", c, None)["risk"] != "Low", True, c)

    def test_searches_of_one_folder_are_not(self):
        for c in ("find . -name x", "find ~/.npm -name x", "grep -rn foo .", "find ~/Library/Caches/ms-playwright",
                  "rg foo src/", "ls -la ~", "du -sh node_modules", "find /Users/work/TheDen -name x",
                  "rg foo  bar", "find /Library/Fonts"):
            self.assertFalse(home_scan(c), c)
