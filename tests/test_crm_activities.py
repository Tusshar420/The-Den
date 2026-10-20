"""CRM 4: activities and follow-ups.

Activities (call, meeting, email, note, decision) are logged on a project, client or contact with
when, a summary, who was involved and an optional outcome. Follow-ups are dated to-dos, marked done
when finished; open ones past due are overdue and get one push at their due time (held through quiet
hours). Each page's activity timeline mixes activities, follow-ups done, stage changes and (for a
project) finished tasks, newest first; the Projects table's "Last activity" comes from the same."""

from tests.helpers import TZ, TestBase  # noqa: I001 - first, so no test sees real API keys

from datetime import datetime, timedelta, timezone
from unittest import mock

from fastapi import HTTPException

import activities
import api
import db
import worker

# Saturday 3 Oct 2026, 11:00 IST.
NOW = datetime(2026, 10, 3, 11, 0, tzinfo=TZ)


class Base(TestBase):
    def setUp(self):
        super().setUp()
        self.p = api.create_project(api.ProjectIn(name="Acme rebrand"))
        self.cl = api.create_client(api.ClientIn(name="Acme"))
        self.ct = api.create_contact(api.ContactIn(name="Priya Shah", client_id=self.cl["id"]))
        self.ct2 = api.create_contact(api.ContactIn(name="Sam Lee", client_id=self.cl["id"]))

    def log(self, **body):
        body.setdefault("kind", "call")
        body.setdefault("occurred_at", "2026-10-02T15:30")
        body.setdefault("summary", "Talked through the brief")
        if not any(k in body for k in activities.SUBJECTS):
            body["project_id"] = self.p["id"]
        return api.create_activity(api.ActivityIn(**body))

    def follow(self, now=NOW, **body):
        body.setdefault("title", "Send invoice")
        body.setdefault("due_date", "2026-10-05")
        if not any(k in body for k in activities.SUBJECTS):
            body["project_id"] = self.p["id"]
        with mock.patch("activities.timeutil.now", return_value=now):
            return api.create_followup(api.FollowupIn(**body))

    def refused(self, fn, *a, status=400, **kw):
        with self.assertRaises(HTTPException) as cm:
            fn(*a, **kw)
        self.assertEqual(cm.exception.status_code, status, cm.exception.detail)
        return cm.exception.detail


class Activities(Base):
    def test_log_a_call_with_people_and_outcome(self):
        a = self.log(contact_ids=[self.ct["id"], self.ct2["id"], self.ct["id"]], outcome="  They want a quote  ")
        self.assertEqual((a["kind"], a["kind_label"], a["summary"], a["outcome"]), ("call", "Call", "Talked through the brief", "They want a quote"))
        self.assertEqual([x["name"] for x in a["people"]], ["Priya Shah", "Sam Lee"])
        self.assertEqual((a["subject"], a["subject_id"], a["subject_name"]), ("project", self.p["id"], "Acme rebrand"))
        # A local time (no offset) is stored in UTC: 15:30 IST = 10:00 UTC.
        self.assertEqual(datetime.fromisoformat(a["occurred_at"]), datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc))

    def test_every_kind_and_subject(self):
        for kind in activities.KINDS:
            self.log(kind=kind)
        self.assertEqual(self.log(client_id=self.cl["id"])["subject"], "client")
        self.assertEqual(self.log(contact_id=self.ct["id"])["subject"], "contact")

    def test_bad_values_are_refused(self):
        self.assertIn("Pick what it was", self.refused(self.log, kind="lunch"))
        self.assertIn("summary", self.refused(self.log, summary="  "))
        self.assertIn("date and time", self.refused(self.log, occurred_at="yesterday"))
        self.assertIn("one project, client or contact", self.refused(self.log, project_id=self.p["id"], client_id=self.cl["id"]))
        self.refused(self.log, contact_ids=[9999], status=404)
        self.refused(self.log, project_id=9999, status=404)
        general = self.project["id"]
        self.refused(self.log, project_id=general, status=404)
        self.refused(lambda: api.create_activity(api.ActivityIn(kind="call", occurred_at="2026-10-02T10:00", summary="x")))

    def test_edit_and_delete(self):
        a = self.log(contact_ids=[self.ct["id"]])
        b = api.update_activity(a["id"], api.ActivityIn(kind="decision", outcome="Go with option B", contact_ids=[]))
        self.assertEqual((b["kind"], b["outcome"], b["people"], b["summary"]), ("decision", "Go with option B", [], a["summary"]))
        moved = api.update_activity(a["id"], api.ActivityIn(client_id=self.cl["id"]))
        self.assertEqual((moved["subject"], moved["project_id"]), ("client", None))
        api.delete_activity(a["id"])
        self.refused(api.delete_activity, a["id"], status=404)
        self.refused(api.update_activity, a["id"], api.ActivityIn(summary="x"), status=404)


