#!/usr/bin/env python3
"""Profile Claude Code hooks from transcripts that already exist. No instrumentation.

Claude Code records every hook run it reports as an attachment entry carrying
durationMs, and records timeouts with timedOut/timeoutMs. This reads those
entries, ranks hooks by the time they held up the main session, and
cross-references the settings cascade and the installed plugins to say which of
them block the loop at all.

usage: hook-audit [--root DIR] [--settings FILE ...] [--project DIR] [--files N]
                  [--redact] [--json]
"""
import argparse
import collections
import datetime
import json
import math
import os
import re
import shlex
import statistics
import sys

# Outcomes Claude Code writes. Anything else starting with hook_ is counted and
# reported as unrecognized rather than dropped: this reads a format it does not own.
SUCCESS = "hook_success"
CANCELLED = "hook_cancelled"
BLOCKED = "hook_blocking_error"          # the hook exited 2 and stopped the tool call
CONTEXT = "hook_additional_context"      # injected context, carries no hook identity

# A hook on a per-tool-call event blocks the loop every time it fires.
PER_CALL_EVENTS = {"PreToolUse", "PostToolUse", "UserPromptSubmit",
                   "PermissionRequest", "PermissionDenied", "PostToolUseFailure"}

P95_MIN_RUNS = 20   # below this a 95th percentile is just the maximum
CMD_W = 96          # one truncation width for every place a command is printed
SHELL_CHARS = ("|", ";", "&&", "$(", "`", ">", "<")
INTERPRETERS = {"python", "python3", "node", "bash", "sh", "zsh", "env", "perl",
                "ruby", "deno", "bun", "npx", "uv", "uvx"}
BLOCKED_CMD = re.compile(r"^\[(.+?)\]:\s", re.S)
# What --redact keeps of an argument. A credential rarely has either shape: a token
# carries digits, and a header or a URL carries punctuation.
PLAIN_ARG = re.compile(r"-{0,2}[A-Za-z][A-Za-z-]{0,23}")
FILE_ARG = re.compile(r"[A-Za-z0-9_-]{1,40}\.[A-Za-z0-9]{1,5}")
SECRET_FLAG = re.compile(r"-.*(token|key|secret|pass|auth|cred)", re.I)
ENV_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
# An absolute or ~ path inside a statusMessage. Not after a word character, a dot or
# a slash, so "and/or", "1/2", "./x" and a URL are left as written.
LABEL_PATH = re.compile(r"(?<![\w.~/])~?(?:/[^\s/]+)+/?")
MASK = "…"


def config_dir():
    return os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")


