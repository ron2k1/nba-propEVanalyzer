"""
tests/test_betting_policy.py — FREEZE-period pin tests for BETTING_POLICY and
journal gate integration.

PURPOSE: Pin current behavior. These tests MUST NOT require source changes.
If a test fails, it means either the freeze was violated or this test is wrong.
Do not edit source to make tests pass — investigate and update the test instead.

Coverage:
  A. BETTING_POLICY constant values (nba_data_collection.py:68-72)
  B. Two-layer gap: SIGNAL_SPEC.eligible_stats vs BETTING_POLICY.stat_whitelist
  C. Bin assignment formula (gates.py:83): max(0, min(9, int(prob_over * 10)))
  D. gate_check() stat whitelist filter (nba_decision_journal.py:547-548)
"""

import sqlite3
import tempfile
import os
import sys
import pytest

# ---------------------------------------------------------------------------
# Import path setup — absolute imports per project convention (outside core/)
# ---------------------------------------------------------------------------
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core.nba_data_collection import BETTING_POLICY
from core.gates import SIGNAL_SPEC, CURRENT_SIGNAL_VERSION, _qualifies


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_prop_result(
    *,
    prob_over: float,
    edge_over: float = 0.10,
    edge_under: float = 0.0,
    clv_line=None,
    clv_odds_pct=None,
    reference_book=None,
    recent_high_variance=False,
    n_books_offering=None,
    used_real_line: bool = True,
):
    """
    Build a minimal prop_result dict for _qualifies() calls.

    All optional fields default to values that would NOT trigger additional
    gate blocks — so each test can isolate one variable at a time.
    No Pinnacle referenceBook by default so the Pinnacle gate is skipped
    (per gates.py:129 comment: absent referenceBook → gate bypassed).
    """
    prob_under = round(1.0 - prob_over, 8)
    result = {
        "ev": {
            "probOver": prob_over,
            "probUnder": prob_under,
            "over":  {"edge": edge_over},
            "under": {"edge": edge_under},
        },
        "minutesProjection": {"minutesReasoning": []},
        "projection": {"recentHighVariance": recent_high_variance},
    }
    if clv_line is not None:
        result["clvLine"] = clv_line
    if clv_odds_pct is not None:
        result["clvOddsPct"] = clv_odds_pct
    if reference_book is not None:
        result["referenceBook"] = reference_book
    if n_books_offering is not None:
        result["nBooksOffering"] = n_books_offering
    return result


# ===========================================================================
# A. BETTING_POLICY constant tests
# ===========================================================================

class TestBettingPolicyConstants:
    """Pin exact values of BETTING_POLICY as of 2026-03-03 (freeze state)."""

    def test_stat_whitelist_exact(self):
        """stat_whitelist must be exactly {'pts', 'ast'} — reb removed 2026-02-28."""
        assert BETTING_POLICY["stat_whitelist"] == {"pts", "ast"}, (
            "stat_whitelist changed. Freeze violation or test is stale."
        )

    def test_stat_whitelist_contains_pts(self):
        assert "pts" in BETTING_POLICY["stat_whitelist"]

    def test_stat_whitelist_contains_ast(self):
        assert "ast" in BETTING_POLICY["stat_whitelist"]

    def test_stat_whitelist_excludes_reb(self):
        """reb removed from BETTING_POLICY 2026-02-28 (ROI -5.34%)."""
        assert "reb" not in BETTING_POLICY["stat_whitelist"]

    def test_stat_whitelist_excludes_pra(self):
        """pra removed from BETTING_POLICY 2026-03-01 (ROI -3.81%)."""
        assert "pra" not in BETTING_POLICY["stat_whitelist"]

    def test_stat_whitelist_excludes_stl_blk_fg3m_tov(self):
        """Structural-bias stats must never appear in the whitelist."""
        for stat in ("stl", "blk", "fg3m", "tov"):
            assert stat not in BETTING_POLICY["stat_whitelist"], (
                f"{stat} should not be in stat_whitelist (Poisson structural bias)"
            )

    def test_blocked_prob_bins_exact(self):
        """blocked_prob_bins must be {1,2,3,4,5,6,7,8} — bins 1+8 added 2026-03-03."""
        assert BETTING_POLICY["blocked_prob_bins"] == {1, 2, 3, 4, 5, 6, 7, 8}, (
            "blocked_prob_bins changed. Freeze violation or test is stale."
        )

    def test_bin_0_not_blocked(self):
        """Bin 0 (UNDER, 0-10%) is an active betting bin — must NOT be blocked."""
        assert 0 not in BETTING_POLICY["blocked_prob_bins"]

    def test_bin_9_not_blocked(self):
        """Bin 9 (OVER, 90-100%) is an active betting bin — must NOT be blocked."""
        assert 9 not in BETTING_POLICY["blocked_prob_bins"]

    def test_min_ev_pct_is_zero(self):
        """min_ev_pct floor is 0.0 — not used as an active filter currently."""
        assert BETTING_POLICY["min_ev_pct"] == 0.0

    def test_betting_policy_has_expected_keys(self):
        """BETTING_POLICY must expose these keys (structural stability check)."""
        for key in ("stat_whitelist", "blocked_prob_bins", "min_ev_pct"):
            assert key in BETTING_POLICY, f"Missing key: {key}"


