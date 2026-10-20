"""CRM 3: clients (accounts) and contacts.

Clients have a name (unique, any case), industry, website, address, notes, status and a logo or
colour. Contacts have a role, email, phone, notes and preferred method, and usually a client.
Projects are linked to a client (client_id on the project record) and contacts to projects as the
key contact (one per project) or stakeholders. The client page adds up its projects' value."""

from tests.helpers import TestBase  # noqa: I001 - first, so no test sees real API keys

import asyncio
from pathlib import Path

from fastapi import HTTPException

import api
import clients
import crm
import db
import project_images

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class ClientBase(TestBase):
    def setUp(self):
        super().setUp()
        self._old_dir, project_images.IMAGE_DIR = project_images.IMAGE_DIR, Path(self._tmp.name) / "img"
        self.addCleanup(lambda: setattr(project_images, "IMAGE_DIR", self._old_dir))
        self.general = self.project

    def client(self, name="Acme", **body):
        return api.create_client(api.ClientIn(name=name, **body))

    def contact(self, name="Priya Shah", **body):
        return api.create_contact(api.ContactIn(name=name, **body))

    def new_project(self, name="Acme rebrand", **body):
        return api.create_project(api.ProjectIn(name=name, **body))

    def patch_project(self, pid, **body):
        return api.update_project(pid, api.ProjectActiveIn(**body))

    def refused(self, fn, *a, status=400):
        with self.assertRaises(HTTPException) as cm:
            fn(*a)
        self.assertEqual(cm.exception.status_code, status, cm.exception.detail)
        return cm.exception.detail


class Clients(ClientBase):
    def test_create_with_every_field(self):
        c = self.client("  Acme   Corp ", industry="Retail", website="acme.example.com", address="  1 Main St \n  Springfield ",
                        notes="Pays **on time**", status="active", color="#4F9DE8")
        self.assertEqual((c["name"], c["industry"], c["website"]), ("Acme Corp", "Retail", "https://acme.example.com"))
        self.assertEqual((c["address"], c["notes"], c["status"], c["color"]), ("1 Main St\nSpringfield", "Pays **on time**", "active", "#4f9de8"))
        self.assertEqual((c["project_count"], c["contact_count"], c["value"], c["projects"], c["contacts"]), (0, 0, [], [], []))

    def test_new_clients_are_prospects(self):
        self.assertEqual(self.client()["status"], "prospect")

    def test_bad_values_are_refused_and_nothing_is_saved(self):
        for body in ({"name": ""}, {"name": "   "}, {"name": "x" * 200}, {"name": "A", "status": "gold"},
                     {"name": "A", "website": "not a site"}, {"name": "A", "color": "blue"}, {"name": "A", "notes": "x" * 30_000}):
            self.refused(api.create_client, api.ClientIn(**body))
        self.assertEqual(db.list_clients(), [])
        self.refused(api.create_client, api.ClientIn(industry="Retail"))         # no name at all

    def test_names_are_unique_whatever_the_case(self):
        a = self.client("Acme")
        self.assertIn("already a client", self.refused(api.create_client, api.ClientIn(name="ACME")))
        b = self.client("Globex")
        self.refused(api.update_client, b["id"], api.ClientIn(name="acme"))
        self.assertEqual(api.update_client(a["id"], api.ClientIn(name="ACME"))["name"], "ACME")   # renaming itself is fine

    def test_patch_changes_only_what_is_sent_and_null_clears(self):
        c = self.client(industry="Retail", website="acme.example.com", color="#e8825a")
        c = api.update_client(c["id"], api.ClientIn(status="past", website=None))
        self.assertEqual((c["status"], c["website"], c["industry"], c["color"]), ("past", None, "Retail", "#e8825a"))
        self.refused(api.update_client, c["id"], api.ClientIn(status="nope"))
        self.refused(api.update_client, 999, api.ClientIn(status="past"), status=404)

    def test_logo_upload_and_removal(self):
        c = self.client()
        req = type("R", (), {"stream": lambda self: _chunks(PNG)})()
        c = asyncio.run(api.upload_client_logo(c["id"], req))
        self.assertTrue(c["logo"].startswith(f"c{c['id']}-") and c["logo"].endswith(".png"))
        self.assertTrue((project_images.IMAGE_DIR / c["logo"]).exists())
        stored = c["logo"]
        c = api.delete_client_logo(c["id"])
        self.assertIsNone(c["logo"])
        self.assertFalse((project_images.IMAGE_DIR / stored).exists())

    def test_a_client_with_projects_cant_be_deleted_and_contacts_outlive_it(self):
        c = self.client()
        p = self.new_project(client_id=c["id"])
        person = self.contact(client_id=c["id"])
        self.assertIn("1 project belongs", self.refused(api.delete_client, c["id"], status=409))
        self.patch_project(p["id"], client_id=None)
        api.delete_client(c["id"])
        self.assertIsNone(db.get_client(c["id"]))
        self.assertIsNone(db.get_contact(person["id"])["client_id"])


