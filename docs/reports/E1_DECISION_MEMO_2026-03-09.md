# E1 Decision Memo

Artifacts reviewed:

- `data/backtest_results/2025-11-17_to_2026-02-25_full_local_realonly.json`
- `data/backtest_results/2025-11-17_to_2026-02-25_full_local_matchlive_noblend_opening.json`

Scope:

- Strict subset only: `used_real_line = true`
- `policy_pass = true`
- `stat in {pts, ast}`
- `bin in {0, 9}`
- Graded outcomes only: `win`, `loss`

## Verified

### Run 1: Real-only

- Strict subset: `288` bets, `76.389%` hit, `39.731%` ROI/bet
- `pts`: `181` bets, `72.928%` hit, `34.488%` ROI/bet
- `ast`: `107` bets, `82.243%` hit, `48.599%` ROI/bet
- `bin 0`: `280` bets, `76.071%` hit, `39.359%` ROI/bet
- `bin 9`: `8` bets, `87.5%` hit, `52.742%` ROI/bet
- `reports.full.brierByStat`: `pts=0.2587`, `ast=0.2486`

### Run 2: Match-live, no-blend, opening line timing

- Strict subset: `271` bets, `78.967%` hit, `44.743%` ROI/bet
- `pts`: `167` bets, `75.449%` hit, `40.461%` ROI/bet
- `ast`: `104` bets, `84.615%` hit, `51.619%` ROI/bet
- `bin 0`: `263` bets, `78.707%` hit, `44.107%` ROI/bet
- `bin 9`: `8` bets, `87.5%` hit, `65.631%` ROI/bet
- `reports.full.brierByStat`: `pts=0.2576`, `ast=0.2477`

### Confidence interval check

For the live-equivalent `bin 0` subset (`263` bets, `78.707%` hit), the 95% Wilson interval is approximately `73.364%` to `83.223%`.

## Unsupported or overstated claims

1. `288` and `271` are not total bets in the artifacts.
   The artifacts each contain about `35.5k` graded emitted rows. `288` and `271` are the strict real-line policy subset only.

2. Match-live is not just "the same set minus 17 marginal bets."
   The two strict subsets overlap on only `196` bets. There are `92` strict bets unique to the real-only run and `75` strict bets unique to the match-live run. `--match-live --line-timing opening` changes the line timing and bet identities materially.

3. "Merge-ready" is not a factual conclusion from these artifacts alone.
   Both artifacts have `walkForward = false`, so they do not eliminate calibration and policy lookahead the way a strict walk-forward validation would.

## Policy choice, not fact

Under the original E1 acceptance rule of `hitRate >= 80%` on the strict real-line subset, E1 does **not** pass:

- Real-only `bin 0`: `76.071%`
- Match-live `bin 0`: `78.707%`

If the team wants to revise E1 to something like `hitRate >= 75%` and `ROI >= 20%`, or treat `80%` as a soft target rather than a hard gate, that is a new policy decision. It should be recorded prospectively in the policy history rather than used retroactively to declare the original gate passed.

## Current recommendation

- Treat the branch as **profitable but below the original E1 hit-rate bar**.
- Use the live-equivalent run for realism, not the broad real-only run, when discussing current deployment behavior.
- If a go-live or merge decision depends on E1, either:
  - keep the original `80%` threshold and call this a narrow fail, or
  - explicitly version a new threshold before using these numbers operationally.

## Repro

Use the helper below to regenerate the strict-subset summary from the saved artifacts:

```powershell
.\.venv\Scripts\python.exe scripts\e1_summary.py
```
