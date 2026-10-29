"""reviews.ops pure functions: launchctl parsing, log redaction, the switch allowlist."""
from unittest import mock

from django.test import SimpleTestCase

from . import ops

PRINT_RUNNING = """gui/501/com.example.mr-sentinel-bot-socket = {
\tactive count = 1
\tpath = /Users/k/Library/LaunchAgents/com.example.mr-sentinel-bot-socket.plist
\tstate = running
\truns = 8
\tpid = 7907
\tlast terminating signal = Terminated: 15
\tendpoints = {
\t\tstate = active
\t}
}
"""
PRINT_FAILED = """gui/501/com.example.mr-sentinel = {
\tstate = not running
\truns = 5471
\tlast exit code = 1
}
"""
DISABLED = """disabled services = {
\t"com.example.mr-sentinel-bot-socket" => disabled
\t"tw.pocket.alert-doctor.collector" => enabled
}
"""


class TestParsing(SimpleTestCase):
    def test_top_level_fields_only(self):
        info = ops.parse_print(PRINT_RUNNING)
        self.assertEqual((info["state"], info["pid"], info["runs"]), ("running", "7907", "8"))

    def test_failed_scheduled_job(self):
        info = ops.parse_print(PRINT_FAILED)
        self.assertEqual((info["state"], info["last exit code"]), ("not running", "1"))

    def test_disabled_list(self):
        self.assertEqual(ops.parse_disabled(DISABLED),
                         {"com.example.mr-sentinel-bot-socket": True,
                          "tw.pocket.alert-doctor.collector": False})


class TestRedact(SimpleTestCase):
    def test_tokens_never_reach_the_page(self):
        line = ("-Dsonar.token=squ_deb0af25 SONAR_TOKEN=squ_x glpat-AbC xoxb-1-2 "
                "xapp-1-A PRIVATE-TOKEN: abc password=hunter2")
        out = ops.redact(line)
        for secret in ("squ_deb0af25", "squ_x", "glpat-AbC", "xoxb-1-2", "xapp-1-A", "abc", "hunter2"):
            self.assertNotIn(secret, out)


class TestSwitch(SimpleTestCase):
    def test_only_the_socket_listeners_can_be_switched(self):
        with mock.patch.object(ops, "launchctl") as lc:
            with self.assertRaises(ValueError):
                ops.switch("com.example.mr-sentinel", on=False)
        lc.assert_not_called()

    def test_off_disables_then_unloads_so_it_stays_off_after_a_reboot(self):
        with mock.patch.object(ops, "launchctl", return_value=(0, "")) as lc:
            ops.switch("com.example.mr-sentinel-bot-socket", on=False)
        self.assertEqual([c.args[0][0] for c in lc.call_args_list], ["disable", "bootout"])

    def test_on_enables_then_loads(self):
        with mock.patch.object(ops, "launchctl", return_value=(0, "")) as lc:
            ops.switch("tw.pocket.alert-doctor.collector", on=True)
        self.assertEqual([c.args[0][0] for c in lc.call_args_list], ["enable", "bootstrap"])
