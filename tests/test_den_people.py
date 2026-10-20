"""The Den 2.0 Contacts and Team pages: contact types / favourites / links, leave requests, the Review step."""

import inspect
from unittest import mock

from tests.helpers import TestBase  # noqa: I001 - first, so the shell's real API keys are cleared before api/worker load

from fastapi import HTTPException
from fastapi.routing import APIRoute
from pydantic import BaseModel

import clients
import db
import den_people
import team

TODAY = "2026-10-06"          # a Tuesday


class People(TestBase):
    def setUp(self):
        super().setUp()
        for target in (den_people, team):
            p = mock.patch.object(target, "_today", return_value=TODAY)
            p.start()
            self.addCleanup(p.stop)
        self.routes = [r for r in den_people.build_router(lambda: None).routes if isinstance(r, APIRoute)]

    def call(self, method, path, json=None):
        """Run the endpoint for method + path (path parameters filled in) - the router without a
        server (the venv has no httpx for TestClient). Returns (status, body)."""
        for route in self.routes:
            match = route.path_regex.match(path)
            if method in route.methods and match:
                kw = {k: int(v) for k, v in match.groupdict().items()}
                for name, param in inspect.signature(route.endpoint).parameters.items():
                    if isinstance(param.annotation, type) and issubclass(param.annotation, BaseModel):
                        kw[name] = param.annotation(**(json or {}))
                try:
                    return 200, route.endpoint(**kw)
                except HTTPException as e:
                    return e.status_code, e.detail
        raise AssertionError(f"no route {method} {path}")

    def ok(self, method, path, json=None):
        status, out = self.call(method, path, json)
        self.assertEqual(status, 200, out)
        return out

    def status(self, method, path, json=None):
        return self.call(method, path, json)[0]

    def person(self, name="Riya Kapoor", **kw):
        return db.add_employee(team.clean_employee({"name": name, **kw}))

    # ------------------------------------------------------------ contacts

    def test_new_contact_keeps_type_and_matches_company_to_a_client(self):
        acme = db.add_client(clients.clean_client({"name": "Acme", "status": "active"}))
        c = self.ok("POST", "/den/people/contacts", {"name": "Abhishek Rao", "company": "acme", "role": "Founder",
                                                     "email": "a@acme.in", "kind": "client"})
        self.assertEqual((c["client_id"], c["company"], c["kind"], c["fav"]), (acme["id"], "Acme", "client", False))
        v = self.ok("POST", "/den/people/contacts", {"name": "Sana", "company": "Hostinger", "kind": "vendor"})
        self.assertEqual((v["client_id"], v["company"], v["kind"]), (None, "Hostinger", "vendor"))
        self.assertIsNone(db.get_client_by_name("Hostinger"))          # typing a company never makes a client
        self.assertEqual(self.status("POST", "/den/people/contacts", {"name": "X", "kind": "friend"}), 400)
        self.assertEqual(self.status("POST", "/den/people/contacts", {"name": "Y", "email": "nope"}), 400)

    def test_type_is_worked_out_until_one_is_picked(self):
        prospect = db.add_client(clients.clean_client({"name": "Nova", "status": "prospect"}))
        active = db.add_client(clients.clean_client({"name": "Vidya", "status": "active"}))
        self.person(email="riya@den.in")
        ids = {n: db.add_contact(clients.clean_contact(f, new=True))["id"] for n, f in (
            ("lead", {"name": "Ravi", "client_id": prospect["id"]}), ("client", {"name": "Dr Iyer", "client_id": active["id"]}),
            ("team", {"name": "Riya", "email": "Riya@den.in"}), ("none", {"name": "Someone"}))}
        kinds = {c["id"]: c["kind"] for c in den_people.contacts_view()}
        self.assertEqual([kinds[ids[k]] for k in ("lead", "client", "team", "none")], ["lead", "client", "team", "lead"])
        self.ok("PATCH", f"/den/people/contacts/{ids['none']}", {"kind": "vendor", "fav": True, "tags": ["Tax", "Tax", " Weekly call "]})
        c = den_people.contact_view(ids["none"])
        self.assertEqual((c["kind"], c["kind_set"], c["fav"], c["own_tags"]), ("vendor", True, True, ["Tax", "Weekly call"]))

    def test_notes_and_own_fields_save_through_the_contact(self):
        cid = self.ok("POST", "/den/people/contacts", {"name": "Meera"})["id"]
        out = self.ok("PATCH", f"/den/people/contacts/{cid}", {"notes": "Demo Thursday", "city": "Mumbai", "preferred_method": "whatsapp"})
        self.assertEqual((db.get_contact(cid)["notes"], out["city"]), ("Demo Thursday", "Mumbai"))
        self.assertIn("Prefers WhatsApp", out["tags"])                 # automatic tag from the preferred method
        self.assertEqual(self.status("PATCH", "/den/people/contacts/999", {"fav": True}), 404)

    def test_linked_projects_and_deals(self):
        acme = db.add_client(clients.clean_client({"name": "Acme"}))
        site = db.add_project("Acme site", "")
        phase2 = db.add_project("Acme phase 2", "")
        other = db.add_project("Side thing", "")
        db.update_project_record(site["id"], {"client_id": acme["id"], "stage": "active"})
        db.update_project_record(phase2["id"], {"client_id": acme["id"], "stage": "negotiation", "contract_value": 4800})
        c = db.add_contact(clients.clean_contact({"name": "Abhishek", "client_id": acme["id"]}, new=True))
        db.link_project_contact(other["id"], c["id"], "key")
        v = den_people.contact_view(c["id"])
        self.assertEqual({p["name"] for p in v["projects"]}, {"Acme site", "Acme phase 2", "Side thing"})
        self.assertEqual([(d["name"], d["stage"], d["value"]) for d in v["deals"]], [("Acme phase 2", "negotiation", 4800)])
        self.assertIn("Key contact", v["tags"])

    def test_a_deleted_contact_leaves_no_row_behind(self):
        cid = self.ok("POST", "/den/people/contacts", {"name": "Gone", "kind": "vendor"})["id"]
        db.delete_contact(cid)
        self.assertEqual(self.ok("GET", "/den/people/contacts")["contacts"], [])

    # ---------------------------------------------------------------- team

    def test_leave_request_is_pending_until_approved(self):
        e = self.person()
        req = self.ok("POST", "/den/people/leave", {"employee_id": e["id"], "type": "Earned", "start_date": "2026-10-09",
                                                   "end_date": "2026-10-12", "note": "Trip"})
        self.assertEqual((req["status"], req["days"], req["workdays"]), ("pending", 4, 2))   # Fri to Mon
        self.assertEqual(db.list_leave(), [])                           # nothing real until someone says yes
        out = self.ok("POST", f"/den/people/leave/{req['request_id']}/approve")
        self.assertEqual((out["status"], out["type"], out["note"]), ("approved", "Earned", "Trip"))
        self.assertEqual(db.list_leave()[0]["kind"], "vacation")
        self.assertEqual(self.status("POST", f"/den/people/leave/{req['request_id']}/reject"), 409)
        staff = self.ok("GET", "/den/people/team")["staff"][0]
        self.assertEqual((staff["leave_used"], staff["leave_left"]), (2, 10.0))

    def test_rejected_and_unpaid_leave_dont_use_the_allowance(self):
        e = self.person()
        r1 = self.ok("POST", "/den/people/leave", {"employee_id": e["id"], "type": "Casual", "start_date": TODAY})
        r2 = self.ok("POST", "/den/people/leave", {"employee_id": e["id"], "type": "Unpaid", "start_date": "2026-10-07"})
        self.ok("POST", f"/den/people/leave/{r1['request_id']}/reject")
        self.ok("POST", f"/den/people/leave/{r2['request_id']}/approve")
        view = self.ok("GET", "/den/people/team")
        self.assertEqual(sorted(l["status"] for l in view["leave"]), ["approved", "rejected"])
        self.assertEqual(view["staff"][0]["leave_used"], 0)
        self.assertFalse(view["staff"][0]["away_today"])               # the rejected day off doesn't count
        for bad in ({"type": "Holiday", "start_date": TODAY}, {"start_date": "2026-10-09", "end_date": "2026-10-01"}):
            self.assertEqual(self.status("POST", "/den/people/leave", {"employee_id": e["id"], **bad}), 400)
        self.assertEqual(self.status("POST", "/den/people/leave/999/approve"), 404)

    def test_time_off_added_the_old_way_shows_as_approved(self):
        e = self.person()
        db.add_leave(team.clean_leave({"employee_id": e["id"], "kind": "sick", "start_date": TODAY}))
        view = self.ok("GET", "/den/people/team")
        self.assertEqual([(l["type"], l["status"]) for l in view["leave"]], [("Sick", "approved")])
        self.assertTrue(view["staff"][0]["away_today"])

    def test_task_stage_cycle_with_review(self):
        e = self.person()
        t = db.add_employee_task(team.clean_task({"employee_id": e["id"], "title": "Logo options"}, True))
        seen = []
        for stage in ("doing", "review", "done", "todo"):
            seen.append(self.ok("POST", f"/den/people/tasks/{t['id']}/stage", {"stage": stage})["stage"])
            if stage == "review":
                self.assertEqual(db.get_employee_task(t["id"])["status"], "in_progress")   # the rest of the Den sees work under way
                self.assertEqual(self.ok("GET", "/den/people/team")["tasks"][0]["stage"], "review")
            if stage == "done":
                self.assertIsNotNone(db.get_employee_task(t["id"])["done_at"])
        self.assertEqual(seen, ["doing", "review", "done", "todo"])
        self.assertIsNone(db.get_employee_task(t["id"])["done_at"])
        self.assertEqual(self.status("POST", f"/den/people/tasks/{t['id']}/stage", {"stage": "later"}), 400)
        self.assertEqual(self.status("POST", "/den/people/tasks/999/stage", {"stage": "done"}), 404)

    def test_team_view_capacity_skills_and_inactive(self):
        e = self.person(skills="React, CSS; Figma")
        self.person(name="Old Hand", status="inactive")
        self.ok("PATCH", f"/den/people/staff/{e['id']}", {"capacity": 4, "leave_allowance": 15})
        self.assertEqual(self.status("PATCH", f"/den/people/staff/{e['id']}", {"capacity": 0}), 400)
        staff = self.ok("GET", "/den/people/team")["staff"]
        self.assertEqual([s["name"] for s in staff], ["Riya Kapoor"])
        self.assertEqual((staff[0]["capacity"], staff[0]["leave_allowance"], staff[0]["skills_list"]), (4, 15, ["React", "CSS", "Figma"]))

    def test_every_route_needs_the_api_key(self):
        def key():
            pass
        router = den_people.build_router(key)
        self.assertEqual([d.dependency for d in router.dependencies], [key])
        self.assertTrue(all(r.path.startswith("/den/people/") for r in router.routes))
