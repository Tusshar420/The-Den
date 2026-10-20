"""Den 2.1 client form: a client's health is the owner's call - healthy, watch or at risk."""

from tests.helpers import TestBase  # noqa: I001 - first, so the shell's real API keys are cleared before api/worker load

import clients


class ClientHealth(TestBase):
    def test_health_values(self):
        self.assertEqual(clients.clean_client({"health": "at_risk"}, 1), {"health": "at_risk"})
        self.assertEqual(clients.clean_client({"health": ""}, 1), {"health": None})
        with self.assertRaises(clients.ClientError):
            clients.clean_client({"health": "great"}, 1)
