"""CRM 5: the CRM dashboard and reports.

The dashboard (GET /crm/dashboard) lists projects overdue and due in the next 7 / 30 days, at-risk
and off-track ones, follow-ups due today and overdue, the pipeline by stage (count + value), active
value by client, open projects quiet for 14+ days and workload by due week - for Work / Personal /
All and optionally one client. Reports (GET /crm/reports/{client|project}/{id}) summarise one client
or project as JSON, CSV or Markdown."""

from tests.helpers import TZ, TestBase  # noqa: I001 - first, so no test sees real API keys

import csv
import io
from datetime import datetime
from unittest import mock

from fastapi import HTTPException

import api
import crm_reports
import db

# Saturday 3 Oct 2026, 11:00 IST. The week runs Mon 28 Sep - Sun 4 Oct.
NOW = datetime(2026, 10, 3, 11, 0, tzinfo=TZ)


class Base(TestBase):
    def setUp(self):
        super().setUp()
        self.acme = api.create_client(api.ClientIn(name="Acme"))
        self.globex = api.create_client(api.ClientIn(name="Globex"))
        self.priya = api.create_contact(api.ContactIn(name="Priya Shah", role="CTO", client_id=self.acme["id"]))

    def make(self, name, **body):
        body.setdefault("category", "work")
        p = api.create_project(api.ProjectIn(name=name, **body))
        # Created long enough ago not to count as "quiet" by accident - tests that need it set it themselves.
        self.created(p["id"], "2026-10-01T00:00:00+00:00")
        return p

    def created(self, pid, iso):
        conn = db.get_conn()
        conn.execute("UPDATE projects SET created_at = ? WHERE id = ?", (iso, pid))
        conn.commit()

    def follow(self, **body):
        body.setdefault("title", "Chase feedback")
        with mock.patch("activities.timeutil.now", return_value=NOW):
            return api.create_followup(api.FollowupIn(**body))

    def dash(self, scope="all", client_id=None):
        with mock.patch("crm_reports.timeutil.now", return_value=NOW), mock.patch("activities.timeutil.now", return_value=NOW):
            return api.crm_dashboard(scope, client_id)

    def refused(self, fn, *a, status=400):
        with self.assertRaises(HTTPException) as cm:
            fn(*a)
        self.assertEqual(cm.exception.status_code, status, cm.exception.detail)
        return cm.exception.detail


names = lambda rows: [r["name"] for r in rows]


