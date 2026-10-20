"""The Den 2.0 Smart devices: your own spoken commands, and "Pause Octopus" holding the queue."""

from fastapi import HTTPException

from tests.helpers import TestBase  # noqa: I001 - first, so the shell's real API keys are cleared before api/worker load

import db
import den_connections as dc
import worker


class SpokenCommands(TestBase):
    def add(self, phrase, action, **kw):
        cmds = dc.commands() + [{"id": len(dc.commands()) + 1, "phrase": phrase, "action": action, "on": True, "confirm": False, **kw}]
        db.set_setting("voice_commands", cmds)

    def test_pause_and_resume_hold_the_queue(self):
        self.add("Pause Octopus", "pause")
        self.add("Resume the queue", "resume")
        db.add_task(self.project["id"], "work", 0, None, lane="agent")
        out = dc.run_command("octo, pause octopus!")          # wake word and punctuation don't matter
        self.assertTrue(out["ran"])
        self.assertTrue(worker.queue_paused())
        self.assertTrue(dc.run_command("Resume the queue")["ran"])
        self.assertFalse(worker.queue_paused())

    def test_anything_else_goes_to_octopus(self):
        self.add("Pause Octopus", "pause")
        self.assertIsNone(dc.run_command("pause octopus and then write the docs"))
        self.assertFalse(worker.queue_paused())

    def test_confirm_first_then_yes(self):
        self.add("Going to sleep", "sleep", confirm=True)
        first = dc.run_command("going to sleep")
        self.assertEqual((first["ran"], first["confirm"]), (False, True))
        self.assertFalse(db.sleep_mode_active())
        said, yes = dc.confirm_prefix("yes, going to sleep")
        self.assertTrue(dc.run_command(said, yes)["ran"])
        self.assertTrue(db.sleep_mode_active())

    def test_switched_off_command_is_ignored(self):
        self.add("Pause Octopus", "pause", on=False)
        self.assertIsNone(dc.run_command("pause octopus"))

    def test_only_safe_actions(self):
        self.assertEqual(dc._check_action("pause"), "pause")
        with self.assertRaises(HTTPException):
            dc._check_action("approve")
