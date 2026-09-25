"""Home quick buttons: starters ship by default, the whole list is saved through /settings, and junk is refused."""

import unittest

import db
from tests import test_e2e


class QuickButtons(test_e2e.E2E):
    def test_starters_are_there_and_include_the_sanskrit_paper(self):
        got = self.http("GET", "/settings")["quick_buttons"]
        self.assertEqual(got, db.QUICK_BUTTON_DEFAULTS)
        sanskrit = next(b for b in got if b["id"] == "q-sanskrit")
        self.assertTrue(sanskrit["pic"])

    def test_saves_the_whole_list_and_can_empty_it(self):
        mine = [{"id": "a1", "icon": "🧾", "label": "Bill", "prompt": "Add up this bill", "pic": True}]
        self.assertEqual(self.http("PATCH", "/settings", {"quick_buttons": mine})["quick_buttons"], mine)
        self.assertEqual(self.http("GET", "/settings")["quick_buttons"], mine)
        self.assertEqual(self.http("PATCH", "/settings", {"quick_buttons": []})["quick_buttons"], [])

    def test_other_settings_leave_the_buttons_alone(self):
        before = self.http("GET", "/settings")["quick_buttons"]
        self.http("PATCH", "/settings", {"notify_on_resume": True})
        self.assertEqual(self.http("GET", "/settings")["quick_buttons"], before)

    def test_refuses_bad_buttons(self):
        ok = {"id": "a", "label": "x", "prompt": "y"}
        for bad in ({**ok, "label": ""}, {**ok, "prompt": ""}, {**ok, "label": "x" * 41}, {**ok, "prompt": "y" * 2001}, {"id": "a", "label": "x"}):
            self.assertEqual(self.http("PATCH", "/settings", {"quick_buttons": [bad]})["_status"], 422, bad)
        self.assertEqual(self.http("PATCH", "/settings", {"quick_buttons": [dict(ok, id=str(i)) for i in range(25)]})["_status"], 422)


if __name__ == "__main__":
    unittest.main()
