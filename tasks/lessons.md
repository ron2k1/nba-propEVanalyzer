# Lessons Learned

## 2026-03-01 — Multi-agent code review false positive rate

**Mistake:** 7-agent review (RepoScout, RiskReviewer, RefactorPlanner, etc.) produced 12 findings.
On user verification against actual source, the CRITICAL finding and both HIGH findings were false positives.
False positive rate: ~7/12 findings wrong or not applicable.

**Root cause:** Agents read snippets and inferred behavior without reading the full function context.
Specific failures:
- Claimed `stat=stat` missing at `backtest:694` — it was present, correctly scoped from outer loop at line 646
- Claimed `_build_reference_probs` read the projection value — it reads `referenceBook.noVigOver/noVigUnder` (book odds only)
- Claimed `used_real_line` was a URL string — it's a bool expression (`x is not None and y is not None`)
- Claimed `bestRecommendation` miss could corrupt journal — already guarded by `if best_ev:` and `if _qs and _best_ev:`

**Rules going forward:**
1. Never trust agent severity ratings on code logic without reading the specific lines and surrounding context yourself
2. Before promoting any finding to CRITICAL or HIGH, read the actual function — not just the line number
3. Multi-agent review is useful for finding *candidate* issues but requires manual verification before acting
4. When agents flag a "missing param" or "wrong type", check `.get()` usage and guard clauses in the same function first
5. False positives on CRITICAL findings are worse than missing real bugs — they waste implementation time and erode trust

**What was actually real:**
- Delete dead `handle_ev_command` shim at `ev_commands.py:381-383` (cosmetic)
- Add `.get("snapshots", {})` guard at `line_commands.py:58` (low-impact safety)