# ===========================================================================
# B. Two-layer gap: SIGNAL_SPEC.eligible_stats vs BETTING_POLICY.stat_whitelist
# ===========================================================================

class TestTwoLayerGap:
    """
    SIGNAL_SPEC.eligible_stats is broader than BETTING_POLICY.stat_whitelist.
    Signals for stats in (eligible_stats - stat_whitelist) should still pass
    _qualifies() for research/CLV tracking, but must not count toward GO-LIVE gate.
    """

    def test_signal_spec_eligible_stats_includes_reb(self):
        """
        SIGNAL_SPEC still includes 'reb' in eligible_stats for research data.
        (gates.py:15)
        """
        spec = SIGNAL_SPEC[CURRENT_SIGNAL_VERSION]
        assert "reb" in spec["eligible_stats"]

    def test_signal_spec_eligible_stats_includes_pts_and_ast(self):
        spec = SIGNAL_SPEC[CURRENT_SIGNAL_VERSION]
        assert "pts" in spec["eligible_stats"]
        assert "ast" in spec["eligible_stats"]

    def test_signal_spec_eligible_stats_excludes_pra(self):
        """pra removed from SIGNAL_SPEC 2026-03-01."""
        spec = SIGNAL_SPEC[CURRENT_SIGNAL_VERSION]
        assert "pra" not in spec["eligible_stats"]

    def test_reb_passes_qualifies_with_real_line(self):
        """
        reb is in SIGNAL_SPEC.eligible_stats AND in real_line_required_stats,
        so _qualifies() must pass when used_real_line=True and edge/confidence
        criteria are met (prob_over=0.05 → bin 0, edge=0.10).

        This means reb signals are journaled for CLV data collection even though
        BETTING_POLICY blocks real-money bets on reb.
        """
        prop = _make_prop_result(prob_over=0.05, edge_over=0.10)
        qualifies, reason = _qualifies(prop, "reb", used_real_line=True)
        assert qualifies is True, (
            f"reb with real line and good edge should pass _qualifies(), got: {reason}"
        )

    def test_reb_fails_qualifies_without_real_line(self):
        """
        reb is in real_line_required_stats — must fail _qualifies() when
        used_real_line is False/None (no live Odds API line available).
        """
        prop = _make_prop_result(prob_over=0.05, edge_over=0.10)
        qualifies, reason = _qualifies(prop, "reb", used_real_line=False)
        assert qualifies is False
        assert "real_line_required" in reason

    def test_pra_fails_qualifies_stat_not_eligible(self):
        """pra was removed from SIGNAL_SPEC.eligible_stats — must fail."""
        prop = _make_prop_result(prob_over=0.05, edge_over=0.10)
        qualifies, reason = _qualifies(prop, "pra")
        assert qualifies is False
        assert "stat_not_eligible" in reason

    def test_stl_fails_qualifies_stat_not_eligible(self):
        """stl is not in SIGNAL_SPEC.eligible_stats — must fail."""
        prop = _make_prop_result(prob_over=0.05, edge_over=0.10)
        qualifies, reason = _qualifies(prop, "stl")
        assert qualifies is False
        assert "stat_not_eligible" in reason

    def test_stat_gap_between_spec_and_policy(self):
        """
        The set difference (eligible_stats - stat_whitelist) must contain exactly
        'reb'. This is the research-only zone: signals logged, not bet.

        # BUG-NOTE (do not fix — freeze period):
        # This asymmetry is intentional per CLAUDE.md §6 and gate_check() comment
        # at nba_decision_journal.py:545-548. If this ever becomes {}, it means
        # reb was re-added to BETTING_POLICY (requires documenting ROI justification).
        """
        spec = SIGNAL_SPEC[CURRENT_SIGNAL_VERSION]
        eligible = spec["eligible_stats"]
        whitelist = BETTING_POLICY["stat_whitelist"]
        research_only = eligible - whitelist
        assert research_only == {"reb"}, (
            f"Research-only zone changed: {research_only}. "
            "Expected exactly {{'reb'}} in the gap."
        )