class Dashboard(Base):
    def test_due_windows_and_risk(self):
        self.make("Late", due_date="2026-10-01", stage="active")
        self.make("Today", due_date="2026-10-03", stage="active")
        self.make("Next week", due_date="2026-10-10", stage="proposal")
        self.make("This month", due_date="2026-11-02", stage="active", health="at_risk")
        self.make("Far", due_date="2026-12-31", stage="active", health="off_track")
        self.make("Finished late", due_date="2026-09-01", stage="done")
        self.make("Lost late", due_date="2026-09-01", stage="lost", health="off_track")
        d = self.dash()
        self.assertEqual(names(d["overdue"]), ["Late"])
        self.assertEqual(d["overdue"][0]["days_left"], -2)
        self.assertEqual(names(d["due_week"]), ["Today", "Next week"])
        self.assertEqual(names(d["due_month"]), ["This month"])
        # Off track before at risk; done and lost projects never count.
        self.assertEqual(names(d["at_risk"]), ["Far", "This month"])
        self.assertEqual({k: d["counts"][k] for k in ("projects", "open", "overdue", "due_week", "due_month", "at_risk")},
                         {"projects": 7, "open": 5, "overdue": 1, "due_week": 2, "due_month": 1, "at_risk": 2})

    def test_general_and_hidden_projects_stay_out(self):
        p = self.make("Secret", due_date="2026-10-01")
        db.set_project_hidden(p["id"], True)
        d = self.dash()
        self.assertEqual((d["counts"]["projects"], d["overdue"]), (0, []))

    def test_pipeline_counts_and_value_per_currency(self):
        self.make("A", stage="lead", contract_value=1000, currency="USD")
        self.make("B", stage="lead", contract_value=500, currency="USD")
        self.make("C", stage="lead", contract_value=300000, currency="INR")
        self.make("D", stage="active")
        pipe = {s["stage"]: s for s in self.dash()["pipeline"]}
        self.assertEqual(list(pipe), ["lead", "proposal", "negotiation", "active", "on_hold", "done", "lost"])
        self.assertEqual(pipe["lead"]["count"], 3)
        self.assertEqual(pipe["lead"]["value"], [{"currency": "INR", "total": 300000.0}, {"currency": "USD", "total": 1500.0}])
        self.assertEqual((pipe["active"]["count"], pipe["active"]["value"]), (1, []))
        self.assertEqual(pipe["done"]["count"], 0)

    def test_active_value_by_client(self):
        self.make("Acme site", client_id=self.acme["id"], stage="active", contract_value=2000)
        self.make("Acme app", client_id=self.acme["id"], stage="on_hold", contract_value=1000)
        self.make("Acme lead", client_id=self.acme["id"], stage="lead", contract_value=9000)
        self.make("Globex", client_id=self.globex["id"], stage="active", contract_value=5000)
        self.make("Own", stage="active", contract_value=100)
        rows = self.dash()["by_client"]
        self.assertEqual([r["client_name"] for r in rows], ["Globex", "Acme", None])
        acme = rows[1]
        self.assertEqual((acme["active_projects"], acme["open_projects"], acme["value"]), (2, 3, [{"currency": "USD", "active": 3000.0}]))

    def test_client_value_ranked_in_the_main_currency(self):
        # Most values are in USD, so a big INR figure doesn't put Globex first.
        self.make("Acme site", client_id=self.acme["id"], stage="active", contract_value=2000, currency="USD")
        self.make("Acme app", client_id=self.acme["id"], stage="active", contract_value=1000, currency="USD")
        self.make("Globex", client_id=self.globex["id"], stage="active", contract_value=300000, currency="INR")
        d = self.dash()
        self.assertEqual(d["currency"], "USD")
        self.assertEqual([r["client_name"] for r in d["by_client"]], ["Acme", "Globex"])
        self.assertEqual(d["by_client"][1]["value"], [{"currency": "INR", "active": 300000.0}])

    def test_quiet_projects(self):
        quiet = self.make("Quiet", stage="active")
        self.created(quiet["id"], "2026-09-01T00:00:00+00:00")
        busy = self.make("Busy", stage="active")
        self.created(busy["id"], "2026-09-01T00:00:00+00:00")
        api.create_activity(api.ActivityIn(kind="call", occurred_at="2026-10-01T10:00", summary="Catch-up", project_id=busy["id"]))
        old_call = self.make("Old call", stage="lead")
        self.created(old_call["id"], "2026-08-01T00:00:00+00:00")
        api.create_activity(api.ActivityIn(kind="call", occurred_at="2026-09-15T10:00", summary="Intro", project_id=old_call["id"]))
        done = self.make("Done", stage="done")
        self.created(done["id"], "2026-01-01T00:00:00+00:00")
        self.make("New", stage="active")       # created 1 Oct: not quiet yet
        d = self.dash()
        self.assertEqual(names(d["stale"]), ["Quiet", "Old call"])
        self.assertEqual([r["idle_days"] for r in d["stale"]], [32, 18])
        self.assertEqual(d["stale"][1]["last_activity_at"][:10], "2026-09-15")

    def test_followups_due_today_and_overdue(self):
        p = self.make("Site", stage="active")
        self.follow(title="Late one", due_date="2026-10-01", project_id=p["id"])
        self.follow(title="Today", due_date="2026-10-03", client_id=self.acme["id"])
        self.follow(title="Today, past its time", due_date="2026-10-03", due_time="09:00", contact_id=self.priya["id"])
        self.follow(title="Tomorrow", due_date="2026-10-04", project_id=p["id"])
        fu = self.follow(title="Done already", due_date="2026-10-01", project_id=p["id"])
        with mock.patch("activities.timeutil.now", return_value=NOW):
            api.update_followup(fu["id"], api.FollowupIn(status="done"))
        d = self.dash()
        self.assertEqual([f["title"] for f in d["followups"]["overdue"]], ["Late one", "Today, past its time"])
        self.assertEqual([f["title"] for f in d["followups"]["today"]], ["Today"])
        self.assertEqual(d["counts"]["followups_due"], 3)

    def test_workload_by_due_week(self):
        self.make("Late", due_date="2026-09-20", stage="active")
        self.make("This week", due_date="2026-10-04", stage="active", contract_value=10)
        self.make("Week after", due_date="2026-10-05", stage="active")
        self.make("Too far", due_date="2027-03-01", stage="active")
        self.make("Done", due_date="2026-10-02", stage="done")
        self.follow(title="Mid next week", due_date="2026-10-07", client_id=self.acme["id"])
        self.follow(title="Late", due_date="2026-09-30", client_id=self.acme["id"])
        wl = self.dash()["workload"]
        self.assertEqual(len(wl["weeks"]), crm_reports.WORKLOAD_WEEKS)
        w0, w1 = wl["weeks"][:2]
        self.assertEqual((w0["start"], w0["end"]), ("2026-09-28", "2026-10-04"))
        self.assertEqual((names(w0["projects"]), w0["followups"], w0["value"]), (["This week"], 0, [{"currency": "USD", "total": 10.0}]))
        self.assertEqual((names(w1["projects"]), w1["followups"]), (["Week after"], 1))
        self.assertEqual((names(wl["overdue"]["projects"]), wl["overdue"]["followups"]), (["Late"], 1))
        self.assertNotIn("Too far", [p["name"] for w in wl["weeks"] for p in w["projects"]])