def load_json(path, problems=None):
    """None on failure. Appends to problems, because a settings file that exists
    and does not parse inverts the whole report and has to be said out loud."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        if problems is not None:
            problems.append("%s: %s" % (path, exc))
        return None


def redact(s, label=False):
    """A hook identity with the machine taken out of it, for a report you share.

    Absolute paths carry the account name, and a command can carry anything its
    author put in settings.json, including a token passed as an argument or set in
    front of it. The script's file name is kept, and so is an argument shaped like a
    plain word or a file name, because those tell apart two hooks that run the same
    script. Every other argument is masked, and so is the value after a flag named
    like a credential. The masking goes by shape, not by recognizing a secret.

    A statusMessage (label) is text its author chose to display, so it is returned
    as written, except that a path in it is cut to its file name like a command's:
    a plugin's label is written by someone else and can name an absolute path.
    Only the caller knows which strings are labels.
    """
    if label:
        return LABEL_PATH.sub(lambda m: os.path.basename(m.group().rstrip("/")), s)
    if any(t in s for t in SHELL_CHARS):
        return "<inline shell>"
    try:
        words = shlex.split(s)
    except ValueError:
        words = s.split()
    while words and (os.path.basename(words[0]) in INTERPRETERS
                     or ENV_ASSIGN.match(words[0])):
        words.pop(0)                        # NAME=value in front is dropped whole
    if not words:
        return "<command>"
    if words[0].startswith("-"):
        return "<inline script>"            # -c and friends carry the whole script
    out = [os.path.basename(words[0].rstrip("/")) or words[0]]
    after_secret = False
    for w in words[1:]:
        base = os.path.basename(w.rstrip("/")) if "/" in w else w
        if after_secret or "://" in w:
            out.append(MASK)
        elif "=" in w:
            name = w.split("=", 1)[0]
            out.append(name + "=" + MASK if PLAIN_ARG.fullmatch(name) else MASK)
        # Of a value with a slash only a file name is kept: base64 carries slashes too.
        elif FILE_ARG.fullmatch(base) or ("/" not in w and PLAIN_ARG.fullmatch(w)):
            out.append(base)
        else:
            out.append(MASK)
        after_secret = "=" not in w and bool(SECRET_FLAG.match(w))
    return " ".join(out)


def plugin_hook_sources(cascade, problems):
    """(path, origin, enabled, root) for the hooks.json of every installed plugin.

    A plugin declares its hooks in its own file rather than in settings.json, so
    without these every plugin hook reads as blocking with no timeout.

    A plugin turned off since is still read, because the window holds the runs it
    made while it was on. It is marked not enabled so it stays out of the list of
    configured hooks that were never observed.
    """
    path = os.path.join(config_dir(), "plugins", "installed_plugins.json")
    reg = load_json(path, problems) if os.path.exists(path) else None
    if not isinstance(reg, dict):
        return []
    enabled = {}
    for p in cascade:
        d = load_json(p, problems)
        if isinstance(d, dict) and isinstance(d.get("enabledPlugins"), dict):
            enabled.update(d["enabledPlugins"])
    out = []
    for key, installs in (reg.get("plugins") or {}).items():
        if not isinstance(installs, list):
            continue
        on = enabled.get(key) is not False
        for inst in installs:
            root = (inst or {}).get("installPath")
            if not isinstance(root, str):
                continue
            f = os.path.join(root, "hooks", "hooks.json")
            src = (f, key, on, root)
            if os.path.exists(f) and src not in out:
                out.append(src)
    return out


def settings_index(sources, problems):
    """(path, origin, enabled, root) sources -> {(event, key): meta}

    key is the hook's command string and, when it has one, its statusMessage.
    Claude Code writes statusMessage into the transcript in place of the command,
    so a hook with one never matches on its command alone. A plugin command is
    also indexed with ${CLAUDE_PLUGIN_ROOT} expanded, because a blocked tool call
    is recorded with that variable already resolved.

    Keyed per event because async and timeout are properties of the entry, not of
    the command: the same script can be async on one event and blocking on another.
    """
    idx = {}
    for path, origin, enabled, root in sources:
        data = load_json(path, problems)
        if not isinstance(data, dict):
            continue
        for event, groups in (data.get("hooks") or {}).items():
            if not isinstance(groups, list):
                continue
            for g in groups:
                if not isinstance(g, dict):
                    continue
                for h in g.get("hooks") or []:
                    if not isinstance(h, dict):
                        continue
                    cmd = h.get("command")
                    if not isinstance(cmd, str):
                        continue
                    keys = [cmd]
                    label = h.get("statusMessage")
                    if not isinstance(label, str) or label == cmd:
                        label = None
                    if label:
                        keys.append(label)
                    if root and "CLAUDE_PLUGIN_ROOT" in cmd:
                        keys.append(cmd.replace("${CLAUDE_PLUGIN_ROOT}", root)
                                       .replace("$CLAUDE_PLUGIN_ROOT", root))
                    for k in keys:
                        e = idx.setdefault((event, k), {
                            "command": cmd, "async": False, "timeout_s": None,
                            "enabled": False, "matchers": set(), "origins": set(),
                            "label": False})
                        e["enabled"] = e["enabled"] or enabled
                        # Recorded under the statusMessage: --redact keeps it as written
                        # but for paths.
                        e["label"] = e["label"] or k == label
                        e["async"] = e["async"] or bool(h.get("async"))
                        if h.get("timeout") is not None:
                            e["timeout_s"] = h["timeout"]   # settings are in seconds
                        e["matchers"].add(g.get("matcher") or "*")
                        e["origins"].add(origin)
    return idx


SUBAGENT_DIR = os.sep + "subagents" + os.sep


def plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def is_subagent(path):
    """Subagents write their own transcript, under a subagents/ directory. On a busy
    week they outnumber the session transcripts, so the window says how many of each."""
    return SUBAGENT_DIR in path


def iter_files(root, limit, problems):
    found = []

    def onerror(exc):
        problems.append("%s: %s" % (getattr(exc, "filename", root), exc))

    for dirpath, _, names in os.walk(root, onerror=onerror):
        for n in names:
            if n.endswith(".jsonl"):
                p = os.path.join(dirpath, n)
                try:
                    found.append((os.path.getmtime(p), p))
                except OSError as exc:
                    problems.append("%s: %s" % (p, exc))
    found.sort(reverse=True)
    return found[:limit] if limit else found


def blocked_command(a):
    """hook_blocking_error carries no command field. The message it does carry is
    prefixed with the command in brackets, which is the only identity available."""
    be = a.get("blockingError")
    text = be.get("blockingError") if isinstance(be, dict) else be
    m = BLOCKED_CMD.match(text) if isinstance(text, str) else None
    return m.group(1) if m else None


def collect(files):
    runs = collections.defaultdict(list)          # (event, cmd) -> [(ms, sidechain)]
    outcomes = collections.defaultdict(collections.Counter)
    timeouts = collections.Counter()
    timeout_ms = {}
    unattributed = collections.Counter()          # type -> runs with no identity
    for _, path in files:
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"hook_' not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") != "attachment":
                    continue
                a = o.get("attachment") or {}
                kind = a.get("type")
                if not isinstance(kind, str) or not kind.startswith("hook_"):
                    continue
                cmd = a.get("command")
                if not isinstance(cmd, str) and kind == BLOCKED:
                    cmd = blocked_command(a)
                if not isinstance(cmd, str) or not cmd:
                    unattributed[kind] += 1
                    continue
                key = (a.get("hookEvent") or "?", cmd)
                outcomes[key][kind] += 1
                d = a.get("durationMs")
                if isinstance(d, (int, float)):
                    # A run inside a subagent did not hold up the main session: those
                    # run in parallel with each other and with the main loop.
                    runs[key].append((d, bool(o.get("isSidechain"))))
                # timedOut is present on every cancellation. False is the user
                # pressing Esc, which says nothing about how fast the hook is.
                if a.get("timedOut"):
                    timeouts[key] += 1
                    if a.get("timeoutMs"):
                        timeout_ms[key] = a["timeoutMs"]
    return runs, outcomes, timeouts, timeout_ms, unattributed


def percentile(arr, q):
    """Nearest-rank on a sorted list. Returns None when there is nothing to rank."""
    if not arr:
        return None
    return arr[max(0, min(len(arr) - 1, int(math.ceil(q * len(arr))) - 1))]


def build_rows(runs, outcomes, timeouts, cfg, hide):
    rows = []
    for key in set(runs) | set(outcomes):
        pairs = runs.get(key) or []
        main = sorted(d for d, side in pairs if not side)
        sub = sorted(d for d, side in pairs if side)
        every = sorted(d for d, _ in pairs)
        meta = cfg.get(key) or {}
        counts = outcomes.get(key) or {}
        rows.append({
            "event": key[0],
            "command": redact(key[1], meta.get("label", False)) if hide else key[1],
            "runs": len(every),
            "subagent_runs": len(sub),
            "median_ms": round(statistics.median(every)) if every else None,
            # A 95th percentile of fewer than P95_MIN_RUNS samples is the maximum.
            "p95_ms": (round(percentile(every, 0.95))
                       if len(every) >= P95_MIN_RUNS else None),
            "max_ms": round(every[-1]) if every else None,
            # Exact sums. runs x median understates a right-skewed latency by tens
            # of percent, and every sample is already in hand.
            "main_total_ms": round(sum(main)),
            "subagent_total_ms": round(sum(sub)),
            "total_ms": round(sum(every)),
            "total_blocking_ms": 0 if meta.get("async") else round(sum(main)),
            "timeouts": timeouts.get(key, 0),
            "blocked_calls": counts.get(BLOCKED, 0),
            "timeout_s": meta.get("timeout_s"),
            "is_async": bool(meta.get("async")),
            "in_settings": key in cfg,
            "origins": sorted(meta.get("origins") or []),
            "matchers": sorted(meta.get("matchers") or []),
            "other_outcomes": {k: v for k, v in counts.items()
                               if k not in (SUCCESS, CANCELLED, BLOCKED, CONTEXT)},
            "_key": key,
        })
    rows.sort(key=lambda r: (-r["total_blocking_ms"], -r["total_ms"], r["event"]))
    return rows


def unobserved(cfg, observed):
    """Configured hooks with no matching run in the window, grouped by command.

    Grouped on the real command rather than on its printed form, so the count is
    the same with and without --redact.
    """
    canonical = {}
    for (event, key), m in cfg.items():
        canonical.setdefault((event, m["command"]), m)
    seen = set()
    for event, key in observed:
        m = cfg.get((event, key))
        seen.add((event, m["command"] if m else key))
    out = collections.OrderedDict()
    for (event, cmd), m in sorted(canonical.items(), key=lambda kv: kv[0][1]):
        if (event, cmd) in seen or not m["enabled"]:
            continue
        rec = out.setdefault(cmd, {"events": set(), "async": False, "origins": set()})
        rec["events"].add(event)
        rec["async"] = rec["async"] or m["async"]
        rec["origins"] |= m["origins"]
    return out


def main():
    ap = argparse.ArgumentParser(prog="hook-audit")
    ap.add_argument("--root", default=None, help="transcript directory")
    ap.add_argument("--settings", action="append", default=[],
                    help="extra settings file to read (repeatable)")
    ap.add_argument("--project", default=None,
                    help="project dir whose .claude/settings*.json to include")
    ap.add_argument("--files", type=int, default=0,
                    help="read only the N most recently modified transcript files, "
                         "subagent transcripts included (default: all of them)")
    ap.add_argument("--redact", action="store_true",
                    help="print file names, plain-word arguments and labels instead "
                         "of full commands, for a report that leaves this machine")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    args = ap.parse_args()

    if args.files < 0:
        ap.error("--files must be 0 or more")
    problems = []
    root = os.path.expanduser(args.root or os.path.join(config_dir(), "projects"))

    # An implicit cascade member is skipped when absent. A path the user asked for
    # is not: dropping it silently produces a report in which nothing is configured.
    cascade = [p for p in (os.path.join(config_dir(), "settings.json"),
                           os.path.join(config_dir(), "settings.local.json"))
               if os.path.exists(p)]
    asked = [os.path.expanduser(p) for p in args.settings]
    if args.project:
        project = os.path.expanduser(args.project)
        if not os.path.isdir(project):
            sys.exit("--project is not a directory: %s" % project)
        cascade += [p for p in (os.path.join(project, ".claude", "settings.json"),
                                os.path.join(project, ".claude", "settings.local.json"))
                    if os.path.exists(p)]
    for p in asked:
        if not os.path.exists(p):
            sys.exit("--settings file not found: %s" % p)
    cascade += asked

    files = iter_files(root, args.files, problems)
    if not files:
        for p in problems:
            print("warning: %s" % p, file=sys.stderr)
        sys.exit("no transcripts found under %s" % root)

    sources = [(p, os.path.basename(p), True, None) for p in cascade]
    sources += plugin_hook_sources(cascade, problems)
    cfg = settings_index(sources, problems)

    runs, outcomes, timeouts, timeout_ms, unattributed = collect(files)
    rows = build_rows(runs, outcomes, timeouts, cfg, args.redact)
    silent = unobserved(cfg, set(runs) | set(outcomes))
    show = redact if args.redact else (lambda c: c)
    unknown = [r for r in rows if not r["in_settings"]]
    subagents = sum(1 for _, p in files if is_subagent(p))
    span_days = (files[0][0] - files[-1][0]) / 86400

    if args.json:
        json.dump({"window": {"transcripts": len(files), "days": round(span_days, 2),
                              "sessions": len(files) - subagents,
                              "subagent_transcripts": subagents,
                              "from": files[-1][0], "to": files[0][0]},
                   "redacted": args.redact,
                   "sources": [o for _, o, _, _ in sources],
                   "problems": problems,
                   "unattributed_records": dict(unattributed),
                   "hooks": [dict([(k, v) for k, v in r.items() if k != "_key"],
                                  timeout_observed_ms=timeout_ms.get(r["_key"]))
                             for r in rows],
                   "configured_but_unobserved": [show(c) for c in silent]},
                  sys.stdout, indent=2, ensure_ascii=False)
        print()
        return

    def when(ts):
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

    def ms(v):
        return "%6dms" % v if v is not None else "%8s" % "-"

    print("Window: %s + %s, %s - %s (%.1f days)"
          % (plural(len(files) - subagents, "session"),
             plural(subagents, "subagent transcript"),
             when(files[-1][0]), when(files[0][0]), span_days))
    print("Transcripts older than your cleanupPeriodDays are gone; nothing before the "
          "window can be judged from this.")
    for p in problems:
        print("warning: could not read %s" % p)
    print()

    blocking = [r for r in rows if not r["is_async"]]
    total_block = sum(r["total_blocking_ms"] for r in blocking)
    sub_total = sum(r["subagent_total_ms"] for r in rows)
    print("Blocking hooks held up the main session for at least %.1f minutes over this "
          "window" % (total_block / 60000))
    print("  (%d of %d observed event/hook pairs block). A floor, not a total: a hook"
          % (len(blocking), len(rows)))
    print("  that succeeds with empty output is never persisted, and %d configured hooks"
          % len(silent))
    print("  were not observed at all.")
    if sub_total:
        print("  A further %.1f minutes of hook time ran inside subagents. Those run in "
              "parallel" % (sub_total / 60000))
        print("  with each other and with the main loop, so it is not time you waited.")
    if unknown:
        one = len(unknown) == 1
        print("  %d observed hook%s (marked ?) %s in neither the settings cascade nor"
              % (len(unknown), "" if one else "s", "is" if one else "are"))
        print("  an installed plugin, so %s counted as blocking, which may be wrong."
              % ("it is" if one else "they are"))
    if unattributed:
        n = sum(unattributed.values())
        print("  %d record%s name%s no command and could not be attributed to a hook: %s."
              % (n, "" if n == 1 else "s", "s" if n == 1 else "",
                 ", ".join("%s x%d" % kv for kv in sorted(unattributed.items()))))
    print()

    def marks(r):
        return ("*" if not r["is_async"] and r["event"] in PER_CALL_EVENTS else " ") \
               + ("?" if not r["in_settings"] else " ")

    def table(title, subset, note=None):
        if not subset:
            return
        print(title)
        if note:
            print("  %s" % note)
        print("  %8s  %6s  %8s  %8s  %9s  event"
              % ("main", "runs", "median", "p95", "max"))
        for r in subset:
            print("  %7.1fm  %6d  %s  %s  %s  %s"
                  % (r["main_total_ms"] / 60000, r["runs"], ms(r["median_ms"]),
                     ms(r["p95_ms"]), ms(r["max_ms"]).rjust(9), r["event"]))
            print("  %2s        %s" % (marks(r), r["command"][:CMD_W]))
            if r["subagent_runs"]:
                print("            + %d of those runs were inside subagents (%.1fm, "
                      "in parallel)" % (r["subagent_runs"],
                                        r["subagent_total_ms"] / 60000))
        print()

    table("BLOCKING  (main = time the main session spent waiting, summed over its runs)",
          [r for r in blocking if r["runs"]],
          "* fires on every tool call or prompt, so it blocks the loop each time. "
          "p95 needs %d runs" % P95_MIN_RUNS)
    table("NON-BLOCKING  (async; listed for reference, this time is not spent waiting)",
          [r for r in rows if r["is_async"] and r["runs"]])

    # A hook whose real limit was observed is not a hook with an unknown ceiling.
    hang = [r for r in blocking
            if r["runs"] and r["timeout_s"] is None and r["_key"] not in timeout_ms]
    if hang:
        print("BLOCKING WITH NO KNOWN TIMEOUT")
        print("  These fall back to the event default, which can be minutes. One "
              "unresponsive dependency stalls the loop for that long.")
        for r in hang:
            print("  [%s] max seen %s  %s"
                  % (r["event"], ms(r["max_ms"]).strip(), r["command"][:CMD_W]))
        print()

    to = [r for r in rows if r["timeouts"]]
    if to:
        print("TIMED OUT (ran until the limit fired; durationMs is a floor, and can "
              "overshoot it)")
        for r in sorted(to, key=lambda r: -r["timeouts"]):
            print("  %dx  [%s] limit=%sms%s  %s"
                  % (r["timeouts"], r["event"], timeout_ms.get(r["_key"]),
                     "" if r["timeout_s"] is not None else " (not set; event default)",
                     r["command"][:CMD_W]))
        print()

    stopped = [r for r in rows if r["blocked_calls"]]
    if stopped:
        print("STOPPED A TOOL CALL (hook exited 2)")
        print("  Expected from a gate that is doing its job. From a reporting hook it "
              "is a bug, and it costs a retry every time.")
        for r in sorted(stopped, key=lambda r: -r["blocked_calls"]):
            print("  %dx  [%s] %s" % (r["blocked_calls"], r["event"],
                                      r["command"][:CMD_W]))
        print()

    odd = [r for r in rows if r["other_outcomes"]]
    if odd:
        print("UNRECOGNIZED OUTCOMES (this reads a format it does not own)")
        for r in odd:
            print("  [%s] %s  %s" % (r["event"], r["other_outcomes"],
                                     r["command"][:CMD_W]))
        print()

    if silent:
        print("CONFIGURED BUT NOT OBSERVED")
        print("  A hook that succeeds with empty output is not persisted, so this is the "
              "expected place for quiet hooks. Zero runs here does not mean it rarely fires.")
        for cmd, m in silent.items():
            # A hook you did not write, configured and never seen, is worth naming.
            plugins = [o for o in sorted(m["origins"]) if not o.endswith(".json")]
            print("  [%s]%s%s %s" % (",".join(sorted(m["events"])),
                                     " (async)" if m["async"] else "",
                                     " (%s)" % ",".join(plugins) if plugins else "",
                                     show(cmd)[:CMD_W]))
        print()

    if [r for r in blocking if r["event"] in PER_CALL_EVENTS]:
        print("WHAT TO DO")
        print("  Hooks marked * block the loop every time they fire. Split them by role:")
        print("    decides  - a check, a gate, a pre-write validation. It has to block. Leave it.")
        print("    reports  - a status line, a notification, a log shipper. It does not.")
        print("  Set \"async\": true on the reporting ones. They keep firing on every event;")
        print("  only the waiting goes away. Narrowing the matcher also works but loses coverage.")
        print("  Give every blocking hook an explicit \"timeout\" so a hang cannot stall the loop.")


if __name__ == "__main__":
    main()