class ProjectClient(ClientBase):
    def test_pick_change_and_clear_a_projects_client(self):
        acme, globex = self.client("Acme"), self.client("Globex")
        p = self.patch_project(self.new_project()["id"], client_id=acme["id"])
        self.assertEqual((p["client_id"], p["client_name"], p["client"]["name"]), (acme["id"], "Acme", "Acme"))
        p = self.patch_project(p["id"], client_id=globex["id"])
        self.assertEqual(p["client"]["id"], globex["id"])
        p = self.patch_project(p["id"], client_id=None)
        self.assertEqual((p["client_id"], p["client"], p["client_name"]), (None, None, None))

    def test_a_client_that_doesnt_exist_is_refused(self):
        p = self.new_project()
        self.assertIn("doesn't exist", self.refused(api.update_project, p["id"], api.ProjectActiveIn(client_id=404)))
        self.assertIsNone(db.get_project(p["id"])["client_id"])

    def test_create_a_project_for_a_client(self):
        c = self.client()
        p = self.new_project(client_id=c["id"])
        self.assertEqual(db.get_project(p["id"])["client_id"], c["id"])

    def test_project_list_carries_the_client_name(self):
        c = self.client("Acme")
        mine = self.new_project(client_id=c["id"])
        rows = {p["id"]: p for p in api.list_projects()}
        self.assertEqual(rows[mine["id"]]["client_name"], "Acme")
        self.assertIsNone(rows[self.general["id"]]["client_name"])   # General: no client

    def test_general_has_no_client(self):
        c = self.client()
        self.refused(api.update_project, self.general["id"], api.ProjectActiveIn(client_id=c["id"]))


class Contacts(ClientBase):
    def test_create_with_every_field(self):
        c = self.client()
        p = self.contact(" Priya  Shah ", role="Head of marketing", email="priya@acme.example.com", phone="+44 (0)20 7946-0000",
                         notes="Prefers mornings", preferred_method="whatsapp", client_id=c["id"])
        self.assertEqual((p["name"], p["role"], p["email"], p["phone"]), ("Priya Shah", "Head of marketing", "priya@acme.example.com", "+44 (0)20 7946-0000"))
        self.assertEqual((p["notes"], p["preferred_method"], p["client_id"], p["project_ids"]), ("Prefers mornings", "whatsapp", c["id"], []))

    def test_bad_values_are_refused(self):
        for body in ({"name": ""}, {"name": "A", "email": "nope"}, {"name": "A", "phone": "call me"}, {"name": "A", "phone": "+1"},
                     {"name": "A", "preferred_method": "pigeon"}, {"name": "A", "client_id": 77}):
            self.refused(api.create_contact, api.ContactIn(**body))
        self.assertEqual(db.list_contacts(), [])

    def test_patch_and_delete(self):
        p = self.contact(email="a@b.co")
        p = api.update_contact(p["id"], api.ContactIn(role="CEO", email=None, preferred_method="phone"))
        self.assertEqual((p["role"], p["email"], p["preferred_method"], p["name"]), ("CEO", None, "phone", "Priya Shah"))
        self.refused(api.update_contact, p["id"], api.ContactIn(name=""))
        api.delete_contact(p["id"])
        self.assertIsNone(db.get_contact(p["id"]))
        self.refused(api.delete_contact, p["id"], status=404)

    def test_quick_add_from_a_project_links_it_and_takes_the_projects_client(self):
        c = self.client()
        proj = self.new_project(client_id=c["id"])
        person = self.contact(project_id=proj["id"], project_role="key")
        self.assertEqual((person["client_id"], person["project_ids"]), (c["id"], [proj["id"]]))
        rec = api.get_project(proj["id"])
        self.assertEqual([(x["name"], x["project_role"]) for x in rec["contacts"]], [("Priya Shah", "key")])
        loose = self.contact("Sam", project_id=proj["id"], client_id=None)       # client_id sent as null wins
        self.assertIsNone(loose["client_id"])
        self.refused(api.create_contact, api.ContactIn(name="X", project_id=proj["id"], project_role="boss"))
        self.refused(api.create_contact, api.ContactIn(name="X", project_id=999), status=404)
        self.assertEqual(len(db.list_contacts()), 2)                              # refused ones weren't saved

    def test_one_key_contact_per_project(self):
        proj = self.new_project()
        a, b = self.contact("Ann"), self.contact("Bob")
        api.link_project_contact(proj["id"], api.ProjectContactIn(contact_id=a["id"], role="key"))
        rec = api.link_project_contact(proj["id"], api.ProjectContactIn(contact_id=b["id"], role="key"))
        self.assertEqual([(x["name"], x["project_role"]) for x in rec["contacts"]], [("Bob", "key"), ("Ann", "stakeholder")])
        rec = api.link_project_contact(proj["id"], api.ProjectContactIn(contact_id=b["id"]))      # back to stakeholder
        self.assertEqual({x["project_role"] for x in rec["contacts"]}, {"stakeholder"})
        rec = api.unlink_project_contact(proj["id"], a["id"])
        self.assertEqual([x["name"] for x in rec["contacts"]], ["Bob"])
        self.assertIsNotNone(db.get_contact(a["id"]))                                              # unlinking keeps the contact
        self.refused(api.unlink_project_contact, proj["id"], a["id"], status=404)
        self.refused(api.link_project_contact, proj["id"], api.ProjectContactIn(contact_id=a["id"], role="boss"))
        self.refused(api.link_project_contact, proj["id"], api.ProjectContactIn(contact_id=999), status=404)

    def test_deleting_a_contact_or_a_project_drops_their_links(self):
        proj, other = self.new_project(), self.new_project("Other")
        a = self.contact("Ann", project_id=proj["id"])
        api.link_project_contact(other["id"], api.ProjectContactIn(contact_id=a["id"]))
        api.delete_project(other["id"], api.ProjectDeleteIn(confirm="Other"))
        self.assertEqual(db.list_contacts()[0]["project_ids"], [proj["id"]])
        api.delete_contact(a["id"])
        self.assertEqual(api.get_project(proj["id"])["contacts"], [])


