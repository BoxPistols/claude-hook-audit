#!/usr/bin/env python3
"""Profile Claude Code hooks from transcripts that already exist. No instrumentation.

Claude Code records every hook run it reports as an attachment entry carrying
durationMs, and records timeouts with timedOut/timeoutMs. This reads those
entries, ranks hooks by total blocking time, and cross-references the settings
cascade and the installed plugins to say which of them actually block the loop.

usage: hook-audit [--root DIR] [--settings FILE ...] [--project DIR] [--files N]
                  [--redact] [--json]
"""
import argparse
import collections
import datetime
import json
import os
import shlex
import statistics
import sys

HOOK_TYPES = ("hook_success", "hook_non_blocking_error",
              "hook_error_during_execution", "hook_cancelled")

# Rules of thumb. A hook on a per-tool-call event blocks the loop every time it fires.
PER_CALL_EVENTS = {"PreToolUse", "PostToolUse", "UserPromptSubmit",
                   "PermissionRequest", "PermissionDenied", "PostToolUseFailure"}

CMD_W = 96          # one truncation width for every place a command is printed
SHELL_CHARS = ("|", ";", "&&", "$(", "`", ">", "<")
INTERPRETERS = {"python", "python3", "node", "bash", "sh", "zsh", "env", "perl",
                "ruby", "deno", "bun", "npx", "uv", "uvx"}


def config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def redact(s):
    """A hook identity with the machine taken out of it, for a report you share.

    Absolute paths carry the account name, and an inline shell hook or a -c script
    can carry anything its author put in settings.json, including a token. Script
    arguments are kept, because they are what distinguishes two hooks that run the
    same script.
    """
    if any(t in s for t in SHELL_CHARS):
        return "<inline shell>"
    try:
        words = shlex.split(s)
    except ValueError:
        words = s.split()
    if not words:
        return "<command>"
    if "/" not in s and words[0] not in INTERPRETERS:
        return s                            # a statusMessage, nothing in it to hide
    words = [os.path.basename(w.rstrip("/")) if "/" in w else w for w in words]
    while words and words[0] in INTERPRETERS:
        words.pop(0)
    if not words:
        return "<command>"
    if words[0].startswith("-"):
        return "<inline script>"            # -c and friends carry the whole script
    return " ".join(words)


def plugin_hook_sources(cascade):
    """(path, origin, enabled) for the hooks.json of every installed plugin.

    A plugin declares its hooks in its own file rather than in settings.json, so
    without these every plugin hook reads as blocking with no timeout.

    A plugin turned off since is still read, because the window holds the runs it
    made while it was on. It is marked not enabled so it stays out of the list of
    configured hooks that were never observed.
    """
    reg = load_json(os.path.join(config_dir(), "plugins", "installed_plugins.json"))
    if not isinstance(reg, dict):
        return []
    enabled = {}
    for p in cascade:
        d = load_json(p)
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
            src = (f, key, on)
            if os.path.exists(f) and src not in out:
                out.append(src)
    return out


def settings_index(sources):
    """(path, origin, enabled) sources -> {(event, key): meta}

    key is the hook's command string and, when it has one, its statusMessage.
    Claude Code writes statusMessage into the transcript in place of the command,
    so a hook with one never matches on its command alone.

    Keyed per event because async and timeout are properties of the entry, not of
    the command: the same script can be async on one event and blocking on another.
    """
    idx = {}
    for path, origin, enabled in sources:
        data = load_json(path)
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
                    if isinstance(label, str) and label:
                        keys.append(label)
                    for k in keys:
                        e = idx.setdefault((event, k), {
                            "command": cmd, "async": False, "timeout": None,
                            "enabled": False, "matchers": set(), "origins": set()})
                        e["enabled"] = e["enabled"] or enabled
                        e["async"] = e["async"] or bool(h.get("async"))
                        if h.get("timeout") is not None:
                            e["timeout"] = h["timeout"]
                        e["matchers"].add(g.get("matcher") or "*")
                        e["origins"].add(origin)
    return idx


