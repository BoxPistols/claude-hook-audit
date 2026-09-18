# Security

## What this reads, and what it does not do

`scan.py` reads local files only: the session transcripts under `~/.claude/projects/`,
the settings files in the cascade, and the `hooks/hooks.json` of installed plugins. It
makes no network calls, starts no subprocesses, writes no files, and collects no
telemetry. It changes no configuration: every suggestion in the report is for you to
apply by hand.

## Before you share a report

The default report prints absolute paths, which contain your account name, and full hook
command strings, which contain whatever the hook's author put in `settings.json` —
including a credential, if one is there.

Run with `--redact` for anything that leaves your machine. It prints script basenames
with their arguments, and replaces a piped or chained command, or a `-c` script, with a
placeholder rather than its contents. Counts and timings are identical in both modes.

Transcripts themselves carry far more than hook timings. This tool never emits transcript
content: only the hook fields it measures, and the command strings described above.

## Reporting a vulnerability

Use this repository's private vulnerability reporting on GitHub (Security, then Report a
vulnerability). Please do not open a public issue for something exploitable.
