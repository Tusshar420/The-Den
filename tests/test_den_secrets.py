"""The Den 2.0 secrets: the PIN only opens a short session, wrong PINs lock the gate, and
nothing secret is stored readable in the database or the vault folder."""

from fastapi import HTTPException

from tests.helpers import TestBase  # noqa: I001 - first, so the shell's real API keys are cleared before api/worker load

import db
import den_secrets as ds


class Secrets(TestBase):
    def setUp(self):
        super().setUp()
        ds._ready = False
        ds._key_cache.clear()
        ds.lock_all()

    def pin(self, new, current=None, scope="both"):
        return ds.set_pin(ds.PinIn(scope=scope, current=current, new=new))

    def code(self, fn, *a):
        with self.assertRaises(HTTPException) as e:
            fn(*a)
        return e.exception.status_code

    def test_no_pin_means_no_unlock_and_no_access(self):
        self.assertEqual(self.code(ds.unlock, "vault", "1234"), 409)
        self.assertEqual(self.code(ds.require, "vault", "made-up"), 401)

    def test_unlock_opens_only_its_own_scope_and_lock_closes_it(self):
        self.pin("4821")
        t = ds.unlock("vault", "4821")["token"]
        ds.require("vault", t)
        self.assertEqual(self.code(ds.require, "creds", t), 401)
        ds.lock_all()
        self.assertEqual(self.code(ds.require, "vault", t), 401)

    def test_changing_a_pin_needs_the_current_one(self):
        self.pin("4821")
        self.assertEqual(self.code(self.pin, "1111", "0000"), 403)
        self.pin("1111", "4821")
        self.assertEqual(self.code(ds.unlock, "vault", "4821"), 403)
        ds.unlock("vault", "1111")

    def test_wrong_pins_lock_the_gate(self):
        self.pin("4821")
        for _ in range(ds.FREE_TRIES):
            self.assertEqual(self.code(ds.unlock, "vault", "0000"), 403)
        self.assertEqual(self.code(ds.unlock, "vault", "4821"), 429)    # even the right one, while locked out

    def test_separate_pins(self):
        self.pin("4821")
        ds.update_settings(ds.SettingsIn(one_pin=False))          # credentials start with the shared PIN
        ds.unlock("creds", "4821")
        self.pin("7777", "4821", scope="creds")
        self.assertEqual(self.code(ds.unlock, "creds", "4821"), 403)
        ds.unlock("creds", "7777")
        ds.unlock("vault", "4821")

    def test_credentials_are_encrypted_and_password_kept_out_of_lists(self):
        c = ds.add_cred(self.project["id"], ds.CredIn(label="Vercel", platform="Vercel", url="https://vercel.com/login",
                                                      username="tushar@example.com", password="s3cret-pass"))
        self.assertNotIn("password", c)
        self.assertEqual(ds.list_creds(self.project["id"])[0]["username"], "tushar@example.com")
        self.assertEqual(ds.cred_password(c["id"])["password"], "s3cret-pass")
        raw = dict(db.get_conn().execute("SELECT * FROM den_credentials").fetchone())
        self.assertNotIn("s3cret", str(raw))
        self.assertNotIn("tushar@example.com", str(raw))
        ds.edit_cred(c["id"], ds.CredPatch(password="n3w"))
        self.assertEqual(ds.cred_password(c["id"])["password"], "n3w")
        self.assertEqual(self.code(ds.add_cred, self.project["id"], ds.CredIn(label="x", url="javascript:alert(1)")), 400)

    def test_vault_files_and_notes_are_encrypted_on_disk(self):
        f = ds.add_file("Banking", "statement.pdf", "application/pdf", b"%PDF account 12345678")
        blob = (ds.vault_dir() / f"{f['id']}.bin").read_bytes()
        self.assertNotIn(b"12345678", blob)
        self.assertEqual(ds.read_file(f["id"])[1], b"%PDF account 12345678")
        n = ds.save_note(ds.NoteIn(folder="Private notes", name="Locker", text="code 9911"))
        self.assertNotIn("9911", str(dict(db.get_conn().execute("SELECT * FROM den_vault_items WHERE id = ?", (n["id"],)).fetchone())))
        self.assertEqual(ds.vault_list()["folders"][0]["items"][0]["text"], "code 9911")
        ds.delete_item(f["id"])
        self.assertFalse((ds.vault_dir() / f"{f['id']}.bin").exists())