class StageChanges(Base):
    def stages(self, pid):
        return [(x["data"]["from"], x["data"]["to"]) for x in api.crm_feed(project_id=pid)["items"] if x.get("kind") == "stage"]

    def test_moving_the_stage_is_logged(self):
        api.update_project(self.p["id"], api.ProjectActiveIn(stage="proposal"))
        api.update_project(self.p["id"], api.ProjectActiveIn(stage="active"))
        api.update_project(self.p["id"], api.ProjectActiveIn(stage="active"))          # no change: nothing logged
        self.assertEqual(self.stages(self.p["id"]), [("proposal", "active"), ("lead", "proposal")])
        item = api.crm_feed(project_id=self.p["id"])["items"][0]
        self.assertEqual((item["summary"], item["kind_label"]), ("Proposal → Active", "Stage change"))

    def test_a_status_change_that_moves_the_stage_is_logged(self):
        api.update_project(self.p["id"], api.ProjectActiveIn(status="on_hold"))
        self.assertEqual(self.stages(self.p["id"]), [("lead", "on_hold")])

    def test_stage_entries_cant_be_edited_but_can_be_deleted(self):
        api.update_project(self.p["id"], api.ProjectActiveIn(stage="proposal"))
        a = api.crm_feed(project_id=self.p["id"])["items"][0]
        self.assertIn("can be deleted", self.refused(api.update_activity, a["id"], api.ActivityIn(summary="x")))
        api.delete_activity(a["id"])

    def test_first_task_starting_logs_the_automatic_move(self):
        proj = db.add_project("Coded", self._tmp.name)
        t = db.add_task(proj["id"], "build it", 0, None)
        db.mark_running(t["id"])
        self.assertEqual(self.stages(proj["id"]), [("lead", "active")])
        t2 = db.add_task(proj["id"], "again", 0, None)
        db.mark_running(t2["id"])                                                         # already in progress
        self.assertEqual(len(self.stages(proj["id"])), 1)


class Followups(Base):
    def test_add_and_finish(self):
        f = self.follow(due_time="14:00", notes="for September")
        self.assertEqual((f["title"], f["status"], f["due_time"], f["overdue"], f["days_left"], f["reminded_at"]),
                         ("Send invoice", "open", "14:00", False, 2, None))
        self.assertEqual((f["subject"], f["subject_name"]), ("project", "Acme rebrand"))
        with mock.patch("activities.timeutil.now", return_value=NOW):
            done = api.update_followup(f["id"], api.FollowupIn(status="done"))
        self.assertEqual(done["status"], "done")
        self.assertTrue(done["done_at"])
        again = api.update_followup(f["id"], api.FollowupIn(status="open"))
        self.assertIsNone(again["done_at"])

    def test_overdue(self):
        self.assertTrue(self.follow(due_date="2026-10-02")["overdue"])
        self.assertFalse(self.follow(due_date="2026-10-03")["overdue"])                   # all day: due today, not late yet
        self.assertTrue(self.follow(due_date="2026-10-03", due_time="10:30")["overdue"])
        self.assertFalse(self.follow(due_date="2026-10-03", due_time="11:30")["overdue"])

    def test_bad_values_are_refused(self):
        self.assertIn("Give the follow-up", self.refused(self.follow, title=" "))
        self.assertIn("Due date", self.refused(self.follow, due_date="05/10/2026"))
        self.assertIn("real date", self.refused(self.follow, due_date="2026-02-30"))
        self.assertIn("Due time", self.refused(self.follow, due_time="2pm"))
        self.refused(self.follow, contact_id=12345, status=404)
        self.assertIn("open or done", self.refused(api.update_followup, self.follow()["id"], api.FollowupIn(status="later")))

    def test_list_for_home_and_calendar(self):
        a = self.follow(title="Chase feedback", due_date="2026-10-04", contact_id=self.ct["id"])
        b = self.follow(title="Old", due_date="2026-09-01")
        with mock.patch("activities.timeutil.now", return_value=NOW):
            api.update_followup(b["id"], api.FollowupIn(status="done"))
            rows = api.list_followups()
            open_only = api.list_followups(status="open")
        self.assertEqual([r["title"] for r in rows], ["Old", "Chase feedback"])
        self.assertEqual([r["title"] for r in open_only], ["Chase feedback"])
        self.assertEqual((rows[1]["subject"], rows[1]["subject_name"]), ("contact", "Priya Shah"))
        api.delete_followup(a["id"])
        self.refused(api.delete_followup, a["id"], status=404)


