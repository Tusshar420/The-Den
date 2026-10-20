"""The Den 2.0 CRM records page: one activity log, follow-up channels, won dates, CSV / XLSX / PDF
exports of the four datasets and the weekly-report switch."""

from tests.helpers import TestBase  # noqa: I001 - first, so the shell's real API keys are cleared before api/worker load

import csv
import io
import zipfile

from fastapi import HTTPException

import api
import db
import den_crmx


class CrmRecords(TestBase):
    def setUp(self):
        super().setUp()
        den_crmx._ready = False
        self.acme = api.create_client(api.ClientIn(name="Acme"))
        self.priya = api.create_contact(api.ContactIn(name="Priya Shah", email="priya@acme.example", client_id=self.acme["id"]))
        self.deal = api.create_project(api.ProjectIn(name="KodeAura7 Phase 2", category="work", stage="negotiation",
                                                     contract_value=4800, client_id=self.acme["id"]))
        api.link_project_contact(self.deal["id"], api.ProjectContactIn(contact_id=self.priya["id"], role="key"))

    def status(self, fn, *args):
        with self.assertRaises(HTTPException) as e:
            fn(*args)
        return e.exception.status_code

    def log(self, **body):
        body.setdefault("kind", "call")
        body.setdefault("occurred_at", "2026-10-06T11:20:00+05:30")
        return api.create_activity(api.ActivityIn(**body))

    def test_activities_from_every_subject_in_one_log(self):
        self.log(summary="Walked through stage history", project_id=self.deal["id"], contact_ids=[self.priya["id"]])
        self.log(kind="email", summary="Sent fee proposal", contact_id=self.priya["id"], occurred_at="2026-10-06T09:05:00+05:30")
        self.log(kind="note", summary="Referred by Meera", client_id=self.acme["id"], occurred_at="2026-10-05T12:40:00+05:30")
        rows = den_crmx.all_activities()
        self.assertEqual([r["summary"] for r in rows], ["Walked through stage history", "Sent fee proposal", "Referred by Meera"])
        self.assertEqual((rows[0]["subject"], rows[0]["subject_name"], rows[0]["people"][0]["name"]),
                         ("project", "KodeAura7 Phase 2", "Priya Shah"))
        self.assertEqual(rows[1]["kind_label"], "Email")

    def test_channel_is_kept_per_follow_up_and_checked(self):
        f = api.create_followup(api.FollowupIn(title="Send revised SOW", due_date="2026-10-07", contact_id=self.priya["id"]))
        den_crmx.set_channel(f["id"], "WhatsApp")
        den_crmx.set_channel(f["id"], "Call")                                  # changing it replaces it
        self.assertEqual(den_crmx.channels(), {f["id"]: "Call"})
        self.assertEqual(self.status(den_crmx.set_channel, f["id"], "Fax"), 400)
        self.assertEqual(self.status(den_crmx.set_channel, 999, "Call"), 404)
        db.delete_followup(f["id"])
        self.assertEqual(den_crmx.channels(), {})                              # gone with its follow-up

    def test_won_date_is_the_move_into_a_won_stage(self):
        self.assertEqual(den_crmx.won_dates(), {})                             # negotiation isn't won
        api.update_project(self.deal["id"], api.ProjectActiveIn(stage="active"))
        won = den_crmx.won_dates()
        self.assertEqual(list(won), [self.deal["id"]])
        self.assertEqual(won[self.deal["id"]], den_crmx.timeutil.now().date().isoformat())
        api.update_project(self.deal["id"], api.ProjectActiveIn(stage="done"))   # active -> done is still the same win
        self.assertEqual(den_crmx.won_dates(), won)

    def test_deals_csv_has_contact_probability_and_next_step(self):
        api.create_followup(api.FollowupIn(title="Agree on fee", due_date="2026-10-08", project_id=self.deal["id"]))
        res = den_crmx.export("deals", "csv")
        self.assertIn('filename="den-deals-', res.headers["content-disposition"])
        rows = list(csv.reader(io.StringIO(res.body.decode("utf-8-sig"))))
        head, row = rows[0], rows[1]
        got = dict(zip(head, row))
        self.assertEqual((got["Deal"], got["Client"], got["Contact"], got["Stage"], got["Value"], got["Probability %"], got["Next step"]),
                         ("KodeAura7 Phase 2", "Acme", "Priya Shah", "Negotiation", "4800.0", "70", "Agree on fee"))
        self.assertEqual(len(rows), 2)                                         # General isn't a deal

    def test_csv_cells_cannot_start_a_formula(self):
        self.log(summary="=HYPERLINK(\"http://x\")", project_id=self.deal["id"])
        text = den_crmx.export("activities", "csv").body.decode("utf-8-sig")
        self.assertIn("'=HYPERLINK", text)

    def test_xlsx_is_a_workbook_with_the_rows(self):
        z = zipfile.ZipFile(io.BytesIO(den_crmx.export("contacts", "xlsx").body))
        self.assertIn("xl/workbook.xml", z.namelist())
        sheet = z.read("xl/worksheets/sheet1.xml").decode()
        self.assertIn("Priya Shah", sheet)
        self.assertIn("priya@acme.example", sheet)

    def test_pdf_is_a_pdf_with_every_page_listed(self):
        for i in range(70):                                                    # more than one page of rows
            api.create_followup(api.FollowupIn(title=f"Chase (part {i}) \\ done", due_date="2026-10-09", contact_id=self.priya["id"]))
        body = den_crmx.export("followups", "pdf").body
        self.assertTrue(body.startswith(b"%PDF-1.4"))
        self.assertTrue(body.rstrip().endswith(b"%%EOF"))
        self.assertIn(b"/Count 2", body)
        self.assertIn(b"Chase \\(part 0\\) \\\\ done", body)                    # brackets and backslashes escaped
        self.assertIn(b"startxref", body)

    def test_unknown_dataset_or_format(self):
        self.assertEqual(self.status(den_crmx.export, "invoices", "csv"), 404)
        self.assertEqual(self.status(den_crmx.export, "deals", "docx"), 400)

    def test_weekly_report_choice_is_stored_but_not_scheduled(self):
        self.assertFalse(den_crmx.weekly()["on"])
        out = den_crmx.set_weekly(True)
        self.assertEqual((out["on"], out["day"], out["time"], out["scheduled"]), (True, "mon", "09:00", False))
        self.assertTrue(den_crmx.weekly()["on"])

    def test_every_route_needs_the_api_key(self):
        guard = object()
        r = den_crmx.build_router(lambda: guard)
        self.assertEqual({(m, x.path) for x in r.routes for m in x.methods}, {
            ("GET", "/den/crmx/activities"), ("GET", "/den/crmx/channels"), ("PUT", "/den/crmx/followups/{followup_id}/channel"),
            ("GET", "/den/crmx/won"), ("GET", "/den/crmx/export/{name}"), ("GET", "/den/crmx/weekly"), ("PUT", "/den/crmx/weekly")})
        self.assertTrue(all(len(x.dependencies) == 1 for x in r.routes))
