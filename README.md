# claude-hook-audit

Profile [Claude Code](https://code.claude.com) hooks from transcripts that **already exist**.
No wrapper to install in front of your hooks, no synthetic re-runs. Claude Code already
writes `durationMs` for every hook run it reports, and `timedOut` / `timeoutMs` when one
hits its limit. This reads that.

```
Window: 213 transcripts, 2026-09-10 11:31 - 2026-09-17 23:25 (7.4 days)

Blocking hooks cost about 36.2 minutes over this window (6 of 9 observed hooks block).

BLOCKING  (total = runs x median; a fast hook still costs if it fires often)
     total    runs    median       p95        max  event
     17.6m    3662     297ms    1233ms     9045ms  PreToolUse
            <status line updater>
     18.0m    3584     303ms    1242ms     4052ms  PostToolUse
            <status line updater>

BLOCKING WITH NO EXPLICIT TIMEOUT
  [PreToolUse] max seen 777576ms  <desktop widget hook>

TIMED OUT (ran until the timeout fired; durationMs is a floor)
  2x  [PreToolUse] limit=600000ms  <desktop widget hook>
```

Two hooks that each look fine at 300ms were costing half an hour a week. Both only
updated a status display, so neither needed to block. Marking them `"async": true`
removed the wait without losing a single status update.

## Install

```
/plugin marketplace add BoxPistols/claude-hook-audit
/plugin install hook-audit@claude-hook-audit
```

Then ask Claude to audit your hooks, or run the scanner directly:

```bash
python3 ~/.claude/plugins/.../skills/hook-audit/scan.py --project "$PWD"
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

- The mode column (blocking or async) reads your current settings. Timings come from the
  window. After changing a hook, its past runs still appear under the new mode.
- Transcript retention is capped by `cleanupPeriodDays`. Nothing before that window can
  be judged from this data, and the report says so.
- `--json` emits the same data for scripting.

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

## 処方

役割で分けます。判定するフック(検査・ゲート・書き込み前の確認)はブロックする必要があるので、そのままにします。知らせるだけのフック(ステータス表示・通知・ログ送信)は`"async": true`にします。全イベントで動いたまま、待ち時間だけ消えます。matcherを削る方法より副作用が小さく、表示の粒度を失いません。

ブロックするフックには必ず`timeout`を明示します。既定値は数分になりうるので、応答しない相手を待ち続けます。

## 既存のものとの違い

フックの計測自体は先行例がありますが、いずれも事前の仕込みが要ります。ラッパー方式は`settings.json`を書き換えてから待つ必要があり、合成実行方式は実際の発火頻度を反映しません。このツールは、既に`~/.claude/projects/`にある1週間ぶんのデータから、今すぐ答えを出します。

コンテキスト消費(スキル・MCP・CLAUDE.mdの大きさ)は[unclog](https://github.com/thomaschill/unclog)が扱っているので、この道具では重複させていません。