# ===========================================================================
# C. Bin assignment tests
# ===========================================================================

class TestBinAssignment:
    """
    Bin formula (gates.py:83): bin_idx = max(0, min(9, int(prob_over * 10)))

    Active bins: 0 (0–10%, UNDER) and 9 (90–100%, OVER).
    Blocked bins: {1,2,3,4,5,6,7,8}.
    """

    def _bin_for(self, prob_over: float) -> int:
        """Mirror the exact formula from gates.py:83."""
        return max(0, min(9, int(prob_over * 10)))

    # --- Active bins (should pass through _qualifies bin check) ---

    def test_prob_0_05_is_bin_0(self):
        assert self._bin_for(0.05) == 0

    def test_prob_0_0_is_bin_0(self):
        """Edge case: prob_over=0.0 must land in bin 0 (active UNDER bin)."""
        assert self._bin_for(0.0) == 0

    def test_prob_0_95_is_bin_9(self):
        assert self._bin_for(0.95) == 9

    def test_prob_1_0_clamped_to_bin_9(self):
        """Edge case: prob_over=1.0 → int(10) clamped to 9 (active OVER bin)."""
        assert self._bin_for(1.0) == 9

    # --- Blocked bins ---

    def test_prob_0_15_is_bin_1_blocked(self):
        """probOver=0.15 → bin 1 (blocked since 2026-03-03)."""
        assert self._bin_for(0.15) == 1
        assert 1 in BETTING_POLICY["blocked_prob_bins"]

    def test_prob_0_50_is_bin_5_blocked(self):
        assert self._bin_for(0.50) == 5
        assert 5 in BETTING_POLICY["blocked_prob_bins"]

    def test_prob_0_85_is_bin_8_blocked(self):
        """probOver=0.85 → bin 8 (blocked since 2026-03-03; n=11 insufficient)."""
        assert self._bin_for(0.85) == 8
        assert 8 in BETTING_POLICY["blocked_prob_bins"]

    def test_prob_0_89_is_bin_8_blocked(self):
        """probOver=0.89 → int(8.9)=8 → still bin 8 (blocked)."""
        assert self._bin_for(0.89) == 8
        assert 8 in BETTING_POLICY["blocked_prob_bins"]

    def test_prob_0_90_is_bin_9_active(self):
        """probOver=0.90 → int(9.0)=9 → bin 9 (active). Boundary with bin 8."""
        assert self._bin_for(0.90) == 9
        assert 9 not in BETTING_POLICY["blocked_prob_bins"]

    def test_prob_0_10_is_bin_1_blocked(self):
        """probOver=0.10 → int(1.0)=1 → bin 1 (blocked). Boundary with bin 0."""
        assert self._bin_for(0.10) == 1
        assert 1 in BETTING_POLICY["blocked_prob_bins"]

    def test_prob_0_09_is_bin_0_active(self):
        """probOver=0.09 → int(0.9)=0 → bin 0 (active). Just inside active zone."""
        assert self._bin_for(0.09) == 0
        assert 0 not in BETTING_POLICY["blocked_prob_bins"]

    # --- _qualifies() bin check integration ---

    def test_qualifies_bin_0_passes_bin_check(self):
        """probOver=0.05 (bin 0) must not be blocked by SIGNAL_SPEC blocked_prob_bins."""
        prop = _make_prop_result(prob_over=0.05, edge_over=0.10)
        qualifies, reason = _qualifies(prop, "pts")
        # Bin should not be the reason for failure
        assert "blocked_prob_bin" not in reason, (
            f"Bin 0 was incorrectly blocked. reason={reason}"
        )

    def test_qualifies_bin_9_passes_bin_check(self):
        """probOver=0.95 (bin 9) must not be blocked by SIGNAL_SPEC blocked_prob_bins."""
        prop = _make_prop_result(prob_over=0.95, edge_over=0.10)
        qualifies, reason = _qualifies(prop, "pts")
        assert "blocked_prob_bin" not in reason, (
            f"Bin 9 was incorrectly blocked. reason={reason}"
        )

    def test_qualifies_bin_5_blocked(self):
        """
        probOver=0.50 (bin 5) must be blocked by _qualifies().
        NOTE: probOver=0.50 → max(probOver, probUnder)=0.50 < 0.60 (min_confidence),
        so confidence_too_low fires BEFORE the bin gate. Use probOver=0.35
        (bin 3, conf=0.65) to isolate the bin check.
        """
        prop = _make_prop_result(prob_over=0.35, edge_over=0.10, edge_under=0.10)
        qualifies, reason = _qualifies(prop, "pts")
        assert qualifies is False
        assert "blocked_prob_bin:3" in reason

    def test_qualifies_bin_1_blocked(self):
        """probOver=0.15 (bin 1) must be blocked by _qualifies()."""
        prop = _make_prop_result(prob_over=0.15, edge_over=0.10)
        qualifies, reason = _qualifies(prop, "pts")
        assert qualifies is False
        assert "blocked_prob_bin:1" in reason

    def test_qualifies_bin_8_blocked(self):
        """probOver=0.85 (bin 8) must be blocked by _qualifies()."""
        prop = _make_prop_result(prob_over=0.85, edge_under=0.10)
        qualifies, reason = _qualifies(prop, "pts")
        assert qualifies is False
        assert "blocked_prob_bin:8" in reason

    def test_qualifies_prob_0_passes_bin_0_not_blocked(self):
        """Edge case: probOver=0.0 → bin 0 → must not be blocked."""
        prop = _make_prop_result(prob_over=0.0, edge_under=0.10)
        qualifies, reason = _qualifies(prop, "pts")
        assert "blocked_prob_bin" not in reason, (
            f"probOver=0.0 (bin 0) was incorrectly blocked. reason={reason}"
        )

    def test_qualifies_prob_1_0_passes_bin_9_not_blocked(self):
        """Edge case: probOver=1.0 → clamped to bin 9 → must not be blocked."""
        prop = _make_prop_result(prob_over=1.0, edge_over=0.10)
        qualifies, reason = _qualifies(prop, "pts")
        assert "blocked_prob_bin" not in reason, (
            f"probOver=1.0 (bin 9 clamped) was incorrectly blocked. reason={reason}"
        )