class Reminders(Base):
    def pending(self, at):
        with mock.patch("activities.timeutil.now", return_value=at):
            return activities.pending_reminders(at)

    def test_due_at_its_time_or_9am(self):
        timed = self.follow(due_date="2026-10-03", due_time="15:00")
        allday = self.follow(title="Call back", due_date="2026-10-04", client_id=self.cl["id"])
        self.assertEqual(self.pending(NOW), [])
        due = self.pending(NOW.replace(hour=15, minute=1))
        self.assertEqual([r["followup"]["id"] for r in due], [timed["id"]])
        self.assertEqual((due[0]["title"], due[0]["message"], due[0]["open_hash"]),
                         ("Follow-up · Acme rebrand", "Send invoice - due at 3:00 PM.", f"#/project/{self.p['id']}/overview"))
        nine = self.pending(datetime(2026, 10, 4, 9, 0, tzinfo=TZ))
        self.assertEqual(nine[-1]["followup"]["id"], allday["id"])
        self.assertEqual(nine[-1]["open_hash"], f"#/client/{self.cl['id']}")

    def test_one_already_due_when_added_doesnt_push(self):
        self.follow(due_date="2026-10-01")
        self.assertEqual(self.pending(NOW), [])

    def test_redating_starts_the_reminder_over(self):
        f = self.follow(due_date="2026-10-03", due_time="12:00")
        later = NOW.replace(hour=12, minute=5)
        [r] = self.pending(later)
        activities.mark_reminded(r["followup"], later)
        self.assertEqual(self.pending(later), [])
        with mock.patch("activities.timeutil.now", return_value=later):
            api.update_followup(f["id"], api.FollowupIn(due_time="13:00"))
        self.assertEqual(len(self.pending(NOW.replace(hour=13, minute=1))), 1)

    def test_done_ones_dont_remind(self):
        f = self.follow(due_date="2026-10-03", due_time="12:00")
        api.update_followup(f["id"], api.FollowupIn(status="done"))
        self.assertEqual(self.pending(NOW.replace(hour=13)), [])

    def test_worker_pushes_once_and_holds_through_quiet_hours(self):
        self.follow(due_date="2026-10-03", due_time="22:00")
        pushed = []
        late = datetime(2026, 10, 3, 23, 30, tzinfo=TZ)
        morning = datetime(2026, 10, 4, 8, 5, tzinfo=TZ)
        with mock.patch("worker.notify_phone", side_effect=lambda *a, **k: pushed.append((a, k))):
            for at in (late, morning, morning):
                with mock.patch("activities.timeutil.now", return_value=at), mock.patch("worker.datetime") as wdt:
                    wdt.now.return_value = at
                    worker.check_followup_reminders(at)
        self.assertEqual(len(pushed), 1)
        (title, message, kind), extra = pushed[0]
        self.assertEqual((kind, extra["open_hash"]), ("reminder", f"#/project/{self.p['id']}/overview"))