class ClientPage(ClientBase):
    def test_projects_contacts_and_value(self):
        c = self.client("Acme")
        self.new_project("Site", client_id=c["id"], stage="active", contract_value=5000, currency="USD", due_date="2026-12-01")
        self.new_project("App", client_id=c["id"], stage="on_hold", contract_value=2000, currency="USD")
        self.new_project("Audit", client_id=c["id"], stage="done", contract_value=1000, currency="USD")
        self.new_project("Pitch", client_id=c["id"], stage="lost", contract_value=9000, currency="USD")
        self.new_project("Retainer", client_id=c["id"], stage="proposal", contract_value=300000, currency="INR")
        self.new_project("Unpriced", client_id=c["id"], stage="active")
        self.new_project("Someone else's", contract_value=777)
        self.contact("Ann", client_id=c["id"])
        page = api.get_client(c["id"])
        self.assertEqual([p["name"] for p in page["projects"]], ["App", "Audit", "Pitch", "Retainer", "Site", "Unpriced"])
        site = next(p for p in page["projects"] if p["name"] == "Site")
        self.assertEqual((site["stage"], site["due_date"], site["contract_value"]), ("active", "2026-12-01", 5000))
        self.assertIn("due_state", site["schedule"])
        self.assertEqual(page["value"], [{"currency": "INR", "total": 300000, "active": 0},
                                         {"currency": "USD", "total": 8000, "active": 7000}])
        self.assertEqual((page["project_count"], page["active_projects"], page["contact_count"]), (6, 3, 1))
        self.assertEqual([x["name"] for x in page["contacts"]], ["Ann"])
        row = next(x for x in api.list_clients() if x["id"] == c["id"])
        self.assertEqual((row["project_count"], row["contact_count"], row["value"]), (6, 1, page["value"]))
        self.refused(api.get_client, 999, status=404)

    def test_value_counts_stage_from_status_when_none_was_picked(self):
        self.assertEqual(clients.value([{"contract_value": 10, "currency": "EUR", "status": "in_progress"},
                                        {"contract_value": 5, "currency": "EUR", "status": "archived"}]),
                         [{"currency": "EUR", "total": 15, "active": 10}])


class Migration(TestBase):
    def test_the_tables_are_added_to_an_existing_database_without_touching_it(self):
        conn = db.get_conn()
        for t in ("project_contacts", "contacts", "clients"):
            conn.execute(f"DROP TABLE {t}")     # an older database (only in this throwaway copy)
        conn.commit()
        db._conn = None
        db.get_conn()
        tables = {r[0] for r in db.get_conn().execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"clients", "contacts", "project_contacts"} <= tables)
        self.assertEqual(db.get_project(self.project["id"])["name"], "General")


class RecordHelpers(TestBase):
    def test_web_address_is_shared_with_links(self):
        self.assertEqual(crm.web_address("acme.example.com/x"), "https://acme.example.com/x")
        with self.assertRaises(crm.RecordError):
            crm.web_address("mailto:a@b.co")


async def _gen(data):
    yield data


def _chunks(data):
    return _gen(data)
