# claude-hook-audit

Profile [Claude Code](https://code.claude.com) hooks from transcripts that **already exist**.
No wrapper to install in front of your hooks, no synthetic re-runs. Claude Code already
writes `durationMs` for every hook run it reports, and `timedOut` / `timeoutMs` when one
hits its limit. This reads that.

```
Window: 213 transcripts, 2026-09-10 11:31 - 2026-09-17 23:25 (7.4 days)

Blocking hooks cost at least 36.2 minutes over this window (6 of 9 observed hooks block).
  A floor, not a total: a hook that succeeds with empty output is never persisted, and
  15 configured hooks were not observed at all.

BLOCKING  (total = runs x median; a fast hook still costs if it fires often)
  * fires on every tool call or prompt, so it blocks the loop each time
     total    runs    median       p95        max  event
     17.6m    3662     297ms    1233ms     9045ms  PreToolUse
  *         [status line updater]
     18.0m    3584     303ms    1242ms     4052ms  PostToolUse
  *         [status line updater]

BLOCKING WITH NO EXPLICIT TIMEOUT
  [PreToolUse] max seen 777576ms  default limit seen: 600000ms  [desktop widget hook]

TIMED OUT (ran until the timeout fired; durationMs is a floor)
  2x  [PreToolUse] limit=600000ms  [desktop widget hook]
```

Hook names are in brackets here because this sample is edited. A real run prints the
command; `--redact` prints the script basename and its arguments.

Two hooks that each look fine at 300ms were costing half an hour a week. Both only
updated a status display, so neither needed to block. Marking them `"async": true`
removed the wait without losing a single status update.

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
| Ranked by total blocking time | `runs x median`. Catches the cheap hook that fires thousands of times |
| Blocking vs async | Which of these are actually costing you wall clock |
| No explicit timeout | Which blocking hooks can stall for the event default, often minutes |
| Timed out | Which hooks ran until their limit fired. `durationMs` there is a floor |
| Errors | Hooks failing rather than just being slow |
| Configured but not observed | Quiet hooks. A hook that succeeds with empty output is never persisted, so a zero here is the absence of logging, not evidence |

## Why total instead of p95

The usual advice is a per-call ceiling: warn above 2 seconds. That misses the shape of
cost that actually hurts. A 290ms hook on `PreToolUse` fires on every tool call. At
7,000 calls a week that is 34 minutes of blocked loop, and every single run looks
healthy. Ranking by `runs x median` surfaces it immediately.

## What it can and cannot see

The headline is a floor. Only hook runs that Claude Code persisted are in it, and a hook
that succeeds with empty output is not persisted. The report says how many configured
hooks it never saw.

To decide whether a run blocked the loop, the scanner matches each run against the hook
that produced it. Two things make that matching non-obvious, and both are handled:

- A hook with a `statusMessage` is recorded under that message, not under its command.
  Matching on the command alone reports such a hook twice: once as "never observed", and
  once as an unrecognized entry assumed to be blocking with no timeout.
- A plugin declares its hooks in its own `hooks/hooks.json`, not in `settings.json`. Those
  are read too, including for a plugin turned off since, because the window still holds
  the runs it made while it was on.

Anything still unmatched is marked `?` in the report and counted as blocking, which is the
conservative reading, not a measurement.

## Sharing a report

The default report prints absolute paths and full command strings. Both identify your
account, and an inline shell hook carries whatever its author put in `settings.json`.
Use `--redact` for a report that leaves your machine:

```
  *         notify.py busy            # instead of /usr/bin/python3 "$HOME/hooks/notify.py" busy
            <inline shell>            # a piped or chained command is not shown at all
```

Counts and timings are identical in both modes.

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
- `--json` emits the same data for scripting. `--files N` limits the window to the N most
  recent transcripts.
- Read from the settings cascade: `~/.claude/settings.json`, `settings.local.json`, the
  project's `.claude/settings*.json` with `--project`, any `--settings FILE`, and the
  `hooks/hooks.json` of each installed plugin.

## Development

```bash
python3 -m unittest discover -s tests
```

The tests run the scanner against a synthetic config directory. They cover the two
matching cases above, per-event `async`, timeout versus user cancellation, and that
`--redact` leaks no path.

## License

MIT

---

# claude-hook-audit (日本語)

Claude Codeのフックを、**既にあるログから**後追いで計測します。ラッパーの仕込みも、計測のための再実行も要りません。Claude Codeはフックの実行ごとに`durationMs`を、タイムアウト時には`timedOut`と`timeoutMs`をセッションログに書いています。それを読みます。

## これが解く問題

1回あたり290msのフックは、どの基準でも「速い」に見えます。それが`PreToolUse`に付いていて週7,000回発火すると、**34分ぶんループが止まります**。1回の速さだけを見ていると気づけません。

`発火回数 × 中央値` で並べ替えると、これが最上位に出ます。

## 導入

```
/plugin marketplace add BoxPistols/claude-hook-audit
/plugin install hook-audit@claude-hook-audit
```

Python 3.9以上、依存なし。ローカルのファイルを読むだけで、通信もせず、何も書き換えません。

## 出力

| 区分 | 答えること |
|---|---|
| 合計ブロック時間の降順 | `発火回数 × 中央値`。安いフックが大量に発火している場合を捕まえる |
| ブロックと非同期の区別 | 実際に待たされているのはどれか |
| タイムアウト未設定 | 既定値(数分になりうる)まで止まりうるフックはどれか |
| タイムアウト発生 | 上限まで走ったもの。`durationMs`は下限であって所要時間ではない |
| エラー | 遅いのではなく失敗しているもの |
| 設定にあるが記録なし | 無音のフック。成功して出力が空のものはログに残らないため、ゼロは「記録が無い」であって「使われていない」ではない |

## 見えるものと見えないもの

合計値は下限です。ログに永続化された実行しか入っておらず、成功して出力が空のフックは永続化されません。見えていない設定済みフックが何件あるかはレポートに出ます。

ブロックしたかどうかの判定には、実行1件ごとに元のフック設定を突き合わせます。ここが素直でない点が2つあり、どちらも扱っています。

- `statusMessage`を持つフックは、コマンドではなくそのメッセージでログに記録されます。コマンドだけで突き合わせると、同じフックが「記録なし」と「設定に無い(ブロック・タイムアウト未設定と仮定)」の2箇所に出ます
- プラグインのフックは`settings.json`ではなくプラグイン自身の`hooks/hooks.json`に書かれています。こちらも読みます。後から無効化したプラグインも読みます(有効だった期間の実行がログに残っているため)

それでも突き合わせられなかったものは`?`を付け、ブロックとして数えます。これは安全側の仮定であって、計測結果ではありません。

## レポートを人に渡すとき

既定の出力には絶対パスとコマンド文字列がそのまま出ます。どちらもアカウント名を含み、インラインシェルのフックは`settings.json`に書かれた内容をそのまま持ちます。手元から出す場合は`--redact`を使います。スクリプト名と引数だけになり、パイプや連結を含むコマンドは`<inline shell>`に置き換わります。件数と時間は両モードで同一です。

## 処方

役割で分けます。判定するフック(検査・ゲート・書き込み前の確認)はブロックする必要があるので、そのままにします。知らせるだけのフック(ステータス表示・通知・ログ送信)は`"async": true`にします。全イベントで動いたまま、待ち時間だけ消えます。matcherを削る方法より副作用が小さく、表示の粒度を失いません。

ブロックするフックには必ず`timeout`を明示します。既定値は数分になりうるので、応答しない相手を待ち続けます。

## 既存のものとの違い

フックの計測自体は先行例がありますが、いずれも事前の仕込みが要ります。ラッパー方式は`settings.json`を書き換えてから待つ必要があり、合成実行方式は実際の発火頻度を反映しません。このツールは、既に`~/.claude/projects/`にある1週間ぶんのデータから、今すぐ答えを出します。

コンテキスト消費(スキル・MCP・CLAUDE.mdの大きさ)は[unclog](https://github.com/thomaschill/unclog)が扱っているので、この道具では重複させていません。