class Filters(Base):
    def setUp(self):
        super().setUp()
        self.work = self.make("Work job", category="work", client_id=self.acme["id"], due_date="2026-10-01")
        self.home = self.make("Home job", category="personal", due_date="2026-10-02")
        self.other = self.make("Globex job", category="work", client_id=self.globex["id"], due_date="2026-10-02")
        self.follow(title="On work", due_date="2026-10-01", project_id=self.work["id"])
        self.follow(title="On home", due_date="2026-10-01", project_id=self.home["id"])
        self.follow(title="On Acme", due_date="2026-10-01", client_id=self.acme["id"])
        self.follow(title="On Priya", due_date="2026-10-01", contact_id=self.priya["id"])
        loner = api.create_contact(api.ContactIn(name="No client"))
        self.follow(title="On loner", due_date="2026-10-01", contact_id=loner["id"])

    def fus(self, d):
        return sorted(f["title"] for f in d["followups"]["overdue"])

    def test_scope(self):
        d = self.dash("work")
        self.assertEqual(names(d["overdue"]), ["Work job", "Globex job"])
        self.assertEqual(self.fus(d), ["On Acme", "On Priya", "On work"])
        d = self.dash("personal")
        self.assertEqual((names(d["overdue"]), self.fus(d)), (["Home job"], ["On home"]))
        self.assertEqual(len(self.dash("all")["followups"]["overdue"]), 5)

    def test_client(self):
        d = self.dash("all", self.acme["id"])
        self.assertEqual(names(d["overdue"]), ["Work job"])
        self.assertEqual(self.fus(d), ["On Acme", "On Priya", "On work"])
        self.assertEqual(d["client_id"], self.acme["id"])
        self.assertEqual(self.dash("personal", self.acme["id"])["overdue"], [])

    def test_bad_filters(self):
        self.assertIn("scope", self.refused(api.crm_dashboard, "office", None))
        self.assertIn("client", self.refused(api.crm_dashboard, "all", 999))