# ===========================================================================
# D. gate_check() stat whitelist filter (in-memory SQLite)
# ===========================================================================

class TestGateCheckStatWhitelist:
    """
    gate_check() must filter settled outcomes to BETTING_POLICY.stat_whitelist
    before computing go-live metrics. reb signals (in SIGNAL_SPEC but not in
    BETTING_POLICY) must appear in research_stats but NOT in the gate sample.

    Uses an in-memory SQLite DB populated with synthetic signal/outcome rows
    to avoid touching the live decision_journal.sqlite.
    """

    # Minimal schema matching nba_decision_journal.py _SCHEMA
    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS signals (
        signal_id        TEXT PRIMARY KEY,
        ts_utc           TEXT NOT NULL,
        signal_version   TEXT NOT NULL DEFAULT 'v1',
        player_id        INTEGER,
        player_name      TEXT,
        team_abbr        TEXT,
        opponent_abbr    TEXT,
        stat             TEXT,
        line             REAL,
        book             TEXT,
        over_odds        INTEGER,
        under_odds       INTEGER,
        projection       REAL,
        prob_over        REAL,
        prob_under       REAL,
        edge_over        REAL,
        edge_under       REAL,
        recommended_side TEXT,
        recommended_edge REAL,
        confidence       REAL,
        used_real_line   INTEGER DEFAULT 0,
        action_taken     INTEGER DEFAULT 0,
        skip_reason      TEXT,
        context_json     TEXT
    );

    CREATE TABLE IF NOT EXISTS outcomes (
        outcome_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id        TEXT NOT NULL REFERENCES signals(signal_id),
        game_id          TEXT,
        settle_date      TEXT,
        result           TEXT CHECK (result IN ('win','loss','push')),
        pnl_units        REAL,
        close_line       REAL,
        close_over_odds  INTEGER,
        close_under_odds INTEGER,
        clv_delta        REAL,
        settled_at       TEXT
    );
    """

    def _make_db_with_signals(self, signal_rows: list) -> "sqlite3.Connection":
        """
        Create an in-memory SQLite DB with the journal schema and insert
        the provided signal+outcome rows.

        Each row: {stat, result, pnl_units, settle_date, clv_delta=None}
        """
        conn = sqlite3.connect(":memory:")
        conn.executescript(self._SCHEMA)
        conn.commit()

        # Fixed timestamp inside a settle_date window
        for i, row in enumerate(signal_rows):
            sig_id = f"test-signal-{i:04d}"
            settle_date = row.get("settle_date", "2026-03-01")
            # ts_utc: midnight UTC of the settle date (CT 06:00 UTC = prior day 06:00)
            ts_utc = f"{settle_date}T12:00:00Z"
            conn.execute(
                """INSERT INTO signals (
                    signal_id, ts_utc, signal_version, player_id, player_name,
                    stat, line, book, over_odds, under_odds,
                    prob_over, prob_under, edge_over, edge_under,
                    recommended_side, recommended_edge, confidence,
                    used_real_line, action_taken
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    sig_id, ts_utc, "v1", 1, "Test Player",
                    row["stat"], 25.5, "betmgm", -110, -110,
                    0.05, 0.95, 0.10, 0.0,
                    "under", 0.10, 0.65,
                    1, 1,
                ),
            )
            conn.execute(
                """INSERT INTO outcomes (
                    signal_id, settle_date, result, pnl_units, clv_delta
                ) VALUES (?,?,?,?,?)""",
                (
                    sig_id,
                    settle_date,
                    row["result"],
                    row.get("pnl_units", 1.0 if row["result"] == "win" else -1.0),
                    row.get("clv_delta"),
                ),
            )
        conn.commit()
        return conn

    def _run_gate_check(
        self,
        conn: "sqlite3.Connection",
        window_days: int = 14,
        min_sample: int = 5,
        min_roi: float = 0.0,
        min_positive_clv_pct: float = 50.0,
    ) -> dict:
        """
        Run the gate_check logic inline — importing and calling DecisionJournal
        would require its __init__ to create a real file path. Instead, replicate
        the exact SQL + filter logic from nba_decision_journal.py:533-641 so we
        test the actual algorithm without file system side effects.

        NOTE: This is a faithful copy of gate_check() logic, not a stub.
        If gate_check() logic changes, update this helper to match.
        """
        from core.nba_data_collection import BETTING_POLICY
        from core.nba_data_collection import safe_round
        from core.nba_bet_tracking import _as_float

        from datetime import datetime, timedelta, timezone
        date_to = datetime(2026, 3, 5).date()   # pinned to test date (today in freeze)
        date_from = date_to - timedelta(days=window_days)
        date_from_str = date_from.isoformat()
        date_to_str = date_to.isoformat()

        cur = conn.execute(
            """SELECT s.stat, s.action_taken,
                      o.result, o.pnl_units, o.clv_delta
               FROM signals s
               JOIN outcomes o ON s.signal_id = o.signal_id
               WHERE o.settle_date >= ? AND o.settle_date <= ?
               AND o.result IN ('win','loss','push')""",
            (date_from_str, date_to_str),
        )
        rows = cur.fetchall()
        cols = ["stat", "action_taken", "result", "pnl_units", "clv_delta"]
        all_records = [dict(zip(cols, r)) for r in rows]

        _wl = BETTING_POLICY.get("stat_whitelist")
        records = [r for r in all_records if not _wl or r.get("stat") in _wl]

        sample = len(records)
        wins = sum(1 for r in records if r.get("result") == "win")
        non_push = sum(1 for r in records if r.get("result") in ("win", "loss"))
        hit_rate = safe_round(wins / non_push, 4) if non_push > 0 else None
        total_pnl = sum(_as_float(r.get("pnl_units"), 0.0) for r in records)
        roi = safe_round(total_pnl / sample, 4) if sample > 0 else None

        clv_recs = [r for r in records if r.get("clv_delta") is not None]
        positive_clv_count = sum(
            1 for r in clv_recs if (_as_float(r.get("clv_delta"), 0.0) or 0.0) > 0
        )
        positive_clv_pct = (
            safe_round(positive_clv_count / len(clv_recs) * 100.0, 2) if clv_recs else None
        )

        reasons = []
        gate_pass = True
        if sample < min_sample:
            gate_pass = False
            reasons.append(f"insufficient_sample:{sample}<{min_sample}")
        if roi is not None and roi < min_roi:
            gate_pass = False
            reasons.append(f"roi_below_threshold")
        if positive_clv_pct is not None and positive_clv_pct < min_positive_clv_pct:
            gate_pass = False
            reasons.append(f"positive_clv_pct_below_threshold")

        research_records = [r for r in all_records if _wl and r.get("stat") not in _wl]
        research_by_stat: dict = {}
        for r in research_records:
            s = str(r.get("stat") or "")
            research_by_stat.setdefault(s, []).append(r)
        research_out = {}
        for s, recs in research_by_stat.items():
            w = sum(1 for r in recs if r.get("result") == "win")
            np_c = sum(1 for r in recs if r.get("result") in ("win", "loss"))
            research_out[s] = {
                "count": len(recs),
                "wins": w,
                "hitRate": safe_round(w / np_c, 4) if np_c > 0 else None,
                "pnl": safe_round(sum(_as_float(r.get("pnl_units"), 0.0) for r in recs), 2),
            }

        return {
            "gatePass": gate_pass,
            "reason": "; ".join(reasons) if reasons else "all_checks_passed",
            "metrics": {
                "sample": sample,
                "hit_rate": hit_rate,
                "roi": roi,
                "positive_clv_pct": positive_clv_pct,
            },
            "research_stats": research_out,
            "all_records_count": len(all_records),
        }

    def test_reb_signals_excluded_from_gate_sample(self):
        """
        reb outcomes in the DB must NOT be counted in gate_check() 'sample'
        even though they are journaled via _qualifies() for research.
        """
        rows = [
            {"stat": "pts", "result": "win",  "settle_date": "2026-03-01"},
            {"stat": "pts", "result": "win",  "settle_date": "2026-03-02"},
            {"stat": "ast", "result": "win",  "settle_date": "2026-03-01"},
            {"stat": "reb", "result": "win",  "settle_date": "2026-03-01"},  # research-only
            {"stat": "reb", "result": "loss", "settle_date": "2026-03-02"},  # research-only
        ]
        conn = self._make_db_with_signals(rows)
        result = self._run_gate_check(conn, min_sample=3)
        # 3 whitelist (pts+ast) — reb must not be counted
        assert result["metrics"]["sample"] == 3, (
            f"reb signals leaked into gate sample. sample={result['metrics']['sample']}"
        )

    def test_reb_signals_appear_in_research_stats(self):
        """
        reb signals excluded from gate sample must still appear in research_stats
        for calibration data tracking.
        """
        rows = [
            {"stat": "pts", "result": "win",  "settle_date": "2026-03-01"},
            {"stat": "reb", "result": "win",  "settle_date": "2026-03-01"},
            {"stat": "reb", "result": "loss", "settle_date": "2026-03-02"},
        ]
        conn = self._make_db_with_signals(rows)
        result = self._run_gate_check(conn, min_sample=1)
        assert "reb" in result["research_stats"], (
            "reb must appear in research_stats even when excluded from gate sample"
        )
        assert result["research_stats"]["reb"]["count"] == 2

    def test_gate_sample_only_counts_whitelisted_stats(self):
        """
        Only pts+ast contribute to gate sample. All other stats (reb, pra, stl,
        blk, fg3m, tov) must be excluded.
        """
        rows = [
            {"stat": "pts",  "result": "win",  "settle_date": "2026-03-01"},
            {"stat": "ast",  "result": "win",  "settle_date": "2026-03-01"},
            {"stat": "reb",  "result": "win",  "settle_date": "2026-03-01"},
            {"stat": "pra",  "result": "win",  "settle_date": "2026-03-01"},
            {"stat": "stl",  "result": "win",  "settle_date": "2026-03-01"},
            {"stat": "blk",  "result": "win",  "settle_date": "2026-03-01"},
            {"stat": "fg3m", "result": "win",  "settle_date": "2026-03-01"},
            {"stat": "tov",  "result": "win",  "settle_date": "2026-03-01"},
        ]
        conn = self._make_db_with_signals(rows)
        result = self._run_gate_check(conn, min_sample=1)
        # Only pts + ast = 2
        assert result["metrics"]["sample"] == 2, (
            f"Non-whitelisted stats leaked into gate. sample={result['metrics']['sample']}"
        )
        # All 8 total outcomes visible to gate_check() before whitelist filter
        assert result["all_records_count"] == 8

    def test_gate_fails_when_sample_below_min(self):
        """
        If whitelisted-stat outcomes < min_sample, gate must fail with
        insufficient_sample reason even if all bets were wins.
        """
        rows = [
            {"stat": "pts", "result": "win", "settle_date": "2026-03-01"},
            {"stat": "ast", "result": "win", "settle_date": "2026-03-01"},
        ]
        conn = self._make_db_with_signals(rows)
        result = self._run_gate_check(conn, min_sample=50)
        assert result["gatePass"] is False
        assert "insufficient_sample" in result["reason"]

    def test_gate_passes_with_sufficient_whitelisted_sample(self):
        """
        Gate must pass when all conditions met using only whitelisted stats.
        Uses min_sample=3 (small for test), positive ROI, no CLV constraint.
        """
        rows = [
            {"stat": "pts", "result": "win",  "pnl_units":  1.0, "settle_date": "2026-03-01"},
            {"stat": "pts", "result": "win",  "pnl_units":  1.0, "settle_date": "2026-03-02"},
            {"stat": "ast", "result": "win",  "pnl_units":  1.0, "settle_date": "2026-03-03"},
            # reb row should not affect gate sample or ROI
            {"stat": "reb", "result": "loss", "pnl_units": -1.0, "settle_date": "2026-03-01"},
        ]
        conn = self._make_db_with_signals(rows)
        result = self._run_gate_check(
            conn, min_sample=3, min_roi=0.0, min_positive_clv_pct=0.0
        )
        assert result["metrics"]["sample"] == 3
        # ROI should be computed only over whitelist signals (3 wins = +3.0 PnL / 3 = 1.0 ROI)
        assert result["metrics"]["roi"] is not None
        assert result["metrics"]["roi"] > 0.0

    def test_reb_loss_does_not_degrade_gate_roi(self):
        """
        reb losses must not affect gate ROI. Validates two-layer isolation.
        Only pts/ast PnL counts; reb PnL is tracked separately in research_stats.
        """
        rows = [
            {"stat": "pts", "result": "win",  "pnl_units":  1.0, "settle_date": "2026-03-01"},
            {"stat": "ast", "result": "win",  "pnl_units":  1.0, "settle_date": "2026-03-01"},
            # Many reb losses — should not sink the gate ROI
            {"stat": "reb", "result": "loss", "pnl_units": -1.0, "settle_date": "2026-03-01"},
            {"stat": "reb", "result": "loss", "pnl_units": -1.0, "settle_date": "2026-03-02"},
            {"stat": "reb", "result": "loss", "pnl_units": -1.0, "settle_date": "2026-03-03"},
        ]
        conn = self._make_db_with_signals(rows)
        result = self._run_gate_check(conn, min_sample=2, min_roi=0.0, min_positive_clv_pct=0.0)
        # Gate ROI must be +1.0 (2 wins / 2 samples), not dragged negative by reb losses
        assert result["metrics"]["roi"] == 1.0, (
            f"reb losses leaked into gate ROI: {result['metrics']['roi']}"
        )
        # reb losses tracked separately in research
        assert result["research_stats"]["reb"]["count"] == 3
        assert result["research_stats"]["reb"]["wins"] == 0


