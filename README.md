# claude-hook-audit

Profile [Claude Code](https://code.claude.com) hooks from transcripts that **already exist**.
No wrapper to install in front of your hooks, no synthetic re-runs. Claude Code already
writes `durationMs` for every hook run it reports, and `timedOut` / `timeoutMs` when one
hits its limit. This reads that.

```
Window: 63 sessions + 168 subagent transcripts, 2026-09-10 11:31 - 2026-09-18 07:20 (7.8 days)

Blocking hooks held up the main session for at least 27.7 minutes over this window
  (10 of 15 observed event/hook pairs block). A floor, not a total: a hook
  that succeeds with empty output is never persisted, and 15 configured hooks
  were not observed at all.
  A further 45.8 minutes of hook time ran inside subagents. Those run in parallel
  with each other and with the main loop, so it is not time you waited.

BLOCKING  (main = time the main session spent waiting, summed over its runs)
  * fires on every tool call or prompt, so it blocks the loop each time. p95 needs 20 runs
      main    runs    median       p95        max  event
     12.7m    4644     296ms    1164ms     9045ms  PreToolUse
  *         [status line updater]
            + 2388 of those runs were inside subagents (22.7m, in parallel)
     12.3m    4542     302ms    1190ms     5277ms  PostToolUse
  *         [status line updater]
            + 2335 of those runs were inside subagents (22.8m, in parallel)
  ... 8 further rows

TIMED OUT (ran until the limit fired; durationMs is a floor, and can overshoot it)
  3x  [UserPromptSubmit] limit=30000ms (not set; event default)  [session bootstrap]
  1x  [PreToolUse] limit=10000ms  [pre-write check]

STOPPED A TOOL CALL (hook exited 2)
  Expected from a gate that is doing its job. From a reporting hook it is a bug, and
  it costs a retry every time.
  116x  [PostToolUse] [prose linter]
  32x  [PostToolUse] [prose linter, from a working copy]
  10x  [PostToolUse] [prose linter, from the installed plugin]
```

An excerpt of a real report. Hook names are replaced with brackets, and the two ranked
rows are shown as they read before those hooks were marked `async`; every number is
measured. A real run prints the command, and `--redact` prints the script's file name
with its plain-word arguments.

Two hooks that each look fine at 300ms were costing 25 of those 27.7 minutes. Both only
updated a status display, so neither needed to block. Marking them `"async": true`
removed the wait without losing a single status update.

Then read the second number. Another 45 minutes of the same two hooks ran inside
subagents, where each one holds up that subagent and nothing else. Subagents run
concurrently with each other and with the main loop, so adding that time in would
describe waiting that never happened. The report keeps the two apart, per hook.

The 158 stopped tool calls were one prose linter, registered three times, refusing
writes. That is the hook working as intended. Those records carry no
duration at all, so a hook can be costing a retry on every write while showing zero
time in every other section.

## Install

```
/plugin marketplace add BoxPistols/claude-hook-audit
/plugin install hook-audit@claude-hook-audit
```

Then ask Claude to audit your hooks. To run the scanner without the plugin:

```bash
git clone https://github.com/BoxPistols/claude-hook-audit
python3 claude-hook-audit/skills/hook-audit/scan.py --project "$PWD"
```

Python 3.9+, no dependencies. Reads local files only, makes no network calls, and
changes nothing on its own.

## What it reports

| Section | What it answers |
|---|---|
| Ranked by main-session total | Summed durations, main session only. Catches the cheap hook that fires thousands of times |
| Blocking vs async | Which of these are actually costing you wall clock |
| Subagent runs, per hook | How much of that time was inside a subagent, where it overlaps instead of accumulating |
| No known timeout | Which blocking hooks can stall for the event default, often minutes |
| Timed out | Which hooks ran until their limit fired. `durationMs` there is a floor, and can overshoot the limit |
| Stopped a tool call | Which hooks exited 2. Expected from a gate, a bug plus a retry from a reporter |
| Unrecognized outcomes | Record types this scanner does not know about, rather than silence |
| Configured but not observed | Quiet hooks. A hook that succeeds with empty output is never persisted, so a zero here is the absence of logging, not evidence |

## Why total instead of p95

The usual advice is a per-call ceiling: warn above 2 seconds. That misses the shape of
cost that actually hurts. A 296ms hook on `PreToolUse` fires on every tool call. At
4,600 calls a week that is 12 minutes of blocked loop per event it is attached to, and
every single run looks healthy. Ranking by total surfaces it immediately.

The total is the sum of the recorded durations, not `runs x median`. On real
right-skewed latency the median estimator was 28% low across the corpus this was
developed against, and 35% low on the two hooks that mattered most, because it throws
away every sample above the middle one. Every sample is already in hand, so the exact
sum is free.

`p95` is only printed at 20 runs or more. Below that, nearest-rank puts the 95th
percentile on the largest sample, so it would be the `max` column under a different
name.

## What it can and cannot see

The headline is a floor. Only hook runs that Claude Code persisted are in it, and a hook
that succeeds with empty output is not persisted. The report says how many configured
hooks it never saw.

To decide whether a run blocked the loop, the scanner matches each run against the hook
that produced it. Four things make that matching non-obvious, and all four are handled:

- A hook with a `statusMessage` is recorded under that message, not under its command.
- A plugin declares its hooks in its own `hooks/hooks.json`, not in `settings.json`. Those
  are read too, including for a plugin turned off since, because the window still holds
  the runs it made while it was on.
- A hook that stopped a tool call by exiting 2 is recorded with no `command` field and no
  `durationMs`. The command is recoverable from the message it does carry, so those runs
  are attributed rather than dropped, and they get a row even with no timing.
- A plugin command can be recorded with `${CLAUDE_PLUGIN_ROOT}` already expanded, so both
  forms are indexed.

Anything still unmatched is marked `?` in the report and counted as blocking, which is the
conservative reading, not a measurement. Records that name no hook at all are counted and
named rather than silently dropped, and an outcome type this scanner does not recognize is
reported as such: this reads a format it does not own.

A file it could not read, and a settings file that exists but does not parse, are reported
as warnings. An empty configuration would otherwise produce a confident report in which
nothing is async and nothing has a timeout. A `--settings` or `--project` path that does
not exist is an error rather than a warning.

## Sharing a report

The default report prints absolute paths and full command strings. Both identify your
account, and a command carries whatever its author put in `settings.json`, including a
credential passed as an argument. Use `--redact` for a report that leaves your machine:

```
  *         notify.py busy            # instead of /usr/bin/python3 "$HOME/hooks/notify.py" busy
            send.py --token …         # instead of python3 "$HOME/hooks/send.py" --token tok-1234
            <inline shell>            # a piped or chained command is not shown at all
```

`--redact` keeps the script's file name, and an argument only when it is a short plain
word or a file name, because those tell two hooks apart. Every other argument becomes
`…`, and so does the value after a flag such as `--token` or `--password`. A URL is
masked, and a `NAME=value` in front of the command is dropped. A `statusMessage` is
printed as written, since it is text you chose to display.

The masking goes by the shape of each argument, not by recognizing a secret, so read a
redacted report once before you share it. Counts and timings are identical in both modes.

## Prior art

Hook timing has been measured before, but always prospectively:

| Tool | Approach | Trade-off |
|---|---|---|
| [hook-latency-wrap](https://dev.to/bokuwalily/find-out-which-claude-code-hook-is-slow-measure-p95-with-a-one-line-wrapper-3ek6) | Prepend a wrapper to each hook command in `settings.json`, accumulate a log | Requires editing settings first, then waiting for data |
| [claudekit `profile`](https://github.com/carlrannaberg/claudekit/blob/main/docs/guides/hook-profiling.md) | Execute hooks directly with `--iterations` | Measures the hook, not how often it actually fires |

Both are useful. This one differs by answering the question *today*, from the week of
data already sitting in `~/.claude/projects/`, and by separating blocking hooks from
async ones so the total means something.

For context window cost rather than hook latency, see
[unclog](https://github.com/thomaschill/unclog), which audits skills, agents, commands
and MCP servers. This project deliberately does not overlap with it.

## Notes

- The mode column (blocking or async) reads your current configuration. Timings come from
  the window. After changing a hook, its past runs still appear under the new mode.
- Transcript retention is capped by `cleanupPeriodDays`. Nothing before that window can
  be judged from this data, and the report says so.
- `--json` emits the same data for scripting, including the window bounds, the observed
  timeout limit per hook, unread files and unattributed records. `--files N` limits the
  window to the N most recent transcript files, newest first by modification time.
  Subagents write their own transcript files, and on a busy week they outnumber the
  session ones, so a small `--files` covers fewer sessions than it looks like.
- A timeout in `settings.json` is in seconds; everything a transcript records is in
  milliseconds. The JSON keeps them as `timeout_s` and `timeout_observed_ms`.
- Read from the settings cascade: `~/.claude/settings.json`, `settings.local.json`, the
  project's `.claude/settings*.json` with `--project`, any `--settings FILE`, and the
  `hooks/hooks.json` of each installed plugin.

## What it was validated against

The record shapes above were read off about 10,000 hook records written by Claude Code
2.1.267 through 2.1.275, on one machine with one person's hook configuration. That is the
evidence behind every claim here about what the transcript contains. A different version
may write something different, so an outcome type this scanner does not recognize is
reported as unrecognized rather than dropped, and a run it cannot attribute to a hook is
counted and named. If your report shows either, the format has moved and the numbers
around it deserve a second look.

## Development

```bash
python3 -m unittest discover -s tests
```

The tests run the scanner against a synthetic config directory whose records reproduce
each shape described above. Each one was checked by reverting the behavior it covers and
confirming the suite fails.

## License

MIT

---

# claude-hook-audit (日本語)

Claude Codeのフックを、**既にあるログから**後追いで計測します。ラッパーの仕込みも、計測のための再実行も要りません。Claude Codeはフックの実行ごとに`durationMs`を、タイムアウト時には`timedOut`と`timeoutMs`をセッションログに書いています。それを読みます。

## これが解く問題

1回あたり296msのフックは、どの基準でも「速い」に見えます。それが`PreToolUse`に付いていて週4,600回発火すると、**12分ぶんループが止まります**。同じものが`PostToolUse`にも付いていれば倍です。1回の速さだけを見ていると気づけません。

合計時間で並べ替えると、これが最上位に出ます。合計は記録された所要時間の実測和で、`発火回数 × 中央値`ではありません。中央値を使うと、開発時に使った実データでは全体で28%、影響の大きい2件では35%低く出ました。中央値より上の標本を全部捨てるためです。標本は全部手元にあるので、正確な和を取るほうが安いです。

`p95`は20件以上のときだけ出します。それ未満では最近傍順位の95パーセンタイルが最大値そのものになるため、`max`列と同じ数字を別の名前で出すことになります。

## 導入

```
/plugin marketplace add BoxPistols/claude-hook-audit
/plugin install hook-audit@claude-hook-audit
```

Python 3.9以上、依存なし。ローカルのファイルを読むだけで、通信もせず、何も書き換えません。

## 出力

| 区分 | 答えること |
|---|---|
| メインセッションでの合計時間の降順 | メインセッションでの所要時間の実測和。安いフックが大量に発火している場合を捕まえる |
| ブロックと非同期の区別 | 実際に待たされているのはどれか |
| サブエージェント内の実行(フックごと) | そのうちサブエージェント内で走った時間。並行して走るため、合計には積み上がらない |
| タイムアウト未設定 | 既定値(数分になりうる)まで止まりうるフックはどれか |
| タイムアウト発生 | 上限まで走ったもの。`durationMs`は下限であって所要時間ではない |
| ツール呼び出しを止めた | 終了コード2で止めたフック。ゲートなら想定どおり、報告用のフックなら不具合で、毎回やり直しが発生する |
| 未知の結果 | このスキャナが知らない型の記録。黙って捨てずに出す |
| 設定にあるが記録なし | 無音のフック。成功して出力が空のものはログに残らないため、ゼロは「記録が無い」であって「使われていない」ではない |

## サブエージェントの実行を合計に入れない

サブエージェント内で動いたフックは、そのサブエージェントを止めますが、利用者を待たせてはいません。サブエージェントは互いに、そしてメインのループと並行して走るためです。開発時のデータでは、メインセッションを止めた時間が27.7分だったのに対し、サブエージェント内でさらに45.8分ぶんフックが走っていました。そのほとんどは上位2行のフックでした。合計に足すと、起きていない待ち時間を記述することになるので、分けて出します。

## 見えるものと見えないもの

合計値は下限です。ログに永続化された実行しか入っておらず、成功して出力が空のフックは永続化されません。見えていない設定済みフックが何件あるかはレポートに出ます。

ブロックしたかどうかの判定には、実行1件ごとに元のフック設定を突き合わせます。素直でない点が4つあり、すべて扱っています。

- `statusMessage`を持つフックは、コマンドではなくそのメッセージでログに記録されます
- プラグインのフックは`settings.json`ではなくプラグイン自身の`hooks/hooks.json`に書かれています。後から無効化したプラグインも読みます(有効だった期間の実行がログに残っているため)
- 終了コード2でツール呼び出しを止めたフックは、`command`も`durationMs`も持ちません。メッセージ側にコマンドが入っているので、そこから復元して計上します。所要時間が無くても行として出します
- プラグインのコマンドは`${CLAUDE_PLUGIN_ROOT}`が展開済みの形で記録されることがあるため、両方の形で索引します

それでも突き合わせられなかったものは`?`を付け、ブロックとして数えます。これは安全側の仮定であって、計測結果ではありません。識別できない記録は件数と型名を出します。知らない型の記録も「知らない型」として報告します。自分が仕様を持っていない形式を読んでいるためです。

ここで挙げた記録の形は、Claude Code 2.1.267から2.1.275が書いた約10,000件のhook記録から読み取ったものです。1台、1人のフック構成ぶんです。別の版では違う形を書く可能性があるため、知らない型は「知らない型」として、識別できない記録は件数と型名として出します。どちらかがレポートに出たら、形式が変わったと考えて周辺の数字を見直してください。

読めなかったファイルと、存在するのにパースできない設定ファイルは警告として出します。黙って空の設定として扱うと、「どれも非同期でなく、どれもタイムアウト未設定」という確信のあるレポートが出てしまいます。`--settings`と`--project`に存在しないパスを渡した場合は警告ではなくエラーにします。

## レポートを人に渡すとき

既定の出力には絶対パスとコマンド文字列がそのまま出ます。どちらもアカウント名を含み、コマンドは引数に渡した認証情報も含めて`settings.json`に書かれた内容をそのまま持ちます。手元から出す場合は`--redact`を使います。スクリプトのファイル名と、短い英単語かファイル名の形をした引数だけを残し、それ以外の引数は`…`に置き換えます。`--token`や`--password`のようなフラグの次の値とURLも`…`になり、コマンドの前の`NAME=値`は出しません。パイプや連結を含むコマンドは`<inline shell>`に置き換わります。`statusMessage`は表示用に書いた文なので、そのまま出します。

引数の形で判定していて、秘密の値そのものを見分けているわけではないため、渡す前に一度目を通してください。件数と時間は両モードで同一です。

## 処方

役割で分けます。判定するフック(検査・ゲート・書き込み前の確認)はブロックする必要があるので、そのままにします。知らせるだけのフック(ステータス表示・通知・ログ送信)は`"async": true`にします。全イベントで動いたまま、待ち時間だけ消えます。matcherを削る方法より副作用が小さく、表示の粒度を失いません。

ブロックするフックには必ず`timeout`を明示します。既定値は数分になりうるので、応答しない相手を待ち続けます。

## 既存のものとの違い

フックの計測自体は先行例がありますが、いずれも事前の仕込みが要ります。ラッパー方式は`settings.json`を書き換えてから待つ必要があり、合成実行方式は実際の発火頻度を反映しません。このツールは、既に`~/.claude/projects/`にある1週間ぶんのデータから、今すぐ答えを出します。

コンテキスト消費(スキル・MCP・CLAUDE.mdの大きさ)は[unclog](https://github.com/thomaschill/unclog)が扱っているので、この道具では重複させていません。
