---
name: hook-audit
description: Profile Claude Code hooks from transcripts that already exist, with no instrumentation. Ranks hooks by total blocking time (runs x median), separates hooks that block the loop from async ones, finds timeouts and hangs, and proposes async conversion. Use when sessions feel sluggish, a hook is suspected of being slow, tool calls stall, or hooks need review. Triggers: slow hook, hooks are slow, session feels sluggish, tool calls hang, hook timeout, PreToolUse latency, hook performance, blocking hooks.
---

# Hook audit

Claude Code records each hook run it reports as an attachment entry in the session
transcript, carrying `durationMs`, and records timeouts with `timedOut` and `timeoutMs`.
That data is already on disk. This skill reads it. Nothing needs to be installed in
front of the hooks first, and no hook is re-executed to measure it.

## Run it

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/hook-audit/scan.py" --project "$PWD"
```

Useful flags: `--files N` to limit the window to the N most recent transcripts,
`--json` for machine-readable output, `--settings FILE` to add a settings file,
`--redact` to print basenames instead of full commands.

Blocking or async comes from the current configuration: the settings cascade plus the
`hooks/hooks.json` of each installed plugin. The timings come from the window. After
changing a hook's configuration, past runs still appear under the new mode.

## Read the report in this order

### 1. Total, not per-call

`total = runs x median`. A hook at 290ms that fires 7,000 times in a week costs about
34 minutes. A rule of thumb like "warn above 2 seconds" never sees that hook. Rank by
total first, then look at the tail.

### 2. Blocking or not

A hook with `"async": true` does not hold up the loop. Its time is real but nobody
waits for it, so it does not belong in the same total as a blocking hook. Read the
two tables separately and never propose "removing" an async hook to save time.

Rows marked `*` fire on every tool call or prompt. Rows marked `?` were not found in
any configuration, so their mode is an assumption; say so rather than reporting them
as measured blocking time.

### 3. The tail is a separate question from the median

A healthy median with a multi-minute maximum means one run stalled the session. Look
at the max column and the timeout section independently of the ranking.

### 4. Timeouts are a floor, not a duration

A hook that timed out ran until its limit fired. `durationMs` is the limit, not how
long the work needed. A hook that repeatedly times out is the worst blocking case even
though it never records a success.

Only entries carrying `timedOut: true` are timeouts. A cancellation without that field
is the user pressing Esc and says nothing about hook speed.

### 5. Silence is not absence

A hook that succeeds with empty output is not persisted to the transcript. Hooks listed
under "configured but not observed" are the expected case for quiet hooks. Zero recorded
runs does not mean the hook rarely fires. Judge those by reading the command, not by the
count. For the same reason the headline total is a floor, not a total.

## What to change

Split hooks by role before touching anything.

| Role | Examples | Blocking needed |
|---|---|---|
| Decides | checks, gates, pre-write validation, formatters that must finish first | Yes. Leave it |
| Reports | status lines, notifications, log shippers, desktop widgets | No |

For a reporting hook, set `"async": true` on its entry. It keeps firing on every event
and the waiting disappears. This is a smaller change than narrowing the matcher, which
trades away coverage for speed.

Give every blocking hook an explicit `"timeout"`. Without one it falls back to the event
default, which can be minutes, so a single unresponsive dependency stalls the loop for
that long.

For a blocking hook that is genuinely slow, in order of preference: cache its result,
narrow its matcher to the tools it actually cares about, move the slow part to a
background process, or remove it.

Do not edit hook configuration without saying what will change and getting agreement.
A hook exists because someone wanted the behavior.

## Before pasting the report anywhere

The default output carries absolute paths, which name the account, and full command
strings, which can carry a token if the hook is inline shell. Re-run with `--redact`
for anything that leaves the machine. Counts and timings are the same either way.

## Scope

Hooks only. For context window cost, unused skills, MCP servers and CLAUDE.md size,
`unclog` covers that ground and this skill deliberately does not.
