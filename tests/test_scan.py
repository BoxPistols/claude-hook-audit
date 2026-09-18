#!/usr/bin/env python3
"""Tests for the scanner, on a synthetic config dir. No dependencies.

    python3 -m unittest discover -s tests

Every path and name here is invented. What is not invented is the shape of the
records: each one below is a shape Claude Code actually writes, including the ones
that cost this scanner a bug — a statusMessage in place of the command, a blocked
tool call that carries no command field and no duration, a plugin command recorded
with ${CLAUDE_PLUGIN_ROOT} already expanded, and runs made inside a subagent.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCAN = os.path.join(HERE, os.pardir, "skills", "hook-audit", "scan.py")
sys.path.insert(0, os.path.join(HERE, os.pardir, "skills", "hook-audit"))
import scan  # noqa: E402

GATE = '/usr/bin/python3 "$HOME/hooks/gate.py"'
WIDGET = "/opt/example/bin/widget-notify"
QUIET = "/opt/example/bin/quiet-hook"
REPORT = 'node "${CLAUDE_PLUGIN_ROOT}/hooks/report.mjs"'
KEEPER = 'node "${CLAUDE_PLUGIN_ROOT}/hooks/gatekeeper.mjs"'
DORMANT = 'node "${CLAUDE_PLUGIN_ROOT}/hooks/farewell.mjs"'
# Punctuation and a digit, so --redact would mask parts of it if it read it as a command.
LABEL = "checking the write, step 2"

SETTINGS = {
    "hooks": {
        # A hook with a statusMessage: the transcript records the message, not this.
        "PreToolUse": [{"matcher": "Write", "hooks": [
            {"type": "command", "command": GATE, "timeout": 10,
             "statusMessage": LABEL}]}],
        # The same command, async on one event and blocking on another.
        "Stop": [{"matcher": "*", "hooks": [
            {"type": "command", "command": WIDGET, "async": True, "timeout": 5}]}],
        "UserPromptSubmit": [{"matcher": "*", "hooks": [
            {"type": "command", "command": WIDGET}]}],
        # Configured, never observed in the window.
        "SessionEnd": [{"matcher": "*", "hooks": [
            {"type": "command", "command": QUIET}]}],
    }
}

PLUGIN_HOOKS = {"hooks": {
    "SessionStart": [{"matcher": "startup", "hooks": [
        {"type": "command", "command": REPORT, "async": True, "timeout": 3}]}],
    "PostToolUse": [{"matcher": "Edit", "hooks": [
        {"type": "command", "command": KEEPER, "timeout": 7}]}],
    # Configured by the plugin, never observed: the report should name the plugin.
    "SessionEnd": [{"matcher": "*", "hooks": [
        {"type": "command", "command": DORMANT}]}],
}}

PLUGIN_KEY = "reporter@example-marketplace"


def rec(event, duration, command=None, kind="hook_success", sidechain=False, **extra):
    a = {"type": kind, "hookEvent": event, "hookName": event + ":Edit"}
    if command is not None:
        a["command"] = command
    if duration is not None:
        a["durationMs"] = duration
    a.update(extra)
    return json.dumps({"type": "attachment", "attachment": a,
                       "isSidechain": sidechain}) + "\n"


def subagent_transcript():
    return "".join(rec("SessionStart", 1000, REPORT, sidechain=True) for _ in range(5))


def transcript(install):
    keeper_expanded = KEEPER.replace("${CLAUDE_PLUGIN_ROOT}", install)
    lines = []
    # 21 runs whose sum, median, p95 and max are all different numbers.
    for _ in range(18):
        lines.append(rec("UserPromptSubmit", 100, WIDGET))
    lines.append(rec("UserPromptSubmit", 5000, WIDGET))
    lines.append(rec("UserPromptSubmit", 20000, WIDGET))
    # Ran until a limit that is nowhere in the settings.
    lines.append(rec("UserPromptSubmit", 60000, WIDGET, kind="hook_cancelled",
                     timedOut=True, timeoutMs=60000))
    # The user pressed Esc. timedOut is present and false.
    lines.append(rec("Stop", 50, WIDGET, kind="hook_cancelled", timedOut=False))
    lines.append(rec("Stop", 400, WIDGET))
    # Recorded under the statusMessage, not the command.
    lines.append(rec("PreToolUse", 120, LABEL))
    lines.append(rec("PreToolUse", 180, LABEL))
    # One main-session run; the five inside subagents live in their own transcript.
    lines.append(rec("SessionStart", 900, REPORT))
    # Blocked tool calls: no command field, no duration, command in the message.
    for _ in range(3):
        lines.append(rec("PostToolUse", None, kind="hook_blocking_error",
                         blockingError={"blockingError":
                                        "[%s]: refused\nline two" % keeper_expanded}))
    # Carries no hook identity at all.
    lines.append(rec("PreToolUse", None, kind="hook_additional_context",
                     content=["some context"]))
    # A type this scanner has never seen.
    lines.append(rec("Stop", 70, WIDGET, kind="hook_something_new"))
    lines.append('{"type":"user","message":"a line with no hook in it"}\n')
    lines.append("not json at all\n")
    return "".join(lines)


def build(home):
    cfg = os.path.join(home, "cfg")
    os.makedirs(os.path.join(cfg, "projects", "example-project"))
    install = os.path.join(cfg, "plugins", "cache", "example-marketplace",
                           "reporter", "1.0.0")
    os.makedirs(os.path.join(install, "hooks"))
    with open(os.path.join(cfg, "projects", "example-project", "s.jsonl"), "w",
              encoding="utf-8") as f:
        f.write(transcript(install))
    subdir = os.path.join(cfg, "projects", "example-project", "s", "subagents")
    os.makedirs(subdir)
    with open(os.path.join(subdir, "agent-1.jsonl"), "w", encoding="utf-8") as f:
        f.write(subagent_transcript())
    with open(os.path.join(cfg, "settings.json"), "w", encoding="utf-8") as f:
        json.dump(SETTINGS, f)
    with open(os.path.join(install, "hooks", "hooks.json"), "w", encoding="utf-8") as f:
        json.dump(PLUGIN_HOOKS, f)
    with open(os.path.join(cfg, "plugins", "installed_plugins.json"), "w",
              encoding="utf-8") as f:
        json.dump({"version": 2, "plugins": {PLUGIN_KEY: [
            {"scope": "user", "installPath": install, "version": "1.0.0"}]}}, f)
    return cfg


def run(home, *args, **kw):
    """Runs with HOME and a tilde in CLAUDE_CONFIG_DIR, which is the shape a
    config file or a quoted argument hands over."""
    env = dict(os.environ, HOME=home, CLAUDE_CONFIG_DIR="~/cfg")
    env.pop("CLAUDE_CODE_SSE_PORT", None)
    p = subprocess.run([sys.executable, SCAN, *args], env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if kw.get("check", True) and p.returncode != 0:
        raise AssertionError("exit %d: %s" % (p.returncode, p.stderr.decode()))
    return p


class ScanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cfg = build(cls.tmp.name)
        cls.data = json.loads(run(cls.tmp.name, "--json").stdout.decode())
        cls.report = run(cls.tmp.name).stdout.decode()
        cls.hidden = json.loads(run(cls.tmp.name, "--json", "--redact").stdout.decode())
        cls.rows = {(r["event"], r["command"]): r for r in cls.data["hooks"]}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # --- identity: matching a run back to the entry that configured it ---

    def test_tilde_in_config_dir_is_expanded(self):
        """Nothing else in this class runs if the default root did not resolve."""
        self.assertEqual(self.data["window"]["transcripts"], 2)

    def test_the_window_separates_sessions_from_subagent_transcripts(self):
        self.assertEqual(self.data["window"]["sessions"], 1)
        self.assertEqual(self.data["window"]["subagent_transcripts"], 1)
        self.assertIn("1 session + 1 subagent transcript,", self.report)

    def test_status_message_resolves_to_its_hook(self):
        r = self.rows[("PreToolUse", LABEL)]
        self.assertTrue(r["in_settings"])
        self.assertEqual(r["timeout_s"], 10)
        self.assertFalse(r["is_async"])

    def test_a_hook_that_ran_is_not_reported_as_unobserved(self):
        self.assertNotIn(GATE, self.data["configured_but_unobserved"])
        self.assertIn(QUIET, self.data["configured_but_unobserved"])

    def test_an_unobserved_plugin_hook_names_the_plugin_that_configured_it(self):
        self.assertIn(DORMANT, self.data["configured_but_unobserved"])
        line = [ln for ln in self.report.splitlines() if "farewell.mjs" in ln][0]
        self.assertIn(PLUGIN_KEY, line)

    def test_plugin_declared_hook_is_matched(self):
        r = self.rows[("SessionStart", REPORT)]
        self.assertTrue(r["in_settings"], "plugin hooks.json was not read")
        self.assertTrue(r["is_async"])
        self.assertEqual(r["timeout_s"], 3)

    def test_expanded_plugin_root_is_matched(self):
        """A blocked call records the command with the variable resolved."""
        row = [r for r in self.data["hooks"] if r["blocked_calls"]]
        self.assertEqual(len(row), 1)
        self.assertTrue(row[0]["in_settings"], "expanded ${CLAUDE_PLUGIN_ROOT} missed")
        self.assertEqual(row[0]["timeout_s"], 7)

    def test_mode_is_per_event_not_per_command(self):
        self.assertTrue(self.rows[("Stop", WIDGET)]["is_async"])
        self.assertFalse(self.rows[("UserPromptSubmit", WIDGET)]["is_async"])

    # --- what the numbers mean ---

    def test_total_is_the_sum_of_the_runs(self):
        r = self.rows[("UserPromptSubmit", WIDGET)]
        total = 18 * 100 + 5000 + 20000 + 60000
        self.assertEqual(r["runs"], 21)
        self.assertEqual(r["median_ms"], 100)
        self.assertEqual(r["total_ms"], total)
        self.assertEqual(r["main_total_ms"], total)
        self.assertEqual(r["total_blocking_ms"], total)
        # runs x median would report 2,100ms for the same 86,800ms of waiting
        self.assertNotEqual(r["total_ms"], r["runs"] * r["median_ms"])

    def test_p95_is_nearest_rank_and_absent_below_twenty_runs(self):
        self.assertEqual(self.rows[("UserPromptSubmit", WIDGET)]["p95_ms"], 20000)
        self.assertEqual(self.rows[("UserPromptSubmit", WIDGET)]["max_ms"], 60000)
        self.assertIsNone(self.rows[("PreToolUse", LABEL)]["p95_ms"])
        self.assertEqual(scan.percentile(list(range(1, 21)), 0.95), 19)
        self.assertIsNone(scan.percentile([], 0.95))

    def test_subagent_runs_are_kept_out_of_main_session_time(self):
        r = self.rows[("SessionStart", REPORT)]
        self.assertEqual(r["runs"], 6)
        self.assertEqual(r["subagent_runs"], 5)
        self.assertEqual(r["main_total_ms"], 900)
        self.assertEqual(r["subagent_total_ms"], 5000)
        self.assertIn("inside subagents", self.report)

    def test_blocking_headline_counts_main_session_time_only(self):
        blocking = [r for r in self.data["hooks"]
                    if not r["is_async"] and r["total_blocking_ms"]]
        self.assertEqual(sum(r["total_blocking_ms"] for r in blocking),
                         86800 + 300)          # the widget row plus the gate's two runs
        self.assertIn("at least 1.5 minutes", self.report)

    def test_only_timed_out_entries_count_as_timeouts(self):
        self.assertEqual(self.rows[("UserPromptSubmit", WIDGET)]["timeouts"], 1)
        self.assertEqual(self.rows[("Stop", WIDGET)]["timeouts"], 0)

    def test_an_observed_limit_is_not_an_unknown_timeout(self):
        """The hook has no configured timeout, but its real limit was recorded."""
        r = self.rows[("UserPromptSubmit", WIDGET)]
        self.assertIsNone(r["timeout_s"])
        self.assertEqual(r["timeout_observed_ms"], 60000)
        section = self.report.split("BLOCKING WITH NO KNOWN TIMEOUT")
        self.assertEqual(len(section), 1, "listed as unknown despite an observed limit")

    # --- records this reads but does not own ---

    def test_a_blocked_call_is_reported_even_with_no_duration(self):
        row = [r for r in self.data["hooks"] if r["blocked_calls"]][0]
        self.assertEqual(row["blocked_calls"], 3)
        self.assertEqual(row["runs"], 0)
        self.assertIn("STOPPED A TOOL CALL", self.report)
        self.assertNotIn(KEEPER.replace("${CLAUDE_PLUGIN_ROOT}", ""),
                         self.report.split("STOPPED A TOOL CALL")[0])

    def test_an_unknown_outcome_type_is_surfaced_not_dropped(self):
        r = self.rows[("Stop", WIDGET)]
        self.assertEqual(r["other_outcomes"], {"hook_something_new": 1})
        self.assertIn("UNRECOGNIZED OUTCOMES", self.report)

    def test_records_with_no_identity_are_counted_and_named(self):
        self.assertEqual(self.data["unattributed_records"],
                         {"hook_additional_context": 1})
        self.assertIn("could not be attributed", self.report)

    def test_unparsable_lines_are_skipped(self):
        self.assertEqual(len(self.data["hooks"]), 5)

    # --- what leaves the machine ---

    def test_redact_removes_paths_and_keeps_the_counts(self):
        for r in self.hidden["hooks"]:
            self.assertNotIn("/", r["command"])
        for c in self.hidden["configured_but_unobserved"]:
            self.assertNotIn("/", c)
        self.assertEqual(len(self.hidden["hooks"]), len(self.data["hooks"]))
        self.assertEqual(len(self.hidden["configured_but_unobserved"]),
                         len(self.data["configured_but_unobserved"]))

    def test_redact_keeps_the_argument_that_tells_two_hooks_apart(self):
        self.assertEqual(scan.redact('/usr/bin/python3 "$HOME/h/notify.py" busy'),
                         "notify.py busy")
        self.assertEqual(scan.redact('bash "/opt/example/run.sh"'), "run.sh")
        self.assertEqual(scan.redact("uv run /opt/example/check.py --quiet"),
                         "run check.py --quiet")
        self.assertEqual(scan.redact('IN=$(cat); printf %s "$IN" | /opt/x/y'),
                         "<inline shell>")
        # -c carries the script itself, so it never reaches a shared report
        self.assertEqual(scan.redact('python3 -c "import os"'), "<inline script>")
        self.assertEqual(scan.redact('python3 -c "print(1); print(TOKEN)"'),
                         "<inline shell>")

    def test_redact_masks_a_credential_passed_to_a_command(self):
        """Every value here is invented. None of them may reach a shared report."""
        cases = {
            "EXAMPLE_TOKEN=tok-0000-FAKE /usr/bin/python3 /opt/example/send.py":
                "send.py",
            "/opt/example/notify --token tok-FAKE-0000": "notify --token …",
            "notify-send --api-key=FAKE0000 done": "notify-send --api-key=… done",
            "/opt/example/login --password hunter": "login --password …",
            'curl -H "Authorization: Bearer FAKE0000" https://example.com/':
                "curl -H … …",
            # base64 carries slashes, and its last piece can be letters only.
            "/opt/example/push.py FAKE0000/K7FAKE/bFakeSecretTail": "push.py …",
            "/opt/example/push.py -s fakeAB/fakeTail": "push.py -s …",
        }
        for command, shown in cases.items():
            self.assertEqual(scan.redact(command), shown, command)

    def test_redact_prints_a_status_message_as_written(self):
        """Known to be a label only from the configuration, so checked end to end."""
        shown = {(r["event"], r["command"]) for r in self.hidden["hooks"]}
        self.assertIn(("PreToolUse", LABEL), shown)

    def test_redact_cuts_a_path_in_a_status_message_to_its_file_name(self):
        """A plugin's label is not the user's text, and a path in it names the account."""
        cases = {
            "Running /Users/example/hooks/check.py": "Running check.py",
            "Linting (/Users/example/proj/src)": "Linting (src)",
            "Checking ~/hooks/check.py, step 1/2 and/or ./local/x.py":
                "Checking check.py, step 1/2 and/or ./local/x.py",
        }
        for label, shown in cases.items():
            self.assertEqual(scan.redact(label, label=True), shown, label)

    def test_the_readme_sample_is_the_format_the_program_prints(self):
        """A sample output that has drifted from the program is worse than none.

        Both lines are taken from this run's own report, so this fails when the
        report changes shape and the README is not updated with it."""
        with open(os.path.join(HERE, os.pardir, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        lines = self.report.splitlines()
        title = [ln for ln in lines if ln.startswith("BLOCKING  (")][0]
        header = [ln for ln in lines if ln.strip().startswith("main    runs")][0]
        self.assertIn(title, readme)
        self.assertIn(header, readme)

    # --- refusing to produce a confident report from an input it could not read ---

    def test_a_settings_path_that_does_not_exist_is_an_error(self):
        p = run(self.tmp.name, "--settings",
                os.path.join(self.tmp.name, "typo.json"), check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn(b"not found", p.stderr)

    def test_a_project_that_is_not_a_directory_is_an_error(self):
        p = run(self.tmp.name, "--project",
                os.path.join(self.tmp.name, "nope"), check=False)
        self.assertNotEqual(p.returncode, 0)

    def test_a_negative_file_count_is_an_error(self):
        p = run(self.tmp.name, "--files", "-5", check=False)
        self.assertNotEqual(p.returncode, 0)

    def test_settings_that_do_not_parse_are_reported(self):
        home = tempfile.mkdtemp()
        cfg = build(home)
        with open(os.path.join(cfg, "settings.json"), "w", encoding="utf-8") as f:
            f.write('{"hooks": {},}')          # the classic trailing comma
        data = json.loads(run(home, "--json").stdout.decode())
        self.assertTrue(data["problems"], "a settings file that failed to parse was silent")
        self.assertIn("could not read", run(home).stdout.decode())


if __name__ == "__main__":
    unittest.main(verbosity=2)
