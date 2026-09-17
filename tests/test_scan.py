#!/usr/bin/env python3
"""Tests for the scanner, on a synthetic config dir. No dependencies.

    python3 -m unittest discover -s tests

Every path and name here is invented. The point of the fixture is the shapes
Claude Code actually writes: a statusMessage in place of the command, a hook
declared by a plugin rather than by settings.json, and a cancellation that is a
user Esc rather than a timeout.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

SCAN = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    os.pardir, "skills", "hook-audit", "scan.py")

GATE = '/usr/bin/python3 "$HOME/hooks/gate.py"'
WIDGET = "/opt/example/bin/widget-notify"
PLUGIN_CMD = 'node "${CLAUDE_PLUGIN_ROOT}/hooks/report.mjs"'

SETTINGS = {
    "hooks": {
        # A hook with a statusMessage: the transcript records the message, not this.
        "PreToolUse": [{"matcher": "Write", "hooks": [
            {"type": "command", "command": GATE, "timeout": 10,
             "statusMessage": "checking the write"}]}],
        # The same command, async on one event and blocking on another.
        "Stop": [{"matcher": "*", "hooks": [
            {"type": "command", "command": WIDGET, "async": True, "timeout": 5}]}],
        "UserPromptSubmit": [{"matcher": "*", "hooks": [
            {"type": "command", "command": WIDGET}]}],
        # Configured, never observed in the window.
        "SessionEnd": [{"matcher": "*", "hooks": [
            {"type": "command", "command": "/opt/example/bin/quiet-hook"}]}],
    }
}

PLUGIN_HOOKS = {
    "hooks": {"SessionStart": [{"matcher": "startup", "hooks": [
        {"type": "command", "command": PLUGIN_CMD, "async": True, "timeout": 3}]}]}
}


def entry(event, command, duration, kind="hook_success", **extra):
    a = {"type": kind, "hookEvent": event, "command": command, "durationMs": duration}
    a.update(extra)
    return json.dumps({"type": "attachment", "attachment": a}) + "\n"


TRANSCRIPT = "".join([
    entry("PreToolUse", "checking the write", 120),
    entry("PreToolUse", "checking the write", 180),
    entry("Stop", WIDGET, 400),
    entry("UserPromptSubmit", WIDGET, 300),
    entry("UserPromptSubmit", WIDGET, 300),
    entry("UserPromptSubmit", WIDGET, 300),
    entry("SessionStart", PLUGIN_CMD, 900),
    # Ran until its limit fired.
    entry("PreToolUse", "checking the write", 10000,
          kind="hook_cancelled", timedOut=True, timeoutMs=10000),
    # The user pressed Esc. Not a timeout, and says nothing about speed.
    entry("Stop", WIDGET, 50, kind="hook_cancelled"),
    entry("SessionStart", PLUGIN_CMD, 900, kind="hook_non_blocking_error"),
    '{"type":"user","message":"a line with no hook in it"}\n',
    "not json at all\n",
])


def build(root):
    os.makedirs(os.path.join(root, "projects", "example-project"))
    with open(os.path.join(root, "projects", "example-project", "s.jsonl"), "w",
              encoding="utf-8") as f:
        f.write(TRANSCRIPT)
    with open(os.path.join(root, "settings.json"), "w", encoding="utf-8") as f:
        json.dump(SETTINGS, f)
    install = os.path.join(root, "plugins", "cache", "example-marketplace",
                           "reporter", "1.0.0")
    os.makedirs(os.path.join(install, "hooks"))
    with open(os.path.join(install, "hooks", "hooks.json"), "w", encoding="utf-8") as f:
        json.dump(PLUGIN_HOOKS, f)
    os.makedirs(os.path.join(root, "plugins"), exist_ok=True)
    with open(os.path.join(root, "plugins", "installed_plugins.json"), "w",
              encoding="utf-8") as f:
        json.dump({"version": 2, "plugins": {"reporter@example-marketplace": [
            {"scope": "user", "installPath": install, "version": "1.0.0"}]}}, f)


def run(root, *args):
    env = dict(os.environ, CLAUDE_CONFIG_DIR=root)
    out = subprocess.check_output(
        [sys.executable, SCAN, "--root", os.path.join(root, "projects"), *args],
        env=env, stderr=subprocess.STDOUT)
    return out.decode("utf-8")


class ScanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        build(cls.tmp.name)
        cls.data = json.loads(run(cls.tmp.name, "--json"))
        cls.report = run(cls.tmp.name)
        cls.hidden = json.loads(run(cls.tmp.name, "--json", "--redact"))
        cls.rows = {(r["event"], r["command"]): r for r in cls.data["hooks"]}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_status_message_resolves_to_its_hook(self):
        """The transcript holds the statusMessage; the timeout is on the command."""
        r = self.rows[("PreToolUse", "checking the write")]
        self.assertTrue(r["in_settings"])
        self.assertEqual(r["timeout_setting"], 10)
        self.assertFalse(r["is_async"])

    def test_a_hook_that_ran_is_not_reported_as_unobserved(self):
        self.assertNotIn(GATE, self.data["configured_but_unobserved"])
        self.assertIn("/opt/example/bin/quiet-hook",
                      self.data["configured_but_unobserved"])

    def test_plugin_declared_hook_is_matched(self):
        r = self.rows[("SessionStart", PLUGIN_CMD)]
        self.assertTrue(r["in_settings"], "plugin hooks.json was not read")
        self.assertTrue(r["is_async"])
        self.assertEqual(r["timeout_setting"], 3)
        self.assertEqual(r["total_blocking_ms"], 0)
        self.assertEqual(r["failures"], 1)

    def test_mode_is_per_event_not_per_command(self):
        self.assertTrue(self.rows[("Stop", WIDGET)]["is_async"])
        self.assertFalse(self.rows[("UserPromptSubmit", WIDGET)]["is_async"])

    def test_blocking_total_is_runs_times_median(self):
        r = self.rows[("UserPromptSubmit", WIDGET)]
        self.assertEqual(r["runs"], 3)
        self.assertEqual(r["median_ms"], 300)
        self.assertEqual(r["total_blocking_ms"], 900)

    def test_only_timed_out_entries_count_as_timeouts(self):
        self.assertEqual(self.rows[("PreToolUse", "checking the write")]["timeouts"], 1)
        self.assertEqual(self.rows[("Stop", WIDGET)]["timeouts"], 0)

    def test_a_timed_out_run_is_in_the_durations(self):
        """durationMs is a floor there, and dropping it would understate the cost."""
        self.assertEqual(self.rows[("PreToolUse", "checking the write")]["runs"], 3)

    def test_unparsable_lines_are_skipped(self):
        self.assertEqual(len(self.data["hooks"]), 4)

    def test_report_names_its_total_a_floor(self):
        self.assertIn("at least", self.report)

    def test_redact_removes_paths_and_keeps_the_counts(self):
        for r in self.hidden["hooks"]:
            self.assertNotIn("/", r["command"])
        for c in self.hidden["configured_but_unobserved"]:
            self.assertNotIn("/", c)
        self.assertEqual(len(self.hidden["hooks"]), len(self.data["hooks"]))
        self.assertEqual(len(self.hidden["configured_but_unobserved"]),
                         len(self.data["configured_but_unobserved"]))

    def test_redact_keeps_the_argument_that_tells_two_hooks_apart(self):
        sys.path.insert(0, os.path.dirname(SCAN))
        import scan
        self.assertEqual(scan.redact('/usr/bin/python3 "$HOME/h/notify.py" busy'),
                         "notify.py busy")
        self.assertEqual(scan.redact('bash "/opt/example/run.sh"'), "run.sh")
        self.assertEqual(scan.redact("a label with no path"), "a label with no path")
        self.assertEqual(scan.redact('IN=$(cat); printf %s "$IN" | /opt/x/y'),
                         "<inline shell>")
        # -c carries the script itself, so it never reaches a shared report
        self.assertEqual(scan.redact('python3 -c "import os"'), "<inline script>")
        self.assertEqual(scan.redact('python3 -c "print(1); print(TOKEN)"'),
                         "<inline shell>")


if __name__ == "__main__":
    unittest.main(verbosity=2)