def iter_files(root, limit):
    found = []
    for dirpath, _, names in os.walk(root):
        for n in names:
            if n.endswith(".jsonl"):
                p = os.path.join(dirpath, n)
                try:
                    found.append((os.path.getmtime(p), p))
                except OSError:
                    pass
    found.sort(reverse=True)
    return found[:limit] if limit else found


def collect(files):
    runs = collections.defaultdict(list)          # (event, command) -> [durationMs]
    outcomes = collections.defaultdict(collections.Counter)
    timeouts = collections.Counter()
    timeout_ms = {}
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
                if kind not in HOOK_TYPES:
                    continue
                key = (a.get("hookEvent") or "?", a.get("command") or "?")
                outcomes[key][kind] += 1
                d = a.get("durationMs")
                if isinstance(d, (int, float)):
                    runs[key].append(d)
                # A user-Esc cancellation lacks timedOut and says nothing about speed.
                if kind == "hook_cancelled" and a.get("timedOut"):
                    timeouts[key] += 1
                    if a.get("timeoutMs"):
                        timeout_ms[key] = a["timeoutMs"]
    return runs, outcomes, timeouts, timeout_ms


def build_rows(runs, outcomes, timeouts, cfg, hide):
    rows = []
    for key, arr in runs.items():
        arr.sort()
        meta = cfg.get(key) or {}
        known = key in cfg
        med = statistics.median(arr)
        rows.append({
            "event": key[0],
            "command": redact(key[1]) if hide else key[1],
            "runs": len(arr),
            "median_ms": round(med),
            "p95_ms": round(arr[min(len(arr) - 1, int(len(arr) * 0.95))]),
            "max_ms": round(arr[-1]),
            "total_blocking_ms": round(len(arr) * med) if not meta.get("async") else 0,
            "total_ms": round(len(arr) * med),
            "timeouts": timeouts.get(key, 0),
            "timeout_setting": meta.get("timeout"),
            "is_async": bool(meta.get("async")),
            "in_settings": known,
            "origins": sorted(meta.get("origins") or []),
            "failures": outcomes[key].get("hook_error_during_execution", 0)
                        + outcomes[key].get("hook_non_blocking_error", 0),
            "_key": key,
        })
    rows.sort(key=lambda r: (-r["total_blocking_ms"], -r["total_ms"]))
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
    ap.add_argument("--root", default=os.path.join(config_dir(), "projects"),
                    help="transcript directory")
    ap.add_argument("--settings", action="append", default=[],
                    help="extra settings file to read (repeatable)")
    ap.add_argument("--project", default=None,
                    help="project dir whose .claude/settings*.json to include")
    ap.add_argument("--files", type=int, default=0,
                    help="read only the N most recently modified transcripts")
    ap.add_argument("--redact", action="store_true",
                    help="print basenames and labels instead of full commands, "
                         "for a report that leaves this machine")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    args = ap.parse_args()

    files = iter_files(args.root, args.files)
    if not files:
        sys.exit("no transcripts found under %s" % args.root)

    cascade = [os.path.join(config_dir(), "settings.json"),
               os.path.join(config_dir(), "settings.local.json")]
    if args.project:
        cascade += [os.path.join(args.project, ".claude", "settings.json"),
                    os.path.join(args.project, ".claude", "settings.local.json")]
    cascade += args.settings
    cascade = [p for p in cascade if os.path.exists(p)]
    sources = [(p, os.path.basename(p), True) for p in cascade]
    sources += plugin_hook_sources(cascade)
    cfg = settings_index(sources)

    runs, outcomes, timeouts, timeout_ms = collect(files)
    rows = build_rows(runs, outcomes, timeouts, cfg, args.redact)
    silent = unobserved(cfg, set(runs) | set(outcomes))
    show = redact if args.redact else (lambda c: c)
    unknown = [r for r in rows if not r["in_settings"]]
    span_days = (files[0][0] - files[-1][0]) / 86400

    if args.json:
        json.dump({"window": {"transcripts": len(files), "days": round(span_days, 2)},
                   "redacted": args.redact,
                   "sources": [o for _, o, _ in sources],
                   "hooks": [{k: v for k, v in r.items() if k != "_key"} for r in rows],
                   "configured_but_unobserved": [show(c) for c in silent]},
                  sys.stdout, indent=2, ensure_ascii=False)
        print()
        return

    def when(ts):
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

    print("Window: %d transcripts, %s - %s (%.1f days)"
          % (len(files), when(files[-1][0]), when(files[0][0]), span_days))
    print("Transcripts older than your cleanupPeriodDays are gone; nothing before the "
          "window can be judged from this.\n")

    blocking = [r for r in rows if not r["is_async"]]
    total_block = sum(r["total_blocking_ms"] for r in blocking)
    print("Blocking hooks cost at least %.1f minutes over this window (%d of %d observed "
          "hooks block)." % (total_block / 60000, len(blocking), len(rows)))
    print("  A floor, not a total: a hook that succeeds with empty output is never")
    print("  persisted, and %d configured hooks were not observed at all." % len(silent))
    if unknown:
        one = len(unknown) == 1
        print("  %d observed hook%s (marked ?) %s in neither the settings cascade nor"
              % (len(unknown), "" if one else "s", "is" if one else "are"))
        print("  an installed plugin, so %s counted as blocking, which may be wrong."
              % ("it is" if one else "they are"))
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
        print("  %8s  %6s  %8s  %8s  %9s  event" % ("total", "runs", "median", "p95", "max"))
        for r in subset:
            print("  %7.1fm  %6d  %6dms  %6dms  %7dms  %s"
                  % (r["total_ms"] / 60000, r["runs"], r["median_ms"], r["p95_ms"],
                     r["max_ms"], r["event"]))
            print("  %2s        %s" % (marks(r), r["command"][:CMD_W]))
        print()

    table("BLOCKING  (total = runs x median; a fast hook still costs if it fires often)",
          blocking, "* fires on every tool call or prompt, so it blocks the loop each time")
    table("NON-BLOCKING  (async; listed for reference, this time is not spent waiting)",
          [r for r in rows if r["is_async"]])

    hang = [r for r in rows if not r["is_async"] and r["timeout_setting"] is None]
    if hang:
        print("BLOCKING WITH NO EXPLICIT TIMEOUT")
        print("  These fall back to the event default, which can be minutes. One "
              "unresponsive dependency stalls the loop for that long.")
        for r in hang:
            seen = timeout_ms.get(r["_key"])
            limit = "  default limit seen: %sms" % seen if seen else ""
            print("  [%s] max seen %dms%s  %s"
                  % (r["event"], r["max_ms"], limit, r["command"][:CMD_W]))
        print()

    to = [r for r in rows if r["timeouts"]]
    if to:
        print("TIMED OUT (ran until the timeout fired; durationMs is a floor)")
        for r in sorted(to, key=lambda r: -r["timeouts"]):
            lim = timeout_ms.get(r["_key"])
            print("  %dx  [%s] limit=%sms  %s"
                  % (r["timeouts"], r["event"], lim, r["command"][:CMD_W]))
        print()

    fail = [r for r in rows if r["failures"]]
    if fail:
        print("ERRORS")
        for r in sorted(fail, key=lambda r: -r["failures"]):
            print("  %dx  [%s] %s" % (r["failures"], r["event"], r["command"][:CMD_W]))
        print()

    if silent:
        print("CONFIGURED BUT NOT OBSERVED")
        print("  A hook that succeeds with empty output is not persisted, so this is the "
              "expected place for quiet hooks. Zero runs here does not mean it rarely fires.")
        for cmd, m in silent.items():
            print("  [%s]%s %s" % (",".join(sorted(m["events"])),
                                   " (async)" if m["async"] else "",
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
