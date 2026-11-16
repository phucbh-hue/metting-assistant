# -*- coding: utf-8 -*-
"""3.30.0: nhận link Google Meet / Teams / Zoom cho bot."""
import unittest

from meeting import meet_links
from meeting.meet_bots import key_of, normalize_meet_url


class MeetLinksTest(unittest.TestCase):
    def test_meet(self):
        self.assertEqual(normalize_meet_url("meet.google.com/abc-defg-hij?authuser=0"), "https://meet.google.com/abc-defg-hij")
        self.assertEqual(normalize_meet_url("ABC-DEFG-HIJ"), "https://meet.google.com/abc-defg-hij")
        self.assertEqual(meet_links.code("https://meet.google.com/abc-defg-hij"), "abc-defg-hij")     # khóa cũ không đổi
        self.assertEqual(key_of("https://meet.google.com/abc-defg-hij", 120), "abc-defg-hij|2")

    def test_zoom_keeps_password(self):
        u = "https://us02web.zoom.us/j/81234567890?pwd=abcDEF123"
        self.assertEqual(normalize_meet_url(u), u)
        self.assertEqual(meet_links.platform(u), "zoom")
        self.assertEqual(meet_links.code(u), "zoom:81234567890")
        self.assertEqual(meet_links.describe(u), "Zoom")

    def test_teams(self):
        u = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0?context=%7b%22Tid%22%3a%22t1%22%7d"
        self.assertEqual(normalize_meet_url(u), u)
        self.assertEqual(meet_links.platform(u), "teams")
        self.assertTrue(meet_links.code(u).startswith("teams:"))
        self.assertEqual(normalize_meet_url("https://teams.live.com/meet/9312345678901?p=XyZ"),
                         "https://teams.live.com/meet/9312345678901?p=XyZ")

    def test_invite_text(self):
        txt = ("Microsoft Teams meeting\nJoin on your computer: https://teams.microsoft.com/l/meetup-join/19%3axyz%40thread.v2/0"
               "?context=%7b%22Tid%22%3a%22t1%22%7d\nMeeting ID: 123")
        self.assertTrue(normalize_meet_url(txt).startswith("https://teams.microsoft.com/l/meetup-join/"))
        self.assertEqual(meet_links.find_in_text("Phòng họp tầng 3, xem https://urbox.vn"), "")

    def test_rejects_other(self):
        for bad in ("", "https://urbox.vn/abc", "zoom.us/about", "meet.google.com/landing"):
            with self.assertRaises(ValueError):
                normalize_meet_url(bad)


if __name__ == "__main__":
    unittest.main()
