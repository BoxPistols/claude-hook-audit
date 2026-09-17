#!/usr/bin/env python3
"""Profile Claude Code hooks from transcripts that already exist. No instrumentation.

Claude Code records every hook run it reports as an attachment entry carrying
durationMs, and records timeouts with timedOut/timeoutMs. This reads those
entries, ranks hooks by total blocking time, and cross-references the settings
cascade to say which of them actually block the loop.

usage: hook-audit [--root DIR] [--settings FILE ...] [--project DIR] [--files N] [--json]
"""
import argparse
import collections
import datetime
import json
import os
import statistics
import sys

HOOK_TYPES = ("hook_success", "hook_non_blocking_error",
              "hook_error_during_execution", "hook_cancelled")

# Rules of thumb. A hook on a per-tool-call event blocks the loop every time it fires.
PER_CALL_EVENTS = {"PreToolUse", "PostToolUse", "UserPromptSubmit",
                   "PermissionRequest", "PermissionDenied", "PostToolUseFailure"}


def config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def settings_index(paths):
    """command string -> {async, timeout, events, matchers, sources}"""
    idx = {}
    for p in paths:
        data = load_json(p)
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
                    e = idx.setdefault(cmd, {"async": False, "timeout": None,
                                             "events": set(), "matchers": set(),
                                             "sources": set()})
                    e["async"] = e["async"] or bool(h.get("async"))
                    if h.get("timeout") is not None:
                        e["timeout"] = h["timeout"]
                    e["events"].add(event)
                    e["matchers"].add(g.get("matcher") or "*")
                    e["sources"].add(os.path.basename(p))
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
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    args = ap.parse_args()

    files = iter_files(args.root, args.files)
    if not files:
        sys.exit(f"no transcripts found under {args.root}")

    paths = [os.path.join(config_dir(), "settings.json")]
    if args.project:
        paths += [os.path.join(args.project, ".claude", "settings.json"),
                  os.path.join(args.project, ".claude", "settings.local.json")]
    paths += args.settings
    cfg = settings_index([p for p in paths if os.path.exists(p)])

    runs, outcomes, timeouts, timeout_ms = collect(files)

    rows = []
    for key, arr in runs.items():
        arr.sort()
        meta = cfg.get(key[1], {})
        med = statistics.median(arr)
        rows.append({
            "event": key[0],
            "command": key[1],
            "runs": len(arr),
            "median_ms": round(med),
            "p95_ms": round(arr[min(len(arr) - 1, int(len(arr) * 0.95))]),
            "max_ms": round(arr[-1]),
            "total_blocking_ms": round(len(arr) * med) if not meta.get("async") else 0,
            "total_ms": round(len(arr) * med),
            "timeouts": timeouts.get(key, 0),
            "timeout_setting": meta.get("timeout"),
            "is_async": bool(meta.get("async")),
            "in_settings": key[1] in cfg,
            "failures": outcomes[key].get("hook_error_during_execution", 0)
                        + outcomes[key].get("hook_non_blocking_error", 0),
        })
    rows.sort(key=lambda r: (-r["total_blocking_ms"], -r["total_ms"]))

    span_days = (files[0][0] - files[-1][0]) / 86400
    observed = {r["command"] for r in rows}
    silent = [(c, m) for c, m in cfg.items() if c not in observed]

    if args.json:
        json.dump({"window": {"transcripts": len(files), "days": round(span_days, 2)},
                   "hooks": rows,
                   "configured_but_unobserved": [c for c, _ in silent]},
                  sys.stdout, indent=2, ensure_ascii=False)
        print()
        return

    def when(ts):
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

    print(f"Window: {len(files)} transcripts, {when(files[-1][0])} - {when(files[0][0])} "
          f"({span_days:.1f} days)")
    print("Transcripts older than your cleanupPeriodDays are gone; nothing before the "
          "window can be judged from this.\n")

    blocking = [r for r in rows if not r["is_async"]]
    total_block = sum(r["total_blocking_ms"] for r in blocking)
    print(f"Blocking hooks cost about {total_block/60000:.1f} minutes over this window "
          f"({len(blocking)} of {len(rows)} observed hooks block).\n")

    def table(title, subset, note=None):
        if not subset:
            return
        print(title)
        if note:
            print(f"  {note}")
        print(f"  {'total':>8}  {'runs':>6}  {'median':>8}  {'p95':>8}  {'max':>9}  event")
        for r in subset:
            print(f"  {r['total_ms']/60000:7.1f}m  {r['runs']:6d}  {r['median_ms']:6d}ms  "
                  f"{r['p95_ms']:6d}ms  {r['max_ms']:7d}ms  {r['event']}")
            print(f"  {'':>8}  {r['command'][:100]}")
        print()

    table("BLOCKING  (total = runs x median; a fast hook still costs if it fires often)",
          blocking)
    table("NON-BLOCKING  (async; listed for reference, this time is not spent waiting)",
          [r for r in rows if r["is_async"]])

    hang = [r for r in rows if not r["is_async"] and r["timeout_setting"] is None]
    if hang:
        print("\nBLOCKING WITH NO EXPLICIT TIMEOUT")
        print("  These fall back to the event default, which can be minutes. One "
              "unresponsive dependency stalls the loop for that long.")
        for r in hang:
            print(f"  [{r['event']}] max seen {r['max_ms']}ms  {r['command'][:80]}")

    to = [r for r in rows if r["timeouts"]]
    if to:
        print("\nTIMED OUT (ran until the timeout fired; durationMs is a floor)")
        for r in sorted(to, key=lambda r: -r["timeouts"]):
            lim = timeout_ms.get((r["event"], r["command"]))
            print(f"  {r['timeouts']}x  [{r['event']}] limit={lim}ms  {r['command'][:70]}")

    fail = [r for r in rows if r["failures"]]
    if fail:
        print("\nERRORS")
        for r in sorted(fail, key=lambda r: -r["failures"]):
            print(f"  {r['failures']}x  [{r['event']}] {r['command'][:70]}")

    if silent:
        print("\nCONFIGURED BUT NOT OBSERVED")
        print("  A hook that succeeds with empty output is not persisted, so this is the "
              "expected place for quiet hooks. Zero runs here does not mean it rarely fires.")
        for c, m in silent:
            ev = ",".join(sorted(m["events"]))
            flag = " (async)" if m["async"] else ""
            print(f"  [{ev}]{flag} {c[:80]}")

    per_call = [r for r in blocking if r["event"] in PER_CALL_EVENTS]
    if per_call:
        print("\nWHAT TO DO")
        print("  Hooks marked * block the loop every time they fire. Split them by role:")
        print("    decides  - a check, a gate, a pre-write validation. It has to block. Leave it.")
        print("    reports  - a status line, a notification, a log shipper. It does not.")
        print("  Set \"async\": true on the reporting ones. They keep firing on every event;")
        print("  only the waiting goes away. Narrowing the matcher also works but loses coverage.")
        print("  Give every blocking hook an explicit \"timeout\" so a hang cannot stall the loop.")


if __name__ == "__main__":
    main()