class Feed(Base):
    def test_project_feed_mixes_everything_newest_first(self):
        self.log(summary="Kickoff call", occurred_at="2026-09-20T10:00")
        f = self.follow(title="Send proposal", due_date="2026-09-25")
        with mock.patch("activities.timeutil.now", return_value=NOW):
            api.update_followup(f["id"], api.FollowupIn(status="done"))
        self.follow(title="Chase feedback", due_date="2026-10-10")
        late = self.follow(title="Send invoice", due_date="2026-10-01")
        api.update_project(self.p["id"], api.ProjectActiveIn(stage="proposal"))
        coded = db.add_project("Coded", self._tmp.name)
        t = db.add_task(self.p["id"], "make the logo", 0, None)
        db.mark_done(t["id"], "done")
        with mock.patch("activities.timeutil.now", return_value=NOW):
            out = api.crm_feed(project_id=self.p["id"])
        types = [(x["type"], x.get("summary") or x.get("title")) for x in out["items"]]
        self.assertEqual(sorted(types[:3]), sorted([("activity", "Lead → Proposal"), ("task", "Make the logo"), ("followup", "Send proposal")]))
        self.assertEqual(types[-1], ("activity", "Kickoff call"))
        self.assertEqual([x["title"] for x in out["open"]], ["Send invoice", "Chase feedback"])   # overdue first
        self.assertTrue(out["open"][0]["overdue"])
        self.assertEqual(out["open"][0]["id"], late["id"])
        self.assertEqual(api.crm_feed(project_id=coded["id"]), {"open": [], "items": []})

    def test_contact_feed_has_what_they_were_in(self):
        self.log(summary="Call with Priya", contact_ids=[self.ct["id"]])
        self.log(summary="About Priya", contact_id=self.ct["id"])
        self.log(summary="Not her", contact_ids=[self.ct2["id"]])
        self.follow(title="Email Priya", contact_id=self.ct["id"])
        out = api.crm_feed(contact_id=self.ct["id"])
        self.assertEqual(sorted(x["summary"] for x in out["items"]), ["About Priya", "Call with Priya"])
        self.assertEqual([x["title"] for x in out["open"]], ["Email Priya"])

    def test_client_feed_has_its_projects_and_contacts(self):
        api.update_project(self.p["id"], api.ProjectActiveIn(client_id=self.cl["id"]))
        self.log(summary="On the project")
        self.log(summary="On the client", client_id=self.cl["id"])
        self.log(summary="On a contact", contact_id=self.ct2["id"])
        other = api.create_project(api.ProjectIn(name="Other"))
        self.log(summary="Elsewhere", project_id=other["id"])
        out = api.crm_feed(client_id=self.cl["id"])
        self.assertEqual(sorted(x["summary"] for x in out["items"]), ["On a contact", "On the client", "On the project"])

    def test_asks_for_exactly_one(self):
        self.refused(api.crm_feed)
        self.refused(lambda: api.crm_feed(project_id=self.p["id"], client_id=self.cl["id"]))
        self.refused(lambda: api.crm_feed(contact_id=999), status=404)


class LastActivity(Base):
    def last(self, pid):
        return next(p for p in api.list_projects() if p["id"] == pid)["last_activity_at"]

    def test_comes_from_activities_followups_and_tasks(self):
        self.assertIsNone(self.last(self.p["id"]))
        self.log(occurred_at="2026-09-01T10:00:00+05:30")
        self.assertEqual(datetime.fromisoformat(self.last(self.p["id"])), datetime(2026, 9, 1, 4, 30, tzinfo=timezone.utc))
        self.follow()                                                    # added now (the real clock)
        self.assertGreater(datetime.fromisoformat(self.last(self.p["id"])), datetime.now(timezone.utc) - timedelta(minutes=5))


class Deleting(Base):
    def test_deleting_a_contact_takes_their_activities_and_followups(self):
        a = self.log(contact_id=self.ct["id"])
        shared = self.log(contact_ids=[self.ct["id"], self.ct2["id"]])
        f = self.follow(contact_id=self.ct["id"])
        api.delete_contact(self.ct["id"])
        self.assertIsNone(db.get_crm_activity(a["id"]))
        self.assertIsNone(db.get_followup(f["id"]))
        self.assertEqual(db.get_crm_activity(shared["id"])["contact_ids"], [self.ct2["id"]])

    def test_deleting_a_project_takes_its_activities_and_followups(self):
        a, f = self.log(contact_ids=[self.ct["id"]]), self.follow()
        api.delete_project(self.p["id"], api.ProjectDeleteIn(confirm="Acme rebrand"))
        self.assertIsNone(db.get_crm_activity(a["id"]))
        self.assertIsNone(db.get_followup(f["id"]))

    def test_deleting_a_client_takes_its_own(self):
        a, f = self.log(client_id=self.cl["id"]), self.follow(client_id=self.cl["id"])
        api.delete_client(self.cl["id"])
        self.assertIsNone(db.get_crm_activity(a["id"]))
        self.assertIsNone(db.get_followup(f["id"]))
