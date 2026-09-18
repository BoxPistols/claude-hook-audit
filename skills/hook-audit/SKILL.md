---
name: hook-audit
description: Profile Claude Code hooks from transcripts that already exist, with no instrumentation. Ranks hooks by the time they held up the main session, separates hooks that block the loop from async ones, finds timeouts and hangs, and proposes async conversion. Use when sessions feel sluggish, a hook is suspected of being slow, tool calls stall, or hooks need review. Triggers: slow hook, hooks are slow, session feels sluggish, tool calls hang, hook timeout, PreToolUse latency, hook performance, blocking hooks.
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

The `main` column is the sum of the recorded durations for the main session. A hook at
296ms that fires 4,600 times on `PreToolUse` in a week costs 12 minutes on that event
alone. A rule of thumb like "warn above 2 seconds" never sees it. Rank by total first,
then look at the tail.

Do not recompute a total as `runs x median`. That throws away every sample above the
middle one and ran 28% low on the corpus this was built against.

### 2. Blocking or not

A hook with `"async": true` does not hold up the loop. Its time is real but nobody
waits for it, so it does not belong in the same total as a blocking hook. Read the
two tables separately and never propose "removing" an async hook to save time.

Rows marked `*` fire on every tool call or prompt. Rows marked `?` were not found in
any configuration, so their mode is an assumption; say so rather than reporting them
as measured blocking time.

### 2b. Subagent time is not time anyone waited

A row that says `+ N of those runs were inside subagents` is telling you that part of
its cost was paid inside a subagent, which runs in parallel with the main loop and with
other subagents. Those minutes overlap, so they cannot be added to the main-session
total. Never quote the two figures as one number, and never present subagent time as
time the user waited.

### 3. The tail is a separate question from the median

A healthy median with a multi-minute maximum means one run stalled the session. Look
at the max column and the timeout section independently of the ranking.

A `-` in the `p95` column means the hook has fewer than 20 recorded runs, so a 95th
percentile would just be the maximum. Read `max` there and say the sample is small.

### 4. Timeouts are a floor, not a duration

A hook that timed out ran until its limit fired, and the recorded `durationMs` can
overshoot that limit (19,455ms against a 10,000ms limit, in real data). So it is a
floor on the wait, never a measurement of how long the work needed. A hook that
repeatedly times out is the worst blocking case even though it never records a success.

Only entries with `timedOut: true` are timeouts. The field is present either way, and
`timedOut: false` is the user pressing Esc, which says nothing about hook speed.

A hook listed under "blocking with no known timeout" has no `timeout` in its
configuration and no observed limit either. If a limit was observed, it appears in the
timeout section instead, marked as coming from the event default.

### 5. Exit 2 is not slowness

"Stopped a tool call" counts runs where the hook exited 2 and refused the call. For a
gate that is the hook working. For a reporting hook it is a bug, and it costs a retry
every time. These records carry no duration, so such a hook can be expensive while
showing no time at all.

### 6. Silence is not absence

A hook that succeeds with empty output is not persisted to the transcript. Hooks listed
under "configured but not observed" are the expected case for quiet hooks. Zero recorded
runs does not mean the hook rarely fires. Judge those by reading the command, not by the
count. For the same reason the headline total is a floor, not a total.

Warnings about a file that could not be read, or a settings file that did not parse,
matter more than anything else in the report: with no configuration read, every hook
reads as blocking with no timeout. Fix that before interpreting a single number.

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