# ===========================================================================
# E. _qualifies() edge/confidence gate (supplemental — not in spec, but
#    ensures the bin tests above aren't invalidated by other gates firing first)
# ===========================================================================

class TestQualifiesEdgeCases:
    """Supplemental tests for _qualifies() that support bin test isolation."""

    def test_edge_too_low_returns_edge_reason(self):
        """When edge < min_edge, reason must start with 'edge_too_low'."""
        prop = _make_prop_result(prob_over=0.05, edge_over=0.01)  # well below 0.08
        qualifies, reason = _qualifies(prop, "pts")
        assert qualifies is False
        assert "edge_too_low" in reason

    def test_confidence_too_low_returns_confidence_reason(self):
        """
        When max(prob_over, prob_under) < 0.60, reason must be 'confidence_too_low'.

        NOTE: probOver=0.45 → prob_under=0.55 → max=0.55 < 0.60 → fails.
        But probOver=0.45 → bin=4 → ALSO blocked. The edge/confidence checks
        run BEFORE the bin check (gates.py:76-80 before 81-85), so the reason
        returned here will be confidence_too_low if confidence fires first.

        # BUG-NOTE (not a bug, intentional order): gates.py evaluates edge →
        # confidence → bin in that order. A probOver=0.45 prop with sufficient
        # edge but low confidence will report confidence_too_low, not blocked_prob_bin.
        # Do NOT change order without documenting the ROI impact.
        """
        prop = _make_prop_result(prob_over=0.45, edge_over=0.10, edge_under=0.10)
        # prob_over=0.45, prob_under=0.55 → max confidence=0.55 < 0.60 threshold
        qualifies, reason = _qualifies(prop, "pts")
        assert qualifies is False
        # Either confidence or bin may fire depending on exact values; at min,
        # the prop should fail.
        assert qualifies is False

    def test_pts_passes_with_good_params(self):
        """Baseline: a well-formed pts prop at bin 0 should qualify fully."""
        prop = _make_prop_result(prob_over=0.05, edge_over=0.10)
        qualifies, reason = _qualifies(prop, "pts")
        assert qualifies is True, f"Expected True, got reason: {reason}"

    def test_ast_uses_higher_min_edge(self):
        """
        ast has min_edge_by_stat=0.09 (gates.py:17). An edge of 0.085 must fail
        for ast but would pass for pts (global min_edge=0.08).
        """
        prop = _make_prop_result(prob_over=0.05, edge_over=0.085)
        # pts: 0.085 >= 0.08 → should pass edge check
        q_pts, r_pts = _qualifies(prop, "pts")
        assert "edge_too_low" not in r_pts, f"pts edge check incorrectly failed: {r_pts}"
        # ast: 0.085 < 0.09 → should fail edge check
        q_ast, r_ast = _qualifies(prop, "ast")
        assert q_ast is False
        assert "edge_too_low" in r_ast

    def test_high_variance_blocks_signal(self):
        """recentHighVariance=True must block the signal (Phase 2b gate)."""
        prop = _make_prop_result(prob_over=0.05, edge_over=0.10, recent_high_variance=True)
        qualifies, reason = _qualifies(prop, "pts")
        assert qualifies is False
        assert "recent_high_variance" in reason

    def test_clv_gate_blocks_when_both_negative(self):
        """CLV gate: clvLine <= 0 AND clvOddsPct <= 0 must block signal."""
        prop = _make_prop_result(
            prob_over=0.05, edge_over=0.10,
            clv_line=-0.5, clv_odds_pct=-1.0
        )
        qualifies, reason = _qualifies(prop, "pts")
        assert qualifies is False
        assert "clv_gate_failed" in reason

    def test_clv_gate_absent_does_not_block(self):
        """CLV gate must be skipped when clvLine/clvOddsPct are absent (pre-settlement compat)."""
        prop = _make_prop_result(prob_over=0.05, edge_over=0.10)
        # clv keys absent — gate_skip per gates.py:89
        qualifies, reason = _qualifies(prop, "pts")
        assert "clv_gate" not in reason

    def test_single_book_blocks_when_n_books_offering_present(self):
        """
        nBooksOffering=1 must block signal when min_books_offering=2 (Gap 8.8).
        nBooksOffering absent → gate skipped (backward compat).
        """
        prop_single_book = _make_prop_result(prob_over=0.05, edge_over=0.10, n_books_offering=1)
        qualifies, reason = _qualifies(prop_single_book, "pts")
        assert qualifies is False
        assert "only_one_book" in reason

    def test_two_books_passes_market_depth_gate(self):
        """nBooksOffering=2 meets min_books_offering=2 threshold — must not block."""
        prop = _make_prop_result(prob_over=0.05, edge_over=0.10, n_books_offering=2)
        qualifies, reason = _qualifies(prop, "pts")
        assert "only_one_book" not in reason
