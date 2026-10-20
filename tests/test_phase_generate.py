"""Generating a phase's tasks with a chosen model (phases.generate_tasks)."""
import unittest
from unittest import mock

import db
import phases
import providers
from tests import helpers


class GenerateTasksTest(helpers.TestBase):
    def test_adds_parsed_tasks_and_needs_a_goal(self):
        p = db.add_project("gen-proj", "")
        bare = phases.create(p["id"], "Bare")
        with self.assertRaises(phases.PhaseError):
            phases.generate_tasks(bare["id"])
        ph = phases.create(p["id"], "Build", goal="Ship it", description="Cart and checkout")
        self.assertEqual(ph["description"], "Cart and checkout")
        model = {"id": "m1", "provider": "openai", "label": "M1"}
        out = providers.Completion("- one\n2. two\n\n* three", 1, 1)
        with mock.patch("router.get_model", return_value=model), mock.patch("router.availability", return_value=(True, "", None)), \
                mock.patch("providers.complete", return_value=out):
            r = phases.generate_tasks(ph["id"], "m1")
        self.assertEqual(r["tasks"], ["one", "two", "three"])
        self.assertEqual(len(r["phase"]["todo_ids"]), 3)


if __name__ == "__main__":
    unittest.main()
