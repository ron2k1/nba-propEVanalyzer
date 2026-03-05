# Prompt: Staged Backtest (7d → 14d → 30d)

---

Copy and paste the text below into Claude:

---

I'm working on the NBA data ver 2 project — an NBA player-prop EV engine. I need you to run a staged backtest and report results.

**Context:** The project uses Python 3.14 with `.venv`. All commands use `.\.venv\Scripts\python.exe`. See `CLAUDE.md` for full project rules.

**Task:**

1. **Run three backtests** against the current model (full, local data, real closing lines):
   - 7-day window
   - 14-day window  
   - 30-day window

   Use a fixed end date that's before today (no-lookahead). Check `odds_coverage` or the local index first to pick valid dates. Example: end = 2026-02-25, then 7d = Feb 19–25, 14d = Feb 12–25, 30d = Jan 26–Feb 25.

2. **Commands:** Run each with `--model full --local --odds-source local_history --save`.

3. **Confirm** that each saved JSON includes a `modelVersion` (or `modelVersionSummary`) section describing how the model computes projections and EV — for comparing runs when we add more features. If it's missing, add it to `core/nba_backtest.py` and re-run.

4. **Report** a comparison table:
   - realLineSamples
   - realLineHitRatePct
   - roiReal
   - Coverage % (real/total)
   
   For 7-day, 14-day, and 30-day windows. Add a short interpretation: is performance consistent across windows? Any regime change?

---