class Reports(Base):
    def setUp(self):
        super().setUp()
        self.p = self.make("Acme | rebrand", client_id=self.acme["id"], stage="lead", contract_value=7000, currency="USD",
                              due_date="2026-10-01", health="at_risk", description="New logo and site", tags=["design"],
                              links=[{"label": "Figma", "url": "figma.com/acme"}])
        api.update_project(self.p["id"], api.ProjectActiveIn(stage="active"))
        api.link_project_contact(self.p["id"], api.ProjectContactIn(contact_id=self.priya["id"], role="key"))
        api.create_activity(api.ActivityIn(kind="meeting", occurred_at="2026-10-02T15:30", summary="=Kick-off, all good",
                                           outcome="Go ahead", contact_ids=[self.priya["id"]], project_id=self.p["id"]))
        api.create_activity(api.ActivityIn(kind="email", occurred_at="2026-10-02T16:00", summary="Sent the contract", client_id=self.acme["id"]))
        self.follow(title="Send invoice", due_date="2026-10-05", project_id=self.p["id"])
        self.other = self.make("Acme app", client_id=self.acme["id"], stage="on_hold", contract_value=500)

    def report(self, kind, item_id, fmt="json"):
        with mock.patch("crm_reports.timeutil.now", return_value=NOW), mock.patch("activities.timeutil.now", return_value=NOW):
            return api.crm_report(kind, item_id, fmt)

    def test_project_report(self):
        r = self.report("project", self.p["id"])
        self.assertEqual((r["kind"], r["name"], r["project"]["client_name"], r["project"]["stage"]), ("project", "Acme | rebrand", "Acme", "active"))
        self.assertEqual([(h["from"], h["to"]) for h in r["stage_history"]], [("lead", "active")])
        self.assertEqual([a["summary"] for a in r["activities"]], ["=Kick-off, all good"])   # the client's email isn't on the project
        self.assertEqual([f["title"] for f in r["followups"]], ["Send invoice"])
        self.assertEqual([(c["name"], c["project_role"]) for c in r["contacts"]], [("Priya Shah", "key")])
        self.assertEqual((r["schedule"]["due_state"], r["progress"]["percent"]), ("overdue", None))

    def test_client_report(self):
        r = self.report("client", self.acme["id"])
        self.assertEqual(names(r["projects"]), ["Acme app", "Acme | rebrand"])
        self.assertEqual(r["value"], [{"currency": "USD", "total": 7500.0, "active": 7500.0}])
        self.assertEqual(sorted(a["summary"] for a in r["activities"]), ["=Kick-off, all good", "Sent the contract"])
        self.assertEqual([h["project"] for h in r["stage_history"]], ["Acme | rebrand"])
        self.assertEqual([c["name"] for c in r["contacts"]], ["Priya Shah"])

    def test_markdown(self):
        res = self.report("project", self.p["id"], "md")
        self.assertIn("text/markdown", res.media_type)
        self.assertIn('filename="acme-rebrand-project-report-2026-10-03.md"', res.headers["content-disposition"])
        md = res.body.decode()
        self.assertTrue(md.startswith("# Project report: Acme | rebrand"))
        self.assertIn("- **Contract value:** USD 7,000", md)
        self.assertIn("- **Overdue by:** 2 days", md)
        self.assertIn("## Stage history", md)
        self.assertIn("| Lead | Active |", md)
        self.assertIn("[Figma](https://figma.com/acme)", md)
        self.assertIn("| 2026-10-02 15:30 | Meeting | Acme \\| rebrand | =Kick-off, all good | Go ahead | Priya Shah |", md)
        client_md = self.report("client", self.acme["id"], "md").body.decode()
        self.assertIn("- **Active value:** USD 7,500", client_md)
        self.assertIn("## Projects", client_md)

    def test_csv(self):
        res = self.report("client", self.acme["id"], "csv")
        self.assertIn("text/csv", res.media_type)
        rows = list(csv.reader(io.StringIO(res.body.decode())))
        self.assertEqual(rows[0], ["Client report", "Acme"])
        self.assertIn(["Total value USD (number)", "7500.0"], rows)
        head = rows.index(["Projects"])
        self.assertEqual(rows[head + 1], ["Project", "Stage", "Status", "Health", "Due", "Value", "Progress"])
        self.assertEqual(rows[head + 3][:2], ["Acme | rebrand", "Active"])
        # A cell a spreadsheet would run as a formula is kept as text.
        self.assertTrue(any("'=Kick-off, all good" in row for row in rows))

    def test_empty_sections_and_errors(self):
        bare = self.make("Bare")
        md = self.report("project", bare["id"], "md").body.decode()
        self.assertIn("## Activities\n\n_None yet._", md)
        self.refused(self.report, "project", 999, "md", status=404)
        self.refused(self.report, "project", self.project["id"], "json", status=404)   # General has no report
        self.refused(self.report, "client", 999, "csv", status=404)
        self.assertIn("format", self.refused(self.report, "client", self.acme["id"], "pdf"))
