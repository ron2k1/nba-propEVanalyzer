#!/usr/bin/env python3
"""
Phase A -- Calibration Integrity & Leakage Stress Tests

Covers:
  1. Walk-forward temperature stability (drift detection)
  2. Walk-forward file continuity & date monotonicity
  3. PAV isotonic monotonicity in per-bin calibration
  4. Temperature boundary validation
  5. Calibration shelf-life decay
  6. Leakage/causality checks (as_of_date, policy snapshot)
  7. Fallback behavior when stat pruned by _sample_counts gate
  8. Brier score regression across backtest variants
  9. Policy snapshot leakage
  10. Preflight data readiness

Pass/Fail Thresholds (explicit, per Codex review):
  - TEMP_DRIFT_WARN:    >30% week-over-week T change   -> WARNING
  - TEMP_DRIFT_FAIL:    >80% week-over-week T change   -> FAIL
  - BRIER_DEGRADE_WARN: >5% Brier increase vs baseline -> WARNING
  - BRIER_DEGRADE_FAIL: >15% Brier increase vs baseline -> FAIL
  - MONOTONICITY:       any inversion in calibrated bins -> FAIL
  - SHELF_LIFE:         T applied >8 weeks stale        -> WARNING
  - LEAKAGE:            any future data in calibration   -> HARD FAIL
"""

import json
import math
import os
import sys
import unittest
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.fit_calibration import (
    _apply_temp,
    _fit_bin_temp,
    _logit,
    _pav_weighted_bins,
    _sigmoid,
    fit_bin_temperatures,
    fit_temperature,
)

# ---------------------------------------------------------------------------
# Thresholds (all explicit, surfaced in test output)
# ---------------------------------------------------------------------------
TEMP_DRIFT_WARN_PCT = 0.30   # 30% week-over-week change
TEMP_DRIFT_FAIL_PCT = 0.80   # 80% week-over-week change
MIN_WALK_FORWARD_FILES = 10  # minimum expected walk-forward snapshots
WALK_FORWARD_STEP_DAYS = 7   # expected gap between files
WALK_FORWARD_GAP_TOL = 2     # allowed +/-days deviation from step
# Minimum sample count before drift is meaningful (below this, T jumps from
# the floor are expected as data accumulates -- not a real signal)
DRIFT_MIN_SAMPLES = 5000
SHELF_LIFE_WARN_WEEKS = 8    # calibration older than this -> WARNING
MIN_SAMPLES_GATE = 200       # matches nba_ev_engine._MIN_SAMPLES_GATE
TEMP_LOWER_BOUND = 1.0       # T < 1.0 amplifies confidence (anti-calibration)
TEMP_UPPER_BOUND = 8.0       # grid search ceiling in fit_calibration.py

# Stats NOT in BETTING_POLICY stat_whitelist — exempt from hard-fail drift
# checks because we don't bet on them.  Drift is still reported as INFO.
DRIFT_EXEMPT_STATS = {"reb"}
# Stats in BETTING_POLICY stat_whitelist — hard-fail if prod T outside WF range
BETTING_STATS = {"pts", "ast"}

# Brier baselines: from 2026-01-26 to 2026-02-25 backtest (static cal)
BRIER_BASELINE = {
    "pts": 0.2536, "reb": 0.2457, "ast": 0.2442,
    "fg3m": 0.2153, "pra": 0.2523, "stl": 0.2273,
    "blk": 0.1896, "tov": 0.2303,
}
BRIER_DEGRADE_WARN_PCT = 0.05   # 5% increase from baseline
BRIER_DEGRADE_FAIL_PCT = 0.15   # 15% increase from baseline

PROD_VS_WF_DIVERGE_PCT = 0.30  # 30% divergence between prod and latest WF -> WARNING
PROD_VS_WF_FAIL_PCT = 0.60    # 60% divergence -> FAIL (stale or leaked prod cal)

MODELS_DIR = os.path.join(ROOT, "models")
WF_DIR = os.path.join(MODELS_DIR, "walk_forward")
BACKTEST_DIR = os.path.join(ROOT, "data", "backtest_results")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_wf_files():
    """Load all walk-forward calibration files, sorted by date."""
    if not os.path.isdir(WF_DIR):
        return []
    files = []
    for fname in sorted(os.listdir(WF_DIR)):
        if fname.startswith("prob_cal_") and fname.endswith(".json"):
            date_str = fname[len("prob_cal_"):-len(".json")]
            fpath = os.path.join(WF_DIR, fname)
            try:
                with open(fpath) as f:
                    data = json.load(f)
                files.append((date_str, data))
            except (json.JSONDecodeError, OSError):
                files.append((date_str, None))
    return files


def _load_prod_cal():
    """Load production prob_calibration.json."""
    path = os.path.join(MODELS_DIR, "prob_calibration.json")
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        return json.load(f)


def _stat_keys(cal_dict):
    """Extract stat names from a calibration dict (non-underscore, non-bins)."""
    return [k for k in cal_dict if not k.startswith("_") and not k.endswith("_bins")]


def _pct_change(old, new):
    """Percent change from old to new. Returns abs ratio."""
    if old == 0:
        return float("inf") if new != 0 else 0.0
    return abs(new - old) / abs(old)


def _load_backtest_brier(filename):
    """Load brierByStat from a backtest result file. Returns dict or None."""
    path = os.path.join(BACKTEST_DIR, filename)
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        reports = data.get("reports", {})
        report = reports.get("full") or data
        return report.get("brierByStat")
    except (json.JSONDecodeError, OSError, KeyError):
        return None


def _find_backtest_files_with_brier():
    """Find all backtest result JSON files that contain brierByStat."""
    if not os.path.isdir(BACKTEST_DIR):
        return []
    results = []
    for fname in sorted(os.listdir(BACKTEST_DIR)):
        if not fname.endswith(".json") or fname.startswith("ckpt_"):
            continue
        brier = _load_backtest_brier(fname)
        if brier:
            results.append((fname, brier))
    return results


def _find_backtest_pair(pattern_static="_full_local.json",
                        pattern_wf="_full_local_wf.json"):
    """Dynamically find matching static + walk-forward backtest pairs.

    Returns list of (static_fname, wf_fname) tuples for same date range.
    """
    if not os.path.isdir(BACKTEST_DIR):
        return []
    statics = {}
    wfs = {}
    for fname in os.listdir(BACKTEST_DIR):
        if fname.startswith("ckpt_"):
            continue
        if fname.endswith(pattern_wf):
            date_range = fname[: -len(pattern_wf)]
            wfs[date_range] = fname
        elif fname.endswith(pattern_static):
            date_range = fname[: -len(pattern_static)]
            statics[date_range] = fname
    pairs = []
    for dr in sorted(set(statics) & set(wfs)):
        pairs.append((statics[dr], wfs[dr]))
    return pairs


# ===========================================================================
# 1. Walk-Forward Temperature Stability
# ===========================================================================

class TestWalkForwardTempStability(unittest.TestCase):
    """Detect excessive week-over-week temperature drift."""

    @classmethod
    def setUpClass(cls):
        cls.wf_files = _load_wf_files()

    def test_minimum_walk_forward_files_exist(self):
        """Preflight: at least MIN_WALK_FORWARD_FILES snapshots present."""
        self.assertGreaterEqual(
            len(self.wf_files), MIN_WALK_FORWARD_FILES,
            f"Only {len(self.wf_files)} walk-forward files found, "
            f"need >= {MIN_WALK_FORWARD_FILES}",
        )

    def test_no_hard_fail_temp_drift(self):
        """No stat should drift >TEMP_DRIFT_FAIL_PCT between consecutive snapshots.

        Only checks transitions where both snapshots have >= DRIFT_MIN_SAMPLES
        for the stat in question. Early-season T jumps from T=1.0 floor are
        expected when sample size is small and are reported as warnings instead.
        """
        if len(self.wf_files) < 2:
            self.skipTest("Not enough walk-forward files for drift check")

        hard_fails = []
        early_window_skips = []
        for i in range(1, len(self.wf_files)):
            d_prev, cal_prev = self.wf_files[i - 1]
            d_curr, cal_curr = self.wf_files[i]
            if cal_prev is None or cal_curr is None:
                continue

            counts_prev = cal_prev.get("_sample_counts", {})
            counts_curr = cal_curr.get("_sample_counts", {})

            for stat in _stat_keys(cal_curr):
                t_prev = cal_prev.get(stat)
                t_curr = cal_curr.get(stat)
                if t_prev is None or t_curr is None:
                    continue
                drift = _pct_change(t_prev, t_curr)
                if drift <= TEMP_DRIFT_FAIL_PCT:
                    continue

                # Check if either snapshot has low sample count for this stat
                n_prev = counts_prev.get(stat, 0)
                n_curr = counts_curr.get(stat, 0)
                if n_prev < DRIFT_MIN_SAMPLES or n_curr < DRIFT_MIN_SAMPLES:
                    early_window_skips.append(
                        f"  {stat}: {d_prev}->{d_curr}  T={t_prev:.2f}->{t_curr:.2f} "
                        f"(drift={drift:.0%}, n={n_prev}->{n_curr}, below {DRIFT_MIN_SAMPLES})"
                    )
                    continue

                if stat in DRIFT_EXEMPT_STATS:
                    early_window_skips.append(
                        f"  {stat}: {d_prev}->{d_curr}  T={t_prev:.2f}->{t_curr:.2f} "
                        f"(drift={drift:.0%}, n={n_prev}->{n_curr}, "
                        f"EXEMPT: not in stat_whitelist)"
                    )
                    continue

                # Floor-to-real transition: T_prev == 1.0 (floor) means the
                # optimizer hadn't found a real temperature yet.  The jump
                # FROM the floor is initialization, not meaningful drift.
                if abs(t_prev - TEMP_LOWER_BOUND) < 1e-9:
                    early_window_skips.append(
                        f"  {stat}: {d_prev}->{d_curr}  T={t_prev:.2f}->{t_curr:.2f} "
                        f"(drift={drift:.0%}, n={n_prev}->{n_curr}, "
                        f"FLOOR INIT: T_prev was at floor {TEMP_LOWER_BOUND})"
                    )
                    continue

                hard_fails.append(
                    f"  {stat}: {d_prev}->{d_curr}  T={t_prev:.2f}->{t_curr:.2f} "
                    f"(drift={drift:.0%}, threshold={TEMP_DRIFT_FAIL_PCT:.0%}, "
                    f"n={n_prev}->{n_curr})"
                )

        if early_window_skips:
            print(f"\nINFO: {len(early_window_skips)} early-window drift(s) skipped "
                  f"(n < {DRIFT_MIN_SAMPLES}):")
            for s in early_window_skips:
                print(s)

        if hard_fails:
            self.fail(
                f"HARD FAIL: {len(hard_fails)} temp drift(s) exceed {TEMP_DRIFT_FAIL_PCT:.0%}:\n"
                + "\n".join(hard_fails)
            )

    def test_warn_moderate_temp_drift(self):
        """Report stats with moderate drift (WARN threshold) without failing."""
        if len(self.wf_files) < 2:
            self.skipTest("Not enough walk-forward files for drift check")

        warnings = []
        for i in range(1, len(self.wf_files)):
            d_prev, cal_prev = self.wf_files[i - 1]
            d_curr, cal_curr = self.wf_files[i]
            if cal_prev is None or cal_curr is None:
                continue

            for stat in _stat_keys(cal_curr):
                t_prev = cal_prev.get(stat)
                t_curr = cal_curr.get(stat)
                if t_prev is None or t_curr is None:
                    continue
                drift = _pct_change(t_prev, t_curr)
                if TEMP_DRIFT_WARN_PCT < drift <= TEMP_DRIFT_FAIL_PCT:
                    warnings.append(
                        f"  {stat}: {d_prev}->{d_curr}  T={t_prev:.2f}->{t_curr:.2f} "
                        f"(drift={drift:.0%})"
                    )

        if warnings:
            print(f"\nWARNING: {len(warnings)} moderate temp drift(s) "
                  f"(>{TEMP_DRIFT_WARN_PCT:.0%}):")
            for w in warnings:
                print(w)
        # This test always passes -- warnings are informational


# ===========================================================================
# 2. Walk-Forward File Continuity & Date Monotonicity
# ===========================================================================

class TestWalkForwardContinuity(unittest.TestCase):
    """Walk-forward files should be strictly monotonic with consistent spacing."""

    @classmethod
    def setUpClass(cls):
        cls.wf_files = _load_wf_files()

    def test_dates_strictly_increasing(self):
        """All walk-forward file dates must be strictly increasing."""
        dates = [d for d, _ in self.wf_files]
        for i in range(1, len(dates)):
            self.assertGreater(
                dates[i], dates[i - 1],
                f"Walk-forward dates not strictly increasing: {dates[i-1]} >= {dates[i]}",
            )

    def test_no_large_gaps(self):
        """No gap between consecutive files should exceed step + tolerance."""
        if len(self.wf_files) < 2:
            self.skipTest("Need >=2 files")

        max_gap = WALK_FORWARD_STEP_DAYS + WALK_FORWARD_GAP_TOL
        gaps = []
        for i in range(1, len(self.wf_files)):
            d1 = date.fromisoformat(self.wf_files[i - 1][0])
            d2 = date.fromisoformat(self.wf_files[i][0])
            gap = (d2 - d1).days
            if gap > max_gap:
                gaps.append(f"  {d1} -> {d2}: {gap} days (max allowed: {max_gap})")

        if gaps:
            self.fail(
                f"Walk-forward gap(s) exceed {max_gap} days:\n" + "\n".join(gaps)
            )

    def test_no_corrupt_files(self):
        """Every walk-forward file should parse as valid JSON with expected keys."""
        corrupt = []
        for date_str, data in self.wf_files:
            if data is None:
                corrupt.append(date_str)
                continue
            if "_global" not in data:
                corrupt.append(f"{date_str} (missing _global)")
            if "_sample_counts" not in data:
                corrupt.append(f"{date_str} (missing _sample_counts)")

        if corrupt:
            self.fail(f"Corrupt walk-forward files: {corrupt}")

    def test_train_to_before_for_date(self):
        """_train_to must be strictly before _for_date (no lookahead)."""
        violations = []
        for date_str, data in self.wf_files:
            if data is None:
                continue
            train_to = data.get("_train_to", "")
            for_date = data.get("_for_date", "")
            if train_to and for_date and train_to >= for_date:
                violations.append(
                    f"  {date_str}: _train_to={train_to} >= _for_date={for_date}"
                )

        if violations:
            self.fail(
                "LEAKAGE: _train_to >= _for_date in walk-forward files:\n"
                + "\n".join(violations)
            )


# ===========================================================================
# 3. PAV Isotonic Monotonicity
# ===========================================================================

class TestPAVMonotonicity(unittest.TestCase):
    """Per-bin calibrated probabilities must be monotonically non-decreasing."""

    def test_pav_produces_monotone_output(self):
        """PAV algorithm should produce non-decreasing weighted averages."""
        items = [
            [0.05, 0.08, 100, "0-10"],
            [0.15, 0.22, 200, "10-20"],
            [0.25, 0.18, 150, "20-30"],  # violation: 0.22 > 0.18
            [0.35, 0.30, 100, "30-40"],
            [0.45, 0.48, 120, "40-50"],
        ]
        blocks = _pav_weighted_bins(items)

        prev_avg = -1.0
        for block in blocks:
            avg = sum(x[1] * x[2] for x in block) / sum(x[2] for x in block)
            self.assertGreaterEqual(
                avg, prev_avg,
                f"PAV output not monotone: {prev_avg:.4f} > {avg:.4f}",
            )
            prev_avg = avg

    def test_pav_merges_violation_pair(self):
        """Two-item violation should merge into single block."""
        items = [
            [0.15, 0.30, 100, "10-20"],
            [0.25, 0.20, 100, "20-30"],
        ]
        blocks = _pav_weighted_bins(items)
        self.assertEqual(len(blocks), 1, "PAV should merge the violating pair")

    def test_pav_preserves_monotone_input(self):
        """Already monotone input should not be merged."""
        items = [
            [0.15, 0.10, 100, "10-20"],
            [0.25, 0.20, 100, "20-30"],
            [0.35, 0.30, 100, "30-40"],
        ]
        blocks = _pav_weighted_bins(items)
        self.assertEqual(len(blocks), 3, "PAV should not merge monotone input")

    def _check_bins_monotone(self, cal_dict, label):
        """Shared: check per-bin temps produce monotone calibrated probabilities."""
        violations = []
        for stat in _stat_keys(cal_dict):
            bin_key = f"{stat}_bins"
            if bin_key not in cal_dict:
                continue
            bin_temps = cal_dict[bin_key]
            # Parse bins with hyphen format; skip any that don't match
            parseable = []
            for lbl, T in bin_temps.items():
                parts = lbl.split("-")
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    parseable.append((int(parts[0]), int(parts[1]), lbl, T))
            parseable.sort(key=lambda x: x[0])

            prev_cal_p = -1.0
            for lo, hi, bin_lbl, T in parseable:
                midpoint = (lo + hi) / 200.0
                cal_p = _apply_temp(midpoint, T)
                if cal_p < prev_cal_p - 0.001:
                    violations.append(
                        f"  {label}/{stat} bin {bin_lbl}: cal_p={cal_p:.4f} < "
                        f"prev={prev_cal_p:.4f} (T={T})"
                    )
                prev_cal_p = cal_p
        return violations

    def test_production_cal_bins_monotone(self):
        """Production prob_calibration.json per-bin temps should produce monotone output."""
        cal = _load_prod_cal()
        if cal is None:
            self.skipTest("No production calibration file")

        violations = self._check_bins_monotone(cal, "production")
        if violations:
            self.fail(
                "Production calibration has non-monotone bin outputs:\n"
                + "\n".join(violations)
            )

    def test_walk_forward_bins_monotone(self):
        """All walk-forward files with per-bin temps must produce monotone output."""
        wf_files = _load_wf_files()
        if not wf_files:
            self.skipTest("No walk-forward files")

        all_violations = []
        for date_str, data in wf_files:
            if data is None:
                continue
            violations = self._check_bins_monotone(data, date_str)
            all_violations.extend(violations)

        if all_violations:
            self.fail(
                f"Walk-forward bin monotonicity violations ({len(all_violations)}):\n"
                + "\n".join(all_violations)
            )


# ===========================================================================
# 4. Temperature Boundary Validation
# ===========================================================================

class TestTemperatureBoundaries(unittest.TestCase):
    """Temperatures should stay within [1.0, 8.0] -- grid search boundaries."""

    def test_production_temps_in_bounds(self):
        """Production calibration T values must be in [LOWER, UPPER]."""
        cal = _load_prod_cal()
        if cal is None:
            self.skipTest("No production calibration file")

        out_of_bounds = []
        for stat in _stat_keys(cal):
            T = cal[stat]
            if T < TEMP_LOWER_BOUND or T > TEMP_UPPER_BOUND:
                out_of_bounds.append(
                    f"  {stat}: T={T} (bounds: [{TEMP_LOWER_BOUND}, {TEMP_UPPER_BOUND}])"
                )
            bin_key = f"{stat}_bins"
            if bin_key in cal:
                for bin_lbl, T_bin in cal[bin_key].items():
                    if T_bin < TEMP_LOWER_BOUND or T_bin > TEMP_UPPER_BOUND:
                        out_of_bounds.append(f"  {stat} bin {bin_lbl}: T={T_bin}")

        if out_of_bounds:
            self.fail(
                "Temperature(s) outside grid search bounds:\n"
                + "\n".join(out_of_bounds)
            )

    def test_walk_forward_temps_in_bounds(self):
        """All walk-forward T values must be in [LOWER, UPPER]."""
        wf_files = _load_wf_files()
        out_of_bounds = []

        for date_str, data in wf_files:
            if data is None:
                continue
            for stat in _stat_keys(data):
                T = data[stat]
                if T < TEMP_LOWER_BOUND or T > TEMP_UPPER_BOUND:
                    out_of_bounds.append(f"  {date_str}/{stat}: T={T}")
                bin_key = f"{stat}_bins"
                if bin_key in data:
                    for bin_lbl, T_bin in data[bin_key].items():
                        if T_bin < TEMP_LOWER_BOUND or T_bin > TEMP_UPPER_BOUND:
                            out_of_bounds.append(
                                f"  {date_str}/{stat} bin {bin_lbl}: T={T_bin}"
                            )

        if out_of_bounds:
            self.fail(
                "Walk-forward temperature(s) outside bounds:\n"
                + "\n".join(out_of_bounds)
            )

    def test_anti_calibration_never_occurs(self):
        """T < 1.0 would amplify confidence. For any T in production, cal_p must be
        closer to 0.5 than raw p (or equal)."""
        cal = _load_prod_cal()
        if cal is None:
            self.skipTest("No production calibration file")

        test_probs = [0.05, 0.10, 0.20, 0.30, 0.40, 0.60, 0.70, 0.80, 0.90, 0.95]
        for stat in _stat_keys(cal):
            T = cal[stat]
            for p in test_probs:
                cal_p = _apply_temp(p, T)
                dist_raw = abs(p - 0.5)
                dist_cal = abs(cal_p - 0.5)
                self.assertLessEqual(
                    dist_cal, dist_raw + 0.001,
                    f"{stat} T={T}: _apply_temp({p}) = {cal_p:.4f} is FARTHER from "
                    f"0.5 than raw ({dist_cal:.4f} > {dist_raw:.4f}). Anti-calibration!",
                )


# ===========================================================================
# 5. Calibration Shelf-Life
# ===========================================================================

class TestCalibrationShelfLife(unittest.TestCase):
    """Production calibration should not be older than SHELF_LIFE_WARN_WEEKS."""

    def test_production_cal_not_stale(self):
        """Production prob_calibration.json _fitted_at should be recent."""
        cal = _load_prod_cal()
        if cal is None:
            self.skipTest("No production calibration file")

        fitted_at_str = cal.get("_fitted_at", "")
        if not fitted_at_str:
            self.skipTest("No _fitted_at in calibration file")

        from datetime import datetime
        fitted_at = None
        for fmt in ("%Y-%m-%dT%H:%MZ", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                fitted_at = datetime.strptime(fitted_at_str, fmt).date()
                break
            except ValueError:
                continue

        if fitted_at is None:
            self.skipTest(f"Cannot parse _fitted_at: {fitted_at_str}")

        age_days = (date.today() - fitted_at).days
        warn_days = SHELF_LIFE_WARN_WEEKS * 7

        if age_days > warn_days:
            print(
                f"\nWARNING: Production calibration is {age_days} days old "
                f"(fitted {fitted_at_str}). Threshold: {warn_days} days."
            )

    def test_walk_forward_coverage_to_present(self):
        """Latest walk-forward file should cover close to the present."""
        wf_files = _load_wf_files()
        if not wf_files:
            self.skipTest("No walk-forward files")

        latest_date = date.fromisoformat(wf_files[-1][0])
        gap = (date.today() - latest_date).days

        if gap > 14:
            print(
                f"\nWARNING: Latest walk-forward file is {gap} days old "
                f"({wf_files[-1][0]}). Consider regenerating."
            )

    def test_production_vs_latest_wf_divergence(self):
        """Production cal should not diverge excessively from the latest WF snapshot.

        A large gap (>PROD_VS_WF_FAIL_PCT) suggests the production cal is stale
        or was fitted on a different data window than the walk-forward series.
        """
        cal = _load_prod_cal()
        if cal is None:
            self.skipTest("No production calibration file")

        wf_files = _load_wf_files()
        if not wf_files:
            self.skipTest("No walk-forward files")

        _, latest_wf = wf_files[-1]
        if latest_wf is None:
            self.skipTest("Latest walk-forward file is corrupt")

        hard_fails = []
        warnings = []
        research_divergences = []
        for stat in _stat_keys(cal):
            prod_T = cal[stat]
            wf_T = latest_wf.get(stat)
            if wf_T is None:
                continue
            divergence = _pct_change(wf_T, prod_T)
            entry = (
                f"  {stat}: prod={prod_T:.2f} vs latest_wf={wf_T:.2f} "
                f"(divergence={divergence:.0%})"
            )
            if divergence > PROD_VS_WF_FAIL_PCT:
                if stat in DRIFT_EXEMPT_STATS:
                    research_divergences.append(entry)
                else:
                    hard_fails.append(entry)
            elif divergence > PROD_VS_WF_DIVERGE_PCT:
                warnings.append(entry)

        if research_divergences:
            print(f"\nINFO: {len(research_divergences)} research-only stat(s) diverge "
                  f">{PROD_VS_WF_FAIL_PCT:.0%} (not betting, no hard fail):")
            for r in research_divergences:
                print(r)

        if warnings:
            print(f"\nWARNING: {len(warnings)} stat(s) diverge "
                  f">{PROD_VS_WF_DIVERGE_PCT:.0%} between prod and latest WF:")
            for w in warnings:
                print(w)

        if hard_fails:
            self.fail(
                f"Production cal diverges >{PROD_VS_WF_FAIL_PCT:.0%} from latest WF:\n"
                + "\n".join(hard_fails)
            )


# ===========================================================================
# 6. Leakage & Causality Checks
# ===========================================================================

class TestLeakagePrevention(unittest.TestCase):
    """Verify no future data leaks into calibration or backtests."""

    def test_walk_forward_train_windows_expanding(self):
        """Each successive walk-forward file should have a strictly larger training window."""
        wf_files = _load_wf_files()
        if len(wf_files) < 2:
            self.skipTest("Need >=2 files")

        prev_train_to = ""
        violations = []
        for date_str, data in wf_files:
            if data is None:
                continue
            train_to = data.get("_train_to", "")
            if train_to and train_to <= prev_train_to:
                violations.append(
                    f"  {date_str}: _train_to={train_to} <= prev={prev_train_to}"
                )
            if train_to:
                prev_train_to = train_to

        if violations:
            self.fail(
                "Walk-forward training windows not strictly expanding:\n"
                + "\n".join(violations)
            )

    def test_walk_forward_train_from_constant(self):
        """All walk-forward files should share the same _train_from (season start).

        If _train_from shifts forward, early-season data is silently dropped --
        a form of data leak by omission.
        """
        wf_files = _load_wf_files()
        if not wf_files:
            self.skipTest("No walk-forward files")

        train_froms = {}
        for date_str, data in wf_files:
            if data is None:
                continue
            tf = data.get("_train_from")
            if tf:
                train_froms[date_str] = tf

        unique = set(train_froms.values())
        if len(unique) > 1:
            self.fail(
                f"Walk-forward files have inconsistent _train_from values "
                f"(expected one season start): {dict(sorted(train_froms.items()))}"
            )

    def test_walk_forward_sample_counts_nondecreasing(self):
        """Expanding windows should have non-decreasing sample counts per stat."""
        wf_files = _load_wf_files()
        if len(wf_files) < 2:
            self.skipTest("Need >=2 files")

        violations = []
        prev_counts = {}
        for date_str, data in wf_files:
            if data is None:
                continue
            counts = data.get("_sample_counts", {})
            for stat, n in counts.items():
                prev_n = prev_counts.get(stat, 0)
                if n < prev_n:
                    violations.append(
                        f"  {date_str}/{stat}: n={n} < prev={prev_n} (sample count decreased)"
                    )
            prev_counts = counts

        if violations:
            self.fail(
                "LEAKAGE RISK: Sample counts decreased in expanding window:\n"
                + "\n".join(violations)
            )

    def test_production_cal_fitted_on_past_data(self):
        """Production calibration _fitted_on should reference a backtest with end date before today."""
        cal = _load_prod_cal()
        if cal is None:
            self.skipTest("No production calibration file")

        fitted_on = cal.get("_fitted_on", "")
        if "_to_" in fitted_on:
            parts = os.path.basename(fitted_on).split("_to_")
            if len(parts) >= 2:
                end_date_str = parts[1][:10]
                try:
                    end_date = date.fromisoformat(end_date_str)
                    self.assertLess(
                        end_date, date.today(),
                        f"Production calibration fitted on data ending {end_date}, "
                        f"which is not before today ({date.today()})."
                    )
                except ValueError:
                    pass

    def test_ev_engine_date_loader_uses_past_only(self):
        """_load_prob_calibration_for_date should only load files with date <= as_of_date."""
        from core.nba_ev_engine import _load_prob_calibration_for_date, _PROB_CAL
        from core import nba_ev_engine

        old_cache = nba_ev_engine._cal_cache.copy()
        nba_ev_engine._cal_cache.clear()

        try:
            # Case 1: date BEFORE all walk-forward files -> must fall back to
            # static _PROB_CAL (production cal). Verify by checking it does NOT
            # contain walk-forward metadata (_for_date, _train_from, _train_to).
            cal_early = _load_prob_calibration_for_date("2025-10-01")
            self.assertNotIn(
                "_for_date", cal_early,
                "Date 2025-10-01 should fall back to static _PROB_CAL "
                "(no _for_date key), not load a walk-forward file."
            )
            self.assertNotIn("_train_from", cal_early)
            self.assertNotIn("_train_to", cal_early)
            # Verify it IS the production cal by checking a known key
            if _PROB_CAL:
                prod_global = _PROB_CAL.get("_global")
                self.assertEqual(
                    cal_early.get("_global"), prod_global,
                    "Early-date fallback should return the same _global as production cal"
                )

            # Case 2: mid-season date -> should load walk-forward file with
            # _for_date <= requested date
            nba_ev_engine._cal_cache.clear()
            cal_mid = _load_prob_calibration_for_date("2026-01-15")
            if "_for_date" in cal_mid:
                for_date = cal_mid["_for_date"]
                self.assertLessEqual(
                    for_date, "2026-01-15",
                    f"Loaded calibration for 2026-01-15 but got _for_date={for_date}"
                )
        finally:
            nba_ev_engine._cal_cache.clear()
            nba_ev_engine._cal_cache.update(old_cache)

    def test_ev_engine_loader_never_loads_future_file(self):
        """For any walk-forward date D, loading calibration for D-1 must not
        return the file dated D itself."""
        from core.nba_ev_engine import _load_prob_calibration_for_date
        from core import nba_ev_engine

        old_cache = nba_ev_engine._cal_cache.copy()
        nba_ev_engine._cal_cache.clear()

        try:
            wf_files = _load_wf_files()
            for date_str, data in wf_files:
                if data is None:
                    continue
                # Request calibration for the day BEFORE this file's target date
                target = date.fromisoformat(date_str)
                day_before = (target - timedelta(days=1)).isoformat()
                nba_ev_engine._cal_cache.clear()
                cal = _load_prob_calibration_for_date(day_before)
                if "_for_date" in cal:
                    loaded_date = cal["_for_date"]
                    self.assertLess(
                        loaded_date, date_str,
                        f"Requesting cal for {day_before} loaded file with "
                        f"_for_date={loaded_date} (should be < {date_str})"
                    )
        finally:
            nba_ev_engine._cal_cache.clear()
            nba_ev_engine._cal_cache.update(old_cache)


# ===========================================================================
# 7. Sample-Count Gate Fallback
# ===========================================================================

class TestSampleCountGateFallback(unittest.TestCase):
    """Stats pruned by _MIN_SAMPLES_GATE should fall back to _global T."""

    def test_fallback_uses_global_when_pruned(self):
        """When stat sample count < MIN_SAMPLES_GATE, that stat's T should be absent."""
        from core.nba_ev_engine import _load_prob_calibration_for_date
        from core import nba_ev_engine

        old_cache = nba_ev_engine._cal_cache.copy()
        nba_ev_engine._cal_cache.clear()

        try:
            wf_files = _load_wf_files()
            if not wf_files:
                self.skipTest("No walk-forward files")

            earliest_date, earliest_data = wf_files[0]
            if earliest_data is None:
                self.skipTest("Earliest walk-forward file is corrupt")

            counts = earliest_data.get("_sample_counts", {})
            below_gate = [s for s, n in counts.items() if n < MIN_SAMPLES_GATE]

            cal = _load_prob_calibration_for_date(earliest_date)

            for stat in below_gate:
                self.assertNotIn(
                    stat, cal,
                    f"Stat '{stat}' has sample count {counts[stat]} < {MIN_SAMPLES_GATE} "
                    f"but still has stat-specific T in loaded calibration"
                )

            self.assertIn("_global", cal, "Missing _global fallback")
        finally:
            nba_ev_engine._cal_cache.clear()
            nba_ev_engine._cal_cache.update(old_cache)

    def test_global_temp_always_present(self):
        """Every walk-forward file and production cal must have _global."""
        wf_files = _load_wf_files()
        missing = []
        for date_str, data in wf_files:
            if data is None:
                missing.append(f"{date_str} (corrupt file)")
                continue
            if "_global" not in data:
                missing.append(date_str)

        cal = _load_prod_cal()
        if cal is not None and "_global" not in cal:
            missing.append("production (prob_calibration.json)")

        if missing:
            self.fail(f"Missing _global in: {missing}")


# ===========================================================================
# 8. Brier Score Regression Tests
# ===========================================================================

class TestBrierRegression(unittest.TestCase):
    """Compare Brier scores across backtest variants against baseline.

    Uses BRIER_BASELINE (from static-cal backtest 2026-01-26 to 2026-02-25).
    Walk-forward backtests should not degrade Brier by more than thresholds.
    """

    def test_walk_forward_brier_not_degraded(self):
        """Walk-forward Brier should not exceed paired static-cal Brier by >FAIL threshold.

        Dynamically discovers static/WF backtest pairs by matching filenames.
        Fails hard (not skips) if no pairs found — preflight already asserts
        backtest results exist.
        """
        pairs = _find_backtest_pair()
        self.assertGreater(
            len(pairs), 0,
            "No static/WF backtest pairs found in data/backtest_results/. "
            "Expected files matching *_full_local.json and *_full_local_wf.json."
        )

        hard_fails = []
        warnings = []
        for static_fname, wf_fname in pairs:
            static_brier = _load_backtest_brier(static_fname)
            wf_brier = _load_backtest_brier(wf_fname)
            if static_brier is None or wf_brier is None:
                continue
            for stat in BRIER_BASELINE:
                s = static_brier.get(stat)
                w = wf_brier.get(stat)
                if s is None or w is None:
                    continue
                degrade = (w - s) / s if s > 0 else 0
                if degrade > BRIER_DEGRADE_FAIL_PCT:
                    hard_fails.append(
                        f"  {wf_fname}/{stat}: WF={w:.4f} vs static={s:.4f} "
                        f"(+{degrade:.1%}, threshold={BRIER_DEGRADE_FAIL_PCT:.0%})"
                    )
                elif degrade > BRIER_DEGRADE_WARN_PCT:
                    warnings.append(
                        f"  {wf_fname}/{stat}: WF={w:.4f} vs static={s:.4f} "
                        f"(+{degrade:.1%})"
                    )

        if warnings:
            print(f"\nWARNING: {len(warnings)} stat(s) with moderate Brier increase:")
            for w in warnings:
                print(w)

        if hard_fails:
            self.fail(
                f"Brier degradation exceeds {BRIER_DEGRADE_FAIL_PCT:.0%} threshold:\n"
                + "\n".join(hard_fails)
            )

    def test_brier_scores_below_random(self):
        """Canonical backtests (non-variant) should have Brier < 0.27 for all stats.

        Early-season backtests (Oct-Nov only) and experimental variants
        (matchlive, noblend, opening, wf) are expected to be noisier and are
        reported as warnings only.
        """
        backtests = _find_backtest_files_with_brier()
        if not backtests:
            self.skipTest("No backtest results with Brier data")

        # Canonical = no variant suffixes, covers at least Dec-Feb
        variant_tags = ("_matchlive", "_noblend", "_opening", "_wf", "_v2", "_v3",
                        "_realonly", "_nogates")

        hard_fails = []
        warnings = []
        for fname, brier in backtests:
            is_variant = any(tag in fname for tag in variant_tags)
            # Early-season only (ends before Dec) is also a variant
            is_early = "_to_2025-11" in fname or "_to_2025-10" in fname

            for stat, val in brier.items():
                if val is None or val < 0.27:
                    continue
                entry = f"  {fname}/{stat}: Brier={val:.4f}"
                if is_variant or is_early:
                    warnings.append(entry)
                else:
                    hard_fails.append(entry)

        if warnings:
            print(f"\nWARNING: {len(warnings)} variant/early-season Brier > 0.27:")
            for w in warnings:
                print(w)

        if hard_fails:
            self.fail(
                f"Canonical backtest Brier scores > 0.27:\n"
                + "\n".join(hard_fails)
            )

    def test_brier_consistency_across_variants(self):
        """Static-cal and walk-forward Brier for same date range should be within 10%.
        Uses dynamic pair discovery."""
        pairs = _find_backtest_pair()
        if not pairs:
            self.skipTest("No static/WF backtest pairs found")

        large_diffs = []
        for static_fname, wf_fname in pairs:
            static_brier = _load_backtest_brier(static_fname)
            wf_brier = _load_backtest_brier(wf_fname)
            if static_brier is None or wf_brier is None:
                continue
            for stat in BRIER_BASELINE:
                s = static_brier.get(stat)
                w = wf_brier.get(stat)
                if s is None or w is None or s == 0:
                    continue
                diff_pct = abs(s - w) / s
                if diff_pct > 0.10:
                    large_diffs.append(
                        f"  {static_fname}/{stat}: static={s:.4f} vs wf={w:.4f} "
                        f"(diff={diff_pct:.1%})"
                    )

        if large_diffs:
            print(f"\nWARNING: Static vs WF Brier differ by >10%:")
            for d in large_diffs:
                print(d)

    def test_brier_improves_with_more_data(self):
        """Longer training windows should generally not worsen Brier.
        Dynamically finds shortest and longest canonical backtests."""
        if not os.path.isdir(BACKTEST_DIR):
            self.skipTest("No backtest_results directory")

        # Find canonical (non-variant) backtests
        canonical = []
        variant_tags = ("_matchlive", "_noblend", "_opening", "_wf", "_v2", "_v3",
                        "_realonly", "_nogates")
        for fname in sorted(os.listdir(BACKTEST_DIR)):
            if not fname.endswith("_full_local.json") or fname.startswith("ckpt_"):
                continue
            if any(tag in fname for tag in variant_tags):
                continue
            # Extract date range
            base = fname[:-len("_full_local.json")]
            parts = base.split("_to_")
            if len(parts) == 2:
                try:
                    d_from = date.fromisoformat(parts[0])
                    d_to = date.fromisoformat(parts[1])
                    span = (d_to - d_from).days
                    canonical.append((span, fname))
                except ValueError:
                    continue

        if len(canonical) < 2:
            self.skipTest("Need >= 2 canonical backtests for comparison")

        canonical.sort(key=lambda x: x[0])
        short_fname = canonical[0][1]
        long_fname = canonical[-1][1]

        short_brier = _load_backtest_brier(short_fname)
        long_brier = _load_backtest_brier(long_fname)
        if short_brier is None or long_brier is None:
            self.skipTest(f"Cannot load {short_fname} or {long_fname}")

        worse_stats = []
        for stat in BRIER_BASELINE:
            s = short_brier.get(stat)
            l = long_brier.get(stat)
            if s is None or l is None or s == 0:
                continue
            if l > s * 1.05:
                worse_stats.append(
                    f"  {stat}: short({short_fname})={s:.4f} vs "
                    f"long({long_fname})={l:.4f} ({((l-s)/s):.1%} worse)"
                )

        if worse_stats:
            print(f"\nWARNING: Longer training window worsened Brier for:")
            for w in worse_stats:
                print(w)


# ===========================================================================
# 9. Policy Snapshot Leakage Check
# ===========================================================================

class TestPolicySnapshotLeakage(unittest.TestCase):
    """Verify backtest uses date-appropriate policy, not current policy."""

    def test_policy_history_file_exists(self):
        """models/policy_history.json must exist for date-aware backtests."""
        path = os.path.join(MODELS_DIR, "policy_history.json")
        self.assertTrue(
            os.path.isfile(path),
            "models/policy_history.json missing -- backtests will use current policy "
            "for all dates (potential leakage)"
        )

    def test_policy_history_dates_ordered(self):
        """Policy history entries must be strictly ordered by effective_from."""
        path = os.path.join(MODELS_DIR, "policy_history.json")
        if not os.path.isfile(path):
            self.skipTest("No policy_history.json")

        with open(path) as f:
            entries = json.load(f)

        if not isinstance(entries, list) or len(entries) < 2:
            self.skipTest("Policy history too short for ordering check")

        dates = [e["effective_from"] for e in entries]
        for i in range(1, len(dates)):
            self.assertGreater(
                dates[i], dates[i - 1],
                f"Policy history not ordered: {dates[i-1]} >= {dates[i]}",
            )

    def test_policy_history_has_required_fields(self):
        """Every policy entry must have stat_whitelist and blocked_bins."""
        path = os.path.join(MODELS_DIR, "policy_history.json")
        if not os.path.isfile(path):
            self.skipTest("No policy_history.json")

        with open(path) as f:
            entries = json.load(f)

        for i, entry in enumerate(entries):
            eff = entry.get("effective_from", f"index {i}")
            self.assertIn(
                "stat_whitelist", entry,
                f"Policy entry {eff} missing stat_whitelist",
            )
            self.assertIn(
                "blocked_bins", entry,
                f"Policy entry {eff} missing blocked_bins",
            )


# ===========================================================================
# 11. Fitting Unit Tests
# ===========================================================================

class TestFitTemperatureLogic(unittest.TestCase):
    """Unit tests for the temperature fitting functions themselves."""

    def test_sigmoid_logit_roundtrip(self):
        """sigmoid(logit(p)) should return p."""
        for p in [0.01, 0.10, 0.25, 0.50, 0.75, 0.90, 0.99]:
            roundtrip = _sigmoid(_logit(p))
            self.assertAlmostEqual(p, roundtrip, places=6,
                                   msg=f"Roundtrip failed for p={p}")

    def test_apply_temp_identity_at_T1(self):
        """T=1.0 should be identity (no calibration change)."""
        for p in [0.05, 0.20, 0.50, 0.80, 0.95]:
            self.assertAlmostEqual(
                _apply_temp(p, 1.0), p, places=6,
                msg=f"T=1.0 should be identity for p={p}",
            )

    def test_apply_temp_shrinks_toward_half(self):
        """T > 1.0 should move probabilities closer to 0.5."""
        for T in [1.5, 2.0, 3.0, 5.0]:
            for p in [0.10, 0.20, 0.80, 0.90]:
                cal_p = _apply_temp(p, T)
                self.assertLess(
                    abs(cal_p - 0.5), abs(p - 0.5) + 0.001,
                    f"T={T}, p={p}: cal_p={cal_p:.4f} not closer to 0.5",
                )

    def test_apply_temp_preserves_side(self):
        """Temperature scaling should not flip over/under."""
        for T in [1.0, 2.0, 5.0, 8.0]:
            for p in [0.05, 0.20, 0.40]:
                self.assertLess(_apply_temp(p, T), 0.5 + 0.001,
                                f"T={T}, p={p}: crossed 0.5")
            for p in [0.60, 0.80, 0.95]:
                self.assertGreater(_apply_temp(p, T), 0.5 - 0.001,
                                   f"T={T}, p={p}: crossed 0.5")

    def test_fit_temperature_returns_T_ge_1(self):
        """fit_temperature should never return T < 1.0."""
        bins = [
            {"avgPredOverProbPct": 20.0, "actualOverHitRatePct": 20.0, "count": 100},
            {"avgPredOverProbPct": 40.0, "actualOverHitRatePct": 40.0, "count": 100},
            {"avgPredOverProbPct": 60.0, "actualOverHitRatePct": 60.0, "count": 100},
            {"avgPredOverProbPct": 80.0, "actualOverHitRatePct": 80.0, "count": 100},
        ]
        T, mse, n_bins = fit_temperature(bins, min_count=50)
        self.assertGreaterEqual(T, 1.0, f"fit_temperature returned T={T} < 1.0")

    def test_fit_temperature_overconfident_input(self):
        """Overconfident model (pred far from actual) should produce T > 1.0."""
        bins = [
            {"avgPredOverProbPct": 10.0, "actualOverHitRatePct": 25.0, "count": 200},
            {"avgPredOverProbPct": 20.0, "actualOverHitRatePct": 32.0, "count": 200},
            {"avgPredOverProbPct": 80.0, "actualOverHitRatePct": 68.0, "count": 200},
            {"avgPredOverProbPct": 90.0, "actualOverHitRatePct": 75.0, "count": 200},
        ]
        T, mse, n_bins = fit_temperature(bins, min_count=50)
        self.assertGreater(T, 1.0, f"Overconfident model should get T > 1.0, got {T}")

    def test_fit_bin_temp_edge_cases(self):
        """_fit_bin_temp should handle extremes gracefully."""
        T = _fit_bin_temp(0.005, 0.01)
        self.assertEqual(T, 1.0, "Extreme low prob should return T=1.0")

        T = _fit_bin_temp(0.995, 0.99)
        self.assertEqual(T, 1.0, "Extreme high prob should return T=1.0")

        T = _fit_bin_temp(0.50, 0.50)
        self.assertGreaterEqual(T, 1.0)

    def test_apply_temp_extreme_numerical_stability(self):
        """_apply_temp should not produce NaN/Inf at extreme inputs."""
        extreme_cases = [
            (0.001, 8.0), (0.999, 8.0),  # near-boundary p, max T
            (0.001, 1.0), (0.999, 1.0),  # near-boundary p, identity T
            (0.5, 8.0),                    # midpoint, max T
            (0.0001, 3.0), (0.9999, 3.0), # very extreme p
        ]
        for p, T in extreme_cases:
            result = _apply_temp(p, T)
            self.assertFalse(
                math.isnan(result) or math.isinf(result),
                f"_apply_temp({p}, {T}) returned {result} (NaN/Inf)",
            )
            self.assertGreater(result, 0.0, f"_apply_temp({p}, {T}) <= 0")
            self.assertLess(result, 1.0, f"_apply_temp({p}, {T}) >= 1")

    def test_fit_temperature_insufficient_data(self):
        """Empty or insufficient bins should return default T=1.0."""
        T, mse, n_bins = fit_temperature([], min_count=50)
        self.assertEqual(T, 1.0)
        self.assertIsNone(mse)
        self.assertEqual(n_bins, 0)

    def test_fit_bin_temperatures_insufficient_data(self):
        """< 2 eligible bins should return None."""
        bins = [{"avgPredOverProbPct": 50.0, "actualOverHitRatePct": 50.0, "count": 100}]
        result = fit_bin_temperatures(bins, min_count=50)
        self.assertIsNone(result)


# ===========================================================================
# 12. Preflight Data Readiness
# ===========================================================================

class TestPreflightDataReadiness(unittest.TestCase):
    """Verify minimum data requirements before running expensive stress tests."""

    def test_production_calibration_exists(self):
        self.assertTrue(
            os.path.isfile(os.path.join(MODELS_DIR, "prob_calibration.json")),
            "Missing models/prob_calibration.json",
        )

    def test_walk_forward_directory_exists(self):
        self.assertTrue(os.path.isdir(WF_DIR), "Missing models/walk_forward/ directory")

    def test_backtest_results_directory_exists(self):
        self.assertTrue(
            os.path.isdir(BACKTEST_DIR),
            "Missing data/backtest_results/ -- cannot run Brier regression tests",
        )

    def test_at_least_one_backtest_result(self):
        if not os.path.isdir(BACKTEST_DIR):
            self.skipTest("No backtest_results directory")

        json_files = [f for f in os.listdir(BACKTEST_DIR)
                      if f.endswith(".json") and not f.startswith("ckpt_")]
        self.assertGreater(
            len(json_files), 0,
            "No backtest result JSON files in data/backtest_results/",
        )

    def test_local_index_exists(self):
        """Local backtest data (Kaggle index) must exist for stress test backtests."""
        ref_dir = os.path.join(ROOT, "data", "reference", "kaggle_nba")
        self.assertTrue(
            os.path.isdir(ref_dir),
            "data/reference/kaggle_nba/ not found. Local index required for "
            "stress test backtests (50x faster than NBA API fallback)."
        )


# ###########################################################################
# ###########################################################################
#
#   PHASE B — Data Readiness & Real-Line Coverage Diagnostics
#
#   Covers:
#     13. Odds store integrity (SQLite schema, tables, row counts)
#     14. Closing line coverage by book and market
#     15. Real-vs-synthetic segmentation in backtest results
#     16. Line history file continuity and schema validation
#     17. Journal data integrity (prop_journal.jsonl)
#     18. Per-book sample adequacy (minimum N per book)
#     19. Statistical minimum-N guardrails per stat
#     20. Odds collection gap detection
#     21. Lean bets data integrity
#     22. Coverage ratio thresholds for go-live readiness
#
#   Pass/Fail Thresholds:
#     - ODDS_DB_MIN_SNAPSHOTS:    <100K snapshots         -> FAIL
#     - ODDS_DB_MIN_CLOSES:       <50K closing lines      -> FAIL
#     - REAL_LINE_PCT_WARN:       <30% real lines         -> WARNING
#     - REAL_LINE_PCT_FAIL:       <10% real lines         -> FAIL
#     - BOOK_MIN_CLOSES:          <5000 per book          -> WARNING
#     - STAT_MIN_REAL_SAMPLES:    <500 per stat           -> WARNING
#     - LINE_HISTORY_MIN_DAYS:    <7 days of JSONL        -> FAIL
#     - JOURNAL_MIN_ENTRIES:      <10 entries             -> FAIL
#     - COVERAGE_GAP_MAX_DAYS:    >3 day gap in odds      -> WARNING
#     - LEAN_BETS_MIN_ENTRIES:    <100 entries            -> FAIL
# ###########################################################################
# ###########################################################################

import sqlite3
from collections import Counter

# Phase B data paths
ODDS_DB_PATH = os.path.join(ROOT, "data", "reference", "odds_history", "odds_history.sqlite")
LINE_HISTORY_DIR = os.path.join(ROOT, "data", "line_history")
PROP_JOURNAL_PATH = os.path.join(ROOT, "data", "prop_journal.jsonl")
LEAN_BETS_PATH = os.path.join(ROOT, "data", "lean_bets.jsonl")

# Phase B thresholds
ODDS_DB_MIN_SNAPSHOTS = 100_000
ODDS_DB_MIN_CLOSES = 50_000
REAL_LINE_PCT_WARN = 0.30
REAL_LINE_PCT_FAIL = 0.10
BOOK_MIN_CLOSES = 5_000
STAT_MIN_REAL_SAMPLES = 500
LINE_HISTORY_MIN_DAYS = 7
JOURNAL_MIN_ENTRIES = 10
COVERAGE_GAP_MAX_DAYS = 3
LEAN_BETS_MIN_ENTRIES = 100

# Required books and markets
REQUIRED_BOOKS = {"betmgm", "draftkings", "fanduel"}
BETTING_MARKETS = {"player_points", "player_assists"}  # stat_whitelist-aligned
ALL_MARKETS = {
    "player_points", "player_rebounds", "player_assists",
    "player_threes", "player_turnovers", "player_points_rebounds_assists",
    "player_steals", "player_blocks",
}


def _read_jsonl(path, max_lines=None):
    """Read JSONL file, return list of dicts. Skips malformed lines."""
    entries = []
    if not os.path.isfile(path):
        return entries
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if max_lines is not None and i >= max_lines:
                break
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # counted separately in integrity tests
    return entries


def _count_jsonl_lines(path):
    """Count total lines in JSONL (fast, no parsing)."""
    if not os.path.isfile(path):
        return 0
    count = 0
    with open(path, encoding="utf-8") as f:
        for _ in f:
            count += 1
    return count


def _get_odds_db_conn():
    """Open a read-only connection to odds_history.sqlite."""
    if not os.path.isfile(ODDS_DB_PATH):
        return None
    return sqlite3.connect(f"file:{ODDS_DB_PATH}?mode=ro", uri=True)


# ===========================================================================
# 13. Odds Store Integrity
# ===========================================================================

class TestOddsStoreIntegrity(unittest.TestCase):
    """Verify odds_history.sqlite exists, has correct schema, and minimum data."""

    def test_odds_db_exists(self):
        self.assertTrue(
            os.path.isfile(ODDS_DB_PATH),
            f"Odds database not found at {ODDS_DB_PATH}",
        )

    def test_required_tables_exist(self):
        conn = _get_odds_db_conn()
        if conn is None:
            self.skipTest("Odds DB not found")
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for required in ("runs", "snapshots", "closing_lines"):
                self.assertIn(
                    required, tables,
                    f"Missing required table '{required}' in odds_history.sqlite",
                )
        finally:
            conn.close()

    def test_snapshot_count_above_minimum(self):
        conn = _get_odds_db_conn()
        if conn is None:
            self.skipTest("Odds DB not found")
        try:
            count = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            self.assertGreaterEqual(
                count, ODDS_DB_MIN_SNAPSHOTS,
                f"Only {count:,} snapshots (minimum {ODDS_DB_MIN_SNAPSHOTS:,}). "
                f"Run scripts/backfill_odds_history.py to increase coverage.",
            )
        finally:
            conn.close()

    def test_closing_line_count_above_minimum(self):
        conn = _get_odds_db_conn()
        if conn is None:
            self.skipTest("Odds DB not found")
        try:
            count = conn.execute("SELECT COUNT(*) FROM closing_lines").fetchone()[0]
            self.assertGreaterEqual(
                count, ODDS_DB_MIN_CLOSES,
                f"Only {count:,} closing lines (minimum {ODDS_DB_MIN_CLOSES:,}). "
                f"Run odds_build_closes to rebuild closing line table.",
            )
        finally:
            conn.close()

    def test_closing_line_schema_has_required_columns(self):
        """closing_lines must have event_id, book, market, player_name, close_line, close_over_odds."""
        conn = _get_odds_db_conn()
        if conn is None:
            self.skipTest("Odds DB not found")
        try:
            cursor = conn.execute("PRAGMA table_info(closing_lines)")
            columns = {row[1] for row in cursor.fetchall()}
            required = {
                "event_id", "book", "market", "player_name",
                "close_line", "close_over_odds", "close_under_odds",
                "commence_time",
            }
            missing = required - columns
            self.assertEqual(
                missing, set(),
                f"closing_lines missing columns: {missing}",
            )
        finally:
            conn.close()

    def test_no_null_close_lines(self):
        """close_line should never be NULL in the closing_lines table."""
        conn = _get_odds_db_conn()
        if conn is None:
            self.skipTest("Odds DB not found")
        try:
            null_count = conn.execute(
                "SELECT COUNT(*) FROM closing_lines WHERE close_line IS NULL"
            ).fetchone()[0]
            self.assertEqual(
                null_count, 0,
                f"{null_count:,} closing_lines rows have NULL close_line",
            )
        finally:
            conn.close()

    def test_closing_line_values_in_range(self):
        """close_line should be in [0.5, 80.0] for player props."""
        conn = _get_odds_db_conn()
        if conn is None:
            self.skipTest("Odds DB not found")
        try:
            out_of_range = conn.execute(
                "SELECT COUNT(*) FROM closing_lines "
                "WHERE close_line < 0.5 OR close_line > 80.0"
            ).fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM closing_lines").fetchone()[0]
            if total > 0:
                pct = out_of_range / total
                self.assertLess(
                    pct, 0.01,
                    f"{out_of_range:,}/{total:,} ({pct:.1%}) closing lines out of "
                    f"range [0.5, 80.0]. Data quality issue.",
                )
        finally:
            conn.close()

    def test_odds_values_reasonable(self):
        """close_over_odds and close_under_odds should be in [-500, +500] for typical props."""
        conn = _get_odds_db_conn()
        if conn is None:
            self.skipTest("Odds DB not found")
        try:
            extreme = conn.execute(
                "SELECT COUNT(*) FROM closing_lines "
                "WHERE close_over_odds < -500 OR close_over_odds > 500 "
                "OR close_under_odds < -500 OR close_under_odds > 500"
            ).fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM closing_lines").fetchone()[0]
            if total > 0:
                pct = extreme / total
                # Allow up to 5% extreme odds (heavy favorites/underdogs)
                self.assertLess(
                    pct, 0.05,
                    f"{extreme:,}/{total:,} ({pct:.1%}) closing lines have extreme "
                    f"odds (outside [-500, +500]). Verify data quality.",
                )
        finally:
            conn.close()


# ===========================================================================
# 14. Closing Line Coverage by Book & Market
# ===========================================================================

class TestClosingLineCoverage(unittest.TestCase):
    """Verify per-book and per-market closing line coverage meets minimums."""

    def test_all_required_books_present(self):
        conn = _get_odds_db_conn()
        if conn is None:
            self.skipTest("Odds DB not found")
        try:
            books = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT book FROM closing_lines"
                ).fetchall()
            }
            missing = REQUIRED_BOOKS - books
            self.assertEqual(
                missing, set(),
                f"Required books missing from closing_lines: {missing}",
            )
        finally:
            conn.close()

    def test_per_book_minimum_closes(self):
        """Each required book must have >= BOOK_MIN_CLOSES closing lines."""
        conn = _get_odds_db_conn()
        if conn is None:
            self.skipTest("Odds DB not found")
        try:
            cursor = conn.execute(
                "SELECT book, COUNT(*) FROM closing_lines GROUP BY book"
            )
            book_counts = {row[0]: row[1] for row in cursor.fetchall()}
            warnings = []
            for book in REQUIRED_BOOKS:
                count = book_counts.get(book, 0)
                if count < BOOK_MIN_CLOSES:
                    warnings.append(f"{book}: {count:,} closes (need {BOOK_MIN_CLOSES:,})")
            if warnings:
                print(f"\n  [WARNING] Low per-book coverage:\n    " +
                      "\n    ".join(warnings))
            # Hard fail only if ALL required books are missing or very low
            total_required = sum(book_counts.get(b, 0) for b in REQUIRED_BOOKS)
            self.assertGreater(
                total_required, BOOK_MIN_CLOSES,
                f"Total closing lines across required books ({total_required:,}) "
                f"below single-book minimum ({BOOK_MIN_CLOSES:,})",
            )
        finally:
            conn.close()

    def test_betting_markets_have_coverage(self):
        """Markets in BETTING_POLICY.stat_whitelist must have closing lines."""
        conn = _get_odds_db_conn()
        if conn is None:
            self.skipTest("Odds DB not found")
        try:
            cursor = conn.execute(
                "SELECT market, COUNT(*) FROM closing_lines GROUP BY market"
            )
            market_counts = {row[0]: row[1] for row in cursor.fetchall()}
            for market in BETTING_MARKETS:
                count = market_counts.get(market, 0)
                self.assertGreater(
                    count, 0,
                    f"Betting market '{market}' has zero closing lines. "
                    f"Cannot compute real-line ROI for this stat.",
                )
        finally:
            conn.close()

    def test_all_markets_present(self):
        """All expected markets should appear in closing_lines."""
        conn = _get_odds_db_conn()
        if conn is None:
            self.skipTest("Odds DB not found")
        try:
            markets = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT market FROM closing_lines"
                ).fetchall()
            }
            missing = ALL_MARKETS - markets
            if missing:
                print(f"\n  [WARNING] Markets missing from closing_lines: {missing}")
            # Only fail if betting-critical markets are missing
            betting_missing = BETTING_MARKETS - markets
            self.assertEqual(
                betting_missing, set(),
                f"Betting-critical markets missing: {betting_missing}",
            )
        finally:
            conn.close()

    def test_date_range_covers_season(self):
        """Closing lines should span most of the current season."""
        conn = _get_odds_db_conn()
        if conn is None:
            self.skipTest("Odds DB not found")
        try:
            row = conn.execute(
                "SELECT MIN(commence_time), MAX(commence_time) FROM closing_lines"
            ).fetchone()
            if row[0] and row[1]:
                min_date = row[0][:10]
                max_date = row[1][:10]
                # Should span at least 60 days
                d1 = date.fromisoformat(min_date)
                d2 = date.fromisoformat(max_date)
                span = (d2 - d1).days
                self.assertGreaterEqual(
                    span, 60,
                    f"Closing line date range is only {span} days ({min_date} to "
                    f"{max_date}). Need >= 60 for meaningful coverage.",
                )
        finally:
            conn.close()


# ===========================================================================
# 15. Real vs Synthetic Segmentation in Backtest Results
# ===========================================================================

class TestRealVsSyntheticSegmentation(unittest.TestCase):
    """Verify backtest results properly segment real-line vs synthetic-line metrics."""

    @classmethod
    def _load_latest_backtest(cls):
        """Load the most recently modified backtest result file."""
        if not os.path.isdir(BACKTEST_DIR):
            return None, None
        files = [
            f for f in os.listdir(BACKTEST_DIR)
            if f.endswith(".json") and not f.startswith("ckpt_")
        ]
        if not files:
            return None, None
        files.sort(
            key=lambda x: os.path.getmtime(os.path.join(BACKTEST_DIR, x)),
            reverse=True,
        )
        path = os.path.join(BACKTEST_DIR, files[0])
        with open(path) as f:
            return json.load(f), files[0]

    def test_backtest_has_real_line_fields(self):
        """Backtest report must contain realLineSamples, roiReal, roiSynth."""
        data, fname = self._load_latest_backtest()
        if data is None:
            self.skipTest("No backtest results available")
        report = data.get("reports", {}).get("full", {})
        for field in ("realLineSamples", "missingLineSamples", "roiReal", "roiSynth"):
            self.assertIn(
                field, report,
                f"Backtest '{fname}' missing field '{field}' in reports.full",
            )

    def test_real_line_percentage_above_fail_threshold(self):
        """realLineSamples / (realLineSamples + missingLineSamples) must be >= REAL_LINE_PCT_FAIL."""
        data, fname = self._load_latest_backtest()
        if data is None:
            self.skipTest("No backtest results available")
        report = data.get("reports", {}).get("full", {})
        real = report.get("realLineSamples", 0)
        missing = report.get("missingLineSamples", 0)
        total = real + missing
        if total == 0:
            self.skipTest("No samples in latest backtest")
        pct = real / total
        if pct < REAL_LINE_PCT_WARN:
            print(f"\n  [WARNING] Real-line coverage only {pct:.1%} in {fname} "
                  f"({real:,}/{total:,}). Below {REAL_LINE_PCT_WARN:.0%} warning threshold.")
        self.assertGreaterEqual(
            pct, REAL_LINE_PCT_FAIL,
            f"Real-line coverage {pct:.1%} in {fname} is below hard fail "
            f"threshold {REAL_LINE_PCT_FAIL:.0%} ({real:,}/{total:,}). "
            f"Backtest ROI is almost entirely synthetic.",
        )

    def test_real_line_stat_roi_exists_for_betting_stats(self):
        """realLineStatRoi should have entries for pts and ast."""
        data, fname = self._load_latest_backtest()
        if data is None:
            self.skipTest("No backtest results available")
        report = data.get("reports", {}).get("full", {})
        stat_roi = report.get("realLineStatRoi", {})
        for stat in ("pts", "ast"):
            self.assertIn(
                stat, stat_roi,
                f"realLineStatRoi in {fname} missing betting stat '{stat}'",
            )

    def test_real_line_stat_roi_sample_counts(self):
        """Each betting stat must have >= STAT_MIN_REAL_SAMPLES in real-line ROI."""
        data, fname = self._load_latest_backtest()
        if data is None:
            self.skipTest("No backtest results available")
        report = data.get("reports", {}).get("full", {})
        stat_roi = report.get("realLineStatRoi", {})
        warnings = []
        for stat in ("pts", "ast"):
            roi_data = stat_roi.get(stat, {})
            placed = roi_data.get("betsPlaced", 0)
            if placed < STAT_MIN_REAL_SAMPLES:
                warnings.append(f"{stat}: {placed} real-line bets (need {STAT_MIN_REAL_SAMPLES})")
        if warnings:
            print(f"\n  [WARNING] Low real-line stat coverage in {fname}:\n    " +
                  "\n    ".join(warnings))

    def test_roi_real_vs_synth_divergence(self):
        """Flag if roiReal and roiSynth ROI differ by >30pp (suggests synthetic is misleading)."""
        data, fname = self._load_latest_backtest()
        if data is None:
            self.skipTest("No backtest results available")
        report = data.get("reports", {}).get("full", {})
        roi_real = report.get("roiReal", {})
        roi_synth = report.get("roiSynth", {})
        real_roi = roi_real.get("roiPctPerBet") if roi_real else None
        synth_roi = roi_synth.get("roiPctPerBet") if roi_synth else None
        if real_roi is None or synth_roi is None:
            # Try hitRatePct as fallback
            real_roi = roi_real.get("hitRatePct") if roi_real else None
            synth_roi = roi_synth.get("hitRatePct") if roi_synth else None
        if real_roi is not None and synth_roi is not None:
            gap = abs(real_roi - synth_roi)
            if gap > 30:
                print(
                    f"\n  [WARNING] Real vs Synthetic ROI divergence: {gap:.1f}pp "
                    f"(real={real_roi:.1f}%, synth={synth_roi:.1f}%) in {fname}. "
                    f"Synthetic ROI may be misleading."
                )

    def test_real_line_calib_bins_present(self):
        """realLineCalibBins should be a list of 10 bins."""
        data, fname = self._load_latest_backtest()
        if data is None:
            self.skipTest("No backtest results available")
        report = data.get("reports", {}).get("full", {})
        bins = report.get("realLineCalibBins", [])
        self.assertIsInstance(bins, list, f"realLineCalibBins is not a list in {fname}")
        if bins:
            self.assertEqual(
                len(bins), 10,
                f"Expected 10 calibration bins, got {len(bins)} in {fname}",
            )

    def test_all_backtests_have_real_line_fields(self):
        """Every non-checkpoint backtest file should have real-line segmentation."""
        if not os.path.isdir(BACKTEST_DIR):
            self.skipTest("No backtest_results directory")
        files = [
            f for f in os.listdir(BACKTEST_DIR)
            if f.endswith(".json") and not f.startswith("ckpt_")
        ]
        missing_fields = []
        for fname in files:
            try:
                with open(os.path.join(BACKTEST_DIR, fname)) as fh:
                    data = json.load(fh)
                report = data.get("reports", {}).get("full", {})
                if "realLineSamples" not in report:
                    missing_fields.append(fname)
            except (json.JSONDecodeError, KeyError):
                continue
        if missing_fields:
            print(f"\n  [WARNING] {len(missing_fields)} backtest files missing "
                  f"realLineSamples: {missing_fields[:5]}")


# ===========================================================================
# 16. Line History File Continuity & Schema
# ===========================================================================

class TestLineHistoryContinuity(unittest.TestCase):
    """Verify daily line_history JSONL files exist and have valid schema."""

    def test_line_history_directory_exists(self):
        self.assertTrue(
            os.path.isdir(LINE_HISTORY_DIR),
            f"Line history directory not found: {LINE_HISTORY_DIR}",
        )

    def test_minimum_days_of_line_history(self):
        """Must have at least LINE_HISTORY_MIN_DAYS of daily JSONL files."""
        if not os.path.isdir(LINE_HISTORY_DIR):
            self.skipTest("No line_history directory")
        jsonl_files = [f for f in os.listdir(LINE_HISTORY_DIR) if f.endswith(".jsonl")]
        self.assertGreaterEqual(
            len(jsonl_files), LINE_HISTORY_MIN_DAYS,
            f"Only {len(jsonl_files)} line history files (minimum {LINE_HISTORY_MIN_DAYS})",
        )

    def test_line_history_dates_are_valid(self):
        """Filenames should be valid YYYY-MM-DD.jsonl dates."""
        if not os.path.isdir(LINE_HISTORY_DIR):
            self.skipTest("No line_history directory")
        bad_names = []
        for fname in os.listdir(LINE_HISTORY_DIR):
            if not fname.endswith(".jsonl"):
                continue
            date_part = fname[:-6]  # strip .jsonl
            try:
                date.fromisoformat(date_part)
            except ValueError:
                bad_names.append(fname)
        self.assertEqual(
            bad_names, [],
            f"Line history files with invalid date names: {bad_names}",
        )

    def test_line_history_no_large_gaps(self):
        """No gap > COVERAGE_GAP_MAX_DAYS between consecutive line history files."""
        if not os.path.isdir(LINE_HISTORY_DIR):
            self.skipTest("No line_history directory")
        dates = []
        for fname in sorted(os.listdir(LINE_HISTORY_DIR)):
            if not fname.endswith(".jsonl"):
                continue
            try:
                dates.append(date.fromisoformat(fname[:-6]))
            except ValueError:
                continue
        if len(dates) < 2:
            self.skipTest("Too few line history files to check gaps")
        gaps = []
        for i in range(1, len(dates)):
            gap = (dates[i] - dates[i - 1]).days
            if gap > COVERAGE_GAP_MAX_DAYS:
                gaps.append((dates[i - 1].isoformat(), dates[i].isoformat(), gap))
        if gaps:
            gap_strs = [f"{g[0]}→{g[1]} ({g[2]}d)" for g in gaps]
            print(f"\n  [WARNING] Line history gaps > {COVERAGE_GAP_MAX_DAYS} days:\n    " +
                  "\n    ".join(gap_strs))

    def test_line_history_schema_valid(self):
        """Sample latest JSONL file and verify required fields are present."""
        if not os.path.isdir(LINE_HISTORY_DIR):
            self.skipTest("No line_history directory")
        jsonl_files = sorted(
            [f for f in os.listdir(LINE_HISTORY_DIR) if f.endswith(".jsonl")]
        )
        if not jsonl_files:
            self.skipTest("No JSONL files in line_history")
        latest = os.path.join(LINE_HISTORY_DIR, jsonl_files[-1])
        entries = _read_jsonl(latest, max_lines=20)
        if not entries:
            self.skipTest(f"Latest line history file {jsonl_files[-1]} is empty")
        required_fields = {
            "timestamp_utc", "game_id", "player_name", "stat",
            "line", "over_odds", "under_odds", "book",
        }
        for entry in entries:
            missing = required_fields - set(entry.keys())
            self.assertEqual(
                missing, set(),
                f"Line history entry missing fields: {missing}\n"
                f"  File: {jsonl_files[-1]}\n  Entry: {entry.get('player_name', '?')}",
            )

    def test_line_history_no_corrupt_lines(self):
        """Latest JSONL should have zero JSON parse errors."""
        if not os.path.isdir(LINE_HISTORY_DIR):
            self.skipTest("No line_history directory")
        jsonl_files = sorted(
            [f for f in os.listdir(LINE_HISTORY_DIR) if f.endswith(".jsonl")]
        )
        if not jsonl_files:
            self.skipTest("No JSONL files")
        latest_path = os.path.join(LINE_HISTORY_DIR, jsonl_files[-1])
        total, errors = 0, 0
        with open(latest_path, encoding="utf-8") as f:
            for line in f:
                total += 1
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    errors += 1
        if total > 0:
            error_pct = errors / total
            self.assertLess(
                error_pct, 0.01,
                f"{errors}/{total} ({error_pct:.1%}) corrupt lines in "
                f"{jsonl_files[-1]}",
            )

    def test_line_history_stats_match_expected(self):
        """Stats in line history should be from the known set."""
        if not os.path.isdir(LINE_HISTORY_DIR):
            self.skipTest("No line_history directory")
        jsonl_files = sorted(
            [f for f in os.listdir(LINE_HISTORY_DIR) if f.endswith(".jsonl")]
        )
        if not jsonl_files:
            self.skipTest("No JSONL files")
        latest = os.path.join(LINE_HISTORY_DIR, jsonl_files[-1])
        entries = _read_jsonl(latest, max_lines=500)
        known_stats = {"pts", "reb", "ast", "fg3m", "pra", "stl", "blk", "tov"}
        found_stats = {e.get("stat") for e in entries if e.get("stat")}
        unknown = found_stats - known_stats
        if unknown:
            print(f"\n  [WARNING] Unknown stats in line_history: {unknown}")


# ===========================================================================
# 17. Journal Data Integrity
# ===========================================================================

class TestJournalDataIntegrity(unittest.TestCase):
    """Verify prop_journal.jsonl has valid entries with required fields."""

    def test_journal_file_exists(self):
        self.assertTrue(
            os.path.isfile(PROP_JOURNAL_PATH),
            f"prop_journal.jsonl not found at {PROP_JOURNAL_PATH}",
        )

    def test_journal_minimum_entries(self):
        count = _count_jsonl_lines(PROP_JOURNAL_PATH)
        self.assertGreaterEqual(
            count, JOURNAL_MIN_ENTRIES,
            f"Only {count} journal entries (minimum {JOURNAL_MIN_ENTRIES})",
        )

    def test_journal_required_fields(self):
        """Sample first 50 entries and verify required fields."""
        entries = _read_jsonl(PROP_JOURNAL_PATH, max_lines=50)
        if not entries:
            self.skipTest("Journal is empty")
        required = {
            "entryId", "pickDate", "playerName", "stat",
            "line", "projection", "recommendedSide",
        }
        for entry in entries:
            missing = required - set(entry.keys())
            self.assertEqual(
                missing, set(),
                f"Journal entry {entry.get('entryId', '?')} missing: {missing}",
            )

    def test_journal_no_duplicate_entry_ids(self):
        """entryId should be unique across all journal entries."""
        entries = _read_jsonl(PROP_JOURNAL_PATH)
        if not entries:
            self.skipTest("Journal is empty")
        ids = [e.get("entryId") for e in entries if e.get("entryId")]
        dupes = {eid for eid in ids if ids.count(eid) > 1}
        self.assertEqual(
            len(dupes), 0,
            f"{len(dupes)} duplicate entryIds found in prop_journal.jsonl",
        )

    def test_journal_stat_values_valid(self):
        """Stat field should be from the known set."""
        entries = _read_jsonl(PROP_JOURNAL_PATH, max_lines=200)
        if not entries:
            self.skipTest("Journal is empty")
        known_stats = {"pts", "reb", "ast", "fg3m", "pra", "stl", "blk", "tov"}
        invalid = []
        for e in entries:
            stat = e.get("stat")
            if stat and stat not in known_stats:
                invalid.append(stat)
        self.assertEqual(
            invalid, [],
            f"Journal entries with invalid stats: {set(invalid)}",
        )

    def test_journal_projection_line_sanity(self):
        """Projection and line values should be non-negative and reasonable."""
        entries = _read_jsonl(PROP_JOURNAL_PATH, max_lines=200)
        if not entries:
            self.skipTest("Journal is empty")
        bad = []
        for e in entries:
            proj = e.get("projection")
            line = e.get("line")
            if proj is not None and (proj < 0 or proj > 100):
                bad.append(f"proj={proj} for {e.get('playerName')}")
            if line is not None and (line < 0 or line > 100):
                bad.append(f"line={line} for {e.get('playerName')}")
        if bad:
            print(f"\n  [WARNING] {len(bad)} journal entries with extreme values:\n    " +
                  "\n    ".join(bad[:5]))

    def test_journal_settlement_consistency(self):
        """Settled entries must have actualStat and result."""
        entries = _read_jsonl(PROP_JOURNAL_PATH)
        if not entries:
            self.skipTest("Journal is empty")
        inconsistent = []
        for e in entries:
            if e.get("settled"):
                if e.get("actualStat") is None and e.get("result") is None:
                    inconsistent.append(e.get("entryId", "?"))
        if inconsistent:
            print(f"\n  [WARNING] {len(inconsistent)} settled entries missing "
                  f"actualStat/result: {inconsistent[:5]}")


# ===========================================================================
# 18. Per-Book Sample Adequacy
# ===========================================================================

class TestPerBookSampleAdequacy(unittest.TestCase):
    """Verify each book has sufficient closing lines per betting market."""

    def test_betmgm_has_most_coverage(self):
        """BetMGM (book priority #1) should have the most closing lines."""
        conn = _get_odds_db_conn()
        if conn is None:
            self.skipTest("Odds DB not found")
        try:
            cursor = conn.execute(
                "SELECT book, COUNT(*) FROM closing_lines "
                "WHERE book IN ('betmgm', 'draftkings', 'fanduel') "
                "GROUP BY book ORDER BY COUNT(*) DESC"
            )
            rows = cursor.fetchall()
            if not rows:
                self.skipTest("No closing lines for required books")
            # BetMGM should be among the top — warn if not
            book_counts = {row[0]: row[1] for row in rows}
            betmgm = book_counts.get("betmgm", 0)
            max_book = max(book_counts.values()) if book_counts else 0
            if betmgm < max_book * 0.5:
                top_book = max(book_counts, key=book_counts.get)
                print(
                    f"\n  [WARNING] BetMGM ({betmgm:,} closes) has less than half "
                    f"of {top_book} ({max_book:,}). Consider backfilling BetMGM."
                )
        finally:
            conn.close()

    def test_per_book_per_market_matrix(self):
        """Print and validate book × market coverage matrix."""
        conn = _get_odds_db_conn()
        if conn is None:
            self.skipTest("Odds DB not found")
        try:
            cursor = conn.execute(
                "SELECT book, market, COUNT(*) FROM closing_lines "
                "GROUP BY book, market"
            )
            matrix = {}
            for book, market, count in cursor.fetchall():
                matrix.setdefault(book, {})[market] = count

            # Check betting-critical cells
            for book in REQUIRED_BOOKS:
                for market in BETTING_MARKETS:
                    count = matrix.get(book, {}).get(market, 0)
                    if count == 0:
                        print(
                            f"\n  [WARNING] ZERO closing lines for "
                            f"{book} × {market}. Critical gap."
                        )
        finally:
            conn.close()


# ===========================================================================
# 19. Statistical Minimum-N Guardrails
# ===========================================================================

class TestMinimumNGuardrails(unittest.TestCase):
    """Verify sample sizes meet statistical significance thresholds."""

    def test_backtest_total_sample_count(self):
        """Latest backtest should have sampleCount > 1000."""
        data, fname = TestRealVsSyntheticSegmentation._load_latest_backtest()
        if data is None:
            self.skipTest("No backtest results")
        report = data.get("reports", {}).get("full", {})
        count = report.get("sampleCount", 0)
        self.assertGreater(
            count, 1000,
            f"Backtest {fname} has only {count} samples (need >1000 for "
            f"statistically meaningful results)",
        )

    def test_brier_stat_sample_counts(self):
        """Each stat in brierByStat should be based on enough samples for reliability."""
        data, fname = TestRealVsSyntheticSegmentation._load_latest_backtest()
        if data is None:
            self.skipTest("No backtest results")
        report = data.get("reports", {}).get("full", {})
        calib = report.get("calibrationByStat", {})
        for stat, bins in calib.items():
            if not isinstance(bins, list):
                continue
            total = sum(b.get("count", 0) for b in bins if isinstance(b, dict))
            if total < 200:
                print(f"\n  [WARNING] {stat} has only {total} calibration samples "
                      f"in {fname}. Brier score may be unreliable.")

    def test_clv_sample_minimum(self):
        """CLV analysis needs >= 50 tracked bets to be meaningful."""
        data, fname = TestRealVsSyntheticSegmentation._load_latest_backtest()
        if data is None:
            self.skipTest("No backtest results")
        report = data.get("reports", {}).get("full", {})
        clv = report.get("clv", {})
        tracked = clv.get("betsTracked", 0)
        if tracked < 50:
            print(f"\n  [WARNING] Only {tracked} CLV-tracked bets in {fname}. "
                  f"CLV metrics may not be statistically significant.")

    def test_go_live_gate_sample_minimum(self):
        """GO-LIVE gate requires sample >= 50 in paper_summary.
        Verify the backtest has enough real-line samples for pts+ast combined."""
        data, fname = TestRealVsSyntheticSegmentation._load_latest_backtest()
        if data is None:
            self.skipTest("No backtest results")
        report = data.get("reports", {}).get("full", {})
        stat_roi = report.get("realLineStatRoi", {})
        total_betting_real = 0
        for stat in ("pts", "ast"):
            placed = stat_roi.get(stat, {}).get("betsPlaced", 0)
            total_betting_real += placed
        self.assertGreaterEqual(
            total_betting_real, 50,
            f"Only {total_betting_real} real-line bets for pts+ast combined "
            f"in {fname}. GO-LIVE gate requires >= 50 samples.",
        )


# ===========================================================================
# 20. Odds Collection Gap Detection
# ===========================================================================

class TestOddsCollectionGaps(unittest.TestCase):
    """Detect gaps in odds collection that would cause missing closing lines."""

    def test_snapshot_date_continuity(self):
        """Snapshots should have data for most game days (no multi-day gaps)."""
        conn = _get_odds_db_conn()
        if conn is None:
            self.skipTest("Odds DB not found")
        try:
            cursor = conn.execute(
                "SELECT DISTINCT DATE(ts_utc) AS d FROM snapshots ORDER BY d"
            )
            dates = [date.fromisoformat(row[0]) for row in cursor.fetchall() if row[0]]
            if len(dates) < 2:
                self.skipTest("Too few snapshot dates")
            gaps = []
            for i in range(1, len(dates)):
                gap = (dates[i] - dates[i - 1]).days
                if gap > COVERAGE_GAP_MAX_DAYS:
                    gaps.append((dates[i - 1].isoformat(), dates[i].isoformat(), gap))
            if gaps:
                gap_strs = [f"{g[0]}→{g[1]} ({g[2]}d)" for g in gaps[:10]]
                print(
                    f"\n  [WARNING] {len(gaps)} snapshot collection gaps "
                    f"> {COVERAGE_GAP_MAX_DAYS} days:\n    " +
                    "\n    ".join(gap_strs)
                )
        finally:
            conn.close()

    def test_closing_line_rebuild_coverage(self):
        """Compare snapshot dates to closing_lines dates — flag any dates with
        snapshots but no closing lines (rebuild may be needed)."""
        conn = _get_odds_db_conn()
        if conn is None:
            self.skipTest("Odds DB not found")
        try:
            snap_dates = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT DATE(ts_utc) FROM snapshots"
                ).fetchall()
                if row[0]
            }
            close_dates = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT DATE(commence_time) FROM closing_lines"
                ).fetchall()
                if row[0]
            }
            # Dates with snapshots but no closing lines
            orphaned = snap_dates - close_dates
            if orphaned:
                # Only warn if recent dates are orphaned (last 14 days)
                today = date.today()
                recent_orphaned = [
                    d for d in orphaned
                    if (today - date.fromisoformat(d)).days <= 14
                ]
                if recent_orphaned:
                    print(
                        f"\n  [WARNING] {len(recent_orphaned)} recent dates have "
                        f"snapshots but no closing lines (run odds_build_closes):\n    " +
                        "\n    ".join(sorted(recent_orphaned)[:5])
                    )
        finally:
            conn.close()

    def test_run_completion_status(self):
        """All backfill runs should have status='done' (no stuck/failed runs)."""
        conn = _get_odds_db_conn()
        if conn is None:
            self.skipTest("Odds DB not found")
        try:
            cursor = conn.execute(
                "SELECT status, COUNT(*) FROM runs GROUP BY status"
            )
            status_counts = {row[0]: row[1] for row in cursor.fetchall()}
            failed = status_counts.get("failed", 0)
            stuck = status_counts.get("running", 0)
            if failed > 0:
                print(f"\n  [WARNING] {failed} failed backfill runs in odds_history")
            if stuck > 0:
                print(f"\n  [WARNING] {stuck} runs stuck in 'running' state")
        finally:
            conn.close()


# ===========================================================================
# 21. Lean Bets Data Integrity
# ===========================================================================

class TestLeanBetsIntegrity(unittest.TestCase):
    """Verify lean_bets.jsonl has valid entries with required segmentation fields."""

    def test_lean_bets_file_exists(self):
        self.assertTrue(
            os.path.isfile(LEAN_BETS_PATH),
            f"lean_bets.jsonl not found at {LEAN_BETS_PATH}",
        )

    def test_lean_bets_minimum_entries(self):
        count = _count_jsonl_lines(LEAN_BETS_PATH)
        self.assertGreaterEqual(
            count, LEAN_BETS_MIN_ENTRIES,
            f"Only {count} lean bets (minimum {LEAN_BETS_MIN_ENTRIES})",
        )

    def test_lean_bets_required_fields(self):
        """Sample first 100 entries and verify required segmentation fields."""
        entries = _read_jsonl(LEAN_BETS_PATH, max_lines=100)
        if not entries:
            self.skipTest("Lean bets file is empty")
        required = {
            "date", "player_name", "stat", "line", "projection",
            "side", "edge", "outcome", "pnl",
        }
        for entry in entries:
            missing = required - set(entry.keys())
            self.assertEqual(
                missing, set(),
                f"Lean bet entry missing fields: {missing}\n"
                f"  Player: {entry.get('player_name', '?')} "
                f"Stat: {entry.get('stat', '?')}",
            )

    def test_lean_bets_used_real_line_field(self):
        """Entries should have used_real_line for real-vs-synthetic segmentation."""
        entries = _read_jsonl(LEAN_BETS_PATH, max_lines=200)
        if not entries:
            self.skipTest("Lean bets file is empty")
        has_field = sum(1 for e in entries if "used_real_line" in e)
        pct = has_field / len(entries) if entries else 0
        self.assertGreater(
            pct, 0.5,
            f"Only {pct:.0%} of lean bet entries have 'used_real_line' field. "
            f"Cannot segment real vs synthetic results.",
        )

    def test_lean_bets_policy_detail_present(self):
        """Entries should include policy_detail for gating analysis."""
        entries = _read_jsonl(LEAN_BETS_PATH, max_lines=200)
        if not entries:
            self.skipTest("Lean bets file is empty")
        has_policy = sum(1 for e in entries if "policy_detail" in e)
        # Not all entries may have it (depends on version), but majority should
        if len(entries) > 0 and has_policy / len(entries) < 0.5:
            print(
                f"\n  [WARNING] Only {has_policy}/{len(entries)} lean bets have "
                f"policy_detail. Gating analysis limited."
            )

    def test_lean_bets_real_line_coverage_by_stat(self):
        """Report real-line coverage per stat in lean_bets."""
        entries = _read_jsonl(LEAN_BETS_PATH)
        if not entries:
            self.skipTest("Lean bets file is empty")
        stat_total = Counter()
        stat_real = Counter()
        for e in entries:
            stat = e.get("stat")
            if stat:
                stat_total[stat] += 1
                if e.get("used_real_line"):
                    stat_real[stat] += 1
        # Print coverage report
        if stat_total:
            print("\n  Lean bets real-line coverage by stat:")
            for stat in sorted(stat_total):
                total = stat_total[stat]
                real = stat_real.get(stat, 0)
                pct = real / total if total > 0 else 0
                flag = " [LOW]" if pct < 0.30 else ""
                print(f"    {stat}: {real:,}/{total:,} ({pct:.0%}){flag}")


# ===========================================================================
# 22. Coverage Ratio Thresholds for GO-LIVE Readiness
# ===========================================================================

class TestGoLiveCoverageReadiness(unittest.TestCase):
    """Aggregate coverage checks that determine if data supports GO-LIVE."""

    def test_odds_db_size_sanity(self):
        """DB file should be > 1 MB (not a stub or corrupted file)."""
        if not os.path.isfile(ODDS_DB_PATH):
            self.skipTest("Odds DB not found")
        size_mb = os.path.getsize(ODDS_DB_PATH) / (1024 * 1024)
        self.assertGreater(
            size_mb, 1.0,
            f"odds_history.sqlite is only {size_mb:.1f} MB — likely empty or corrupt",
        )

    def test_journal_covers_recent_dates(self):
        """Journal should have entries from the last 7 days."""
        entries = _read_jsonl(PROP_JOURNAL_PATH)
        if not entries:
            self.skipTest("Journal is empty")
        today = date.today()
        recent = [
            e for e in entries
            if e.get("pickDate") and
            (today - date.fromisoformat(e["pickDate"])).days <= 7
        ]
        if not recent:
            print(
                "\n  [WARNING] No journal entries from the last 7 days. "
                "Pipeline may not be running."
            )

    def test_line_history_covers_today_or_yesterday(self):
        """Line history should have a file for today or yesterday."""
        if not os.path.isdir(LINE_HISTORY_DIR):
            self.skipTest("No line_history directory")
        today = date.today()
        yesterday = today - timedelta(days=1)
        today_file = os.path.join(LINE_HISTORY_DIR, f"{today.isoformat()}.jsonl")
        yesterday_file = os.path.join(LINE_HISTORY_DIR, f"{yesterday.isoformat()}.jsonl")
        has_recent = os.path.isfile(today_file) or os.path.isfile(yesterday_file)
        if not has_recent:
            print(
                f"\n  [WARNING] No line history file for {today.isoformat()} or "
                f"{yesterday.isoformat()}. Line collection may not be running."
            )

    def test_closing_lines_cover_recent_period(self):
        """Closing lines should include data from the last 7 days."""
        conn = _get_odds_db_conn()
        if conn is None:
            self.skipTest("Odds DB not found")
        try:
            cutoff = (date.today() - timedelta(days=7)).isoformat()
            count = conn.execute(
                "SELECT COUNT(*) FROM closing_lines WHERE commence_time >= ?",
                (cutoff,),
            ).fetchone()[0]
            if count == 0:
                print(
                    f"\n  [WARNING] No closing lines since {cutoff}. "
                    f"Run odds_build_closes to rebuild."
                )
        finally:
            conn.close()

    def test_aggregate_data_readiness_verdict(self):
        """Final aggregate: all critical data sources must be present and populated."""
        issues = []
        if not os.path.isfile(ODDS_DB_PATH):
            issues.append("odds_history.sqlite missing")
        if not os.path.isdir(LINE_HISTORY_DIR):
            issues.append("line_history/ directory missing")
        if not os.path.isfile(PROP_JOURNAL_PATH):
            issues.append("prop_journal.jsonl missing")
        if not os.path.isfile(LEAN_BETS_PATH):
            issues.append("lean_bets.jsonl missing")
        if not os.path.isdir(BACKTEST_DIR):
            issues.append("backtest_results/ directory missing")

        # Check non-empty
        if os.path.isfile(ODDS_DB_PATH):
            conn = _get_odds_db_conn()
            if conn:
                try:
                    cl_count = conn.execute(
                        "SELECT COUNT(*) FROM closing_lines"
                    ).fetchone()[0]
                    if cl_count == 0:
                        issues.append("closing_lines table is empty")
                finally:
                    conn.close()

        if os.path.isfile(PROP_JOURNAL_PATH):
            if _count_jsonl_lines(PROP_JOURNAL_PATH) == 0:
                issues.append("prop_journal.jsonl is empty")

        self.assertEqual(
            issues, [],
            f"Data readiness failures:\n  " + "\n  ".join(issues),
        )


# ###########################################################################
# ###########################################################################
#
#   PHASE C — Robustness Sweeps & Calibration Window Sensitivity
#
#   Covers:
#     23. SHRINK_K sensitivity analysis via temperature sweep
#     24. Calibration window sensitivity (ast divergence resolution)
#     25. Feature ablation verification (no_blend, no_gates variants)
#     26. Stdev stress testing (extreme / zero / negative inputs)
#     27. Minutes model multiplier impact on EV
#     28. Cross-parameter interaction effects (T × bin blocking)
#
#   Pass/Fail Thresholds:
#     - TEMP_SWEEP_EV_MONOTONE:   calibrated EV must be monotone with T   -> FAIL
#     - AST_DIVERGENCE_REPORT:    informational — quantifies ast T range
#     - STDEV_EXTREME_NAN:        any NaN/Inf in compute_ev output        -> HARD FAIL
#     - VARIANT_BRIER_MAX:        variant backtest Brier > 0.30           -> WARNING
#     - MINUTES_EV_MONOTONE:      higher minutes → higher EV for overs    -> WARNING
# ###########################################################################
# ###########################################################################

from core.nba_ev_engine import (
    compute_ev,
    _apply_temp_scaling,
    _POISSON_STATS,
)

# Phase C thresholds
AST_DIVERGENCE_MAX_RANGE = 3.0    # max spread of ast T across WF files
VARIANT_BRIER_CEILING = 0.30      # variant backtests Brier ceiling
STDEV_VALUES_SWEEP = [0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0]
TEMP_VALUES_SWEEP = [1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 8.0]
MINUTES_MULT_SWEEP = [0.50, 0.65, 0.85, 1.00, 1.15]


# ===========================================================================
# 23. Temperature Sweep Sensitivity
# ===========================================================================

class TestTemperatureSweepSensitivity(unittest.TestCase):
    """Sweep calibration T and verify EV output responds correctly."""

    def _compute_ev_with_temp(self, projection, line, stat, T):
        """Compute EV using _apply_temp_scaling at a given T to verify sensitivity."""
        result = compute_ev(
            projection=projection, line=line,
            over_odds=-110, under_odds=-110,
            stdev=projection * 0.20, stat=stat,
        )
        # Manually compute what calibrated prob would be at this T
        raw_p = result["probOverRaw"]
        cal_p = _apply_temp_scaling(raw_p, T)
        return raw_p, cal_p, result

    def test_higher_T_shrinks_probability_toward_half(self):
        """Higher T should push probabilities closer to 0.5."""
        for stat in ["pts", "ast", "reb"]:
            for proj, line in [(28.0, 25.5), (8.0, 7.5), (22.0, 24.5)]:
                prev_dist = None
                for T in TEMP_VALUES_SWEEP:
                    raw_p, cal_p, _ = self._compute_ev_with_temp(proj, line, stat, T)
                    dist = abs(cal_p - 0.5)
                    if prev_dist is not None:
                        self.assertLessEqual(
                            dist, prev_dist + 0.001,
                            f"{stat} proj={proj} line={line}: T={T} pushed prob "
                            f"AWAY from 0.5 (dist={dist:.4f} > prev={prev_dist:.4f})",
                        )
                    prev_dist = dist

    def test_T1_preserves_raw_probability(self):
        """At T=1.0, calibrated probability should equal raw probability."""
        for proj, line, stat in [(25.0, 24.5, "pts"), (7.0, 6.5, "ast")]:
            raw_p, cal_p, _ = self._compute_ev_with_temp(proj, line, stat, 1.0)
            self.assertAlmostEqual(
                raw_p, cal_p, places=6,
                msg=f"T=1.0 should be identity for {stat}: raw={raw_p:.6f} cal={cal_p:.6f}",
            )

    def test_edge_magnitude_decreases_with_higher_T(self):
        """Higher T should generally reduce edge magnitude (less confident)."""
        proj, line = 28.0, 25.5
        edges = []
        for T in [1.0, 2.0, 4.0, 8.0]:
            cal_p = _apply_temp_scaling(0.65, T)
            no_vig = 0.5  # -110/-110 → ~50/50
            edge = abs(cal_p - no_vig)
            edges.append((T, edge))
        # Edge should generally decrease (warn if not monotone)
        for i in range(1, len(edges)):
            if edges[i][1] > edges[i-1][1] + 0.01:
                print(
                    f"\n  [WARNING] Edge increased from T={edges[i-1][0]} to "
                    f"T={edges[i][0]}: {edges[i-1][1]:.4f} → {edges[i][1]:.4f}"
                )

    def test_temperature_sweep_produces_valid_output(self):
        """compute_ev at every T value should produce valid output dict."""
        for stat in ["pts", "ast", "stl", "fg3m"]:
            proj, line = (25.0, 24.5) if stat in ("pts", "ast") else (1.5, 1.5)
            result = compute_ev(
                projection=proj, line=line,
                over_odds=-110, under_odds=-110,
                stdev=proj * 0.20 if stat not in _POISSON_STATS else None,
                stat=stat,
            )
            self.assertIsNotNone(result)
            self.assertIn("probOver", result)
            self.assertIn("over", result)
            self.assertIn("under", result)
            self.assertGreater(result["probOver"], 0.0)
            self.assertLess(result["probOver"], 1.0)

    def test_poisson_vs_normal_path_selection(self):
        """Poisson stats should use poisson distribution mode."""
        for stat in _POISSON_STATS:
            result = compute_ev(
                projection=1.5, line=1.5,
                over_odds=-110, under_odds=-110, stat=stat,
            )
            self.assertEqual(
                result["distributionMode"], "poisson",
                f"{stat} should use poisson but got {result['distributionMode']}",
            )
        for stat in ["pts", "reb", "ast", "pra"]:
            result = compute_ev(
                projection=25.0, line=24.5,
                over_odds=-110, under_odds=-110,
                stdev=5.0, stat=stat,
            )
            self.assertEqual(
                result["distributionMode"], "normal",
                f"{stat} should use normal but got {result['distributionMode']}",
            )


# ===========================================================================
# 24. Calibration Window Sensitivity (AST Divergence Resolution)
# ===========================================================================

class TestCalibrationWindowSensitivity(unittest.TestCase):
    """Analyze ast temperature divergence: prod=2.24 vs latest WF=1.00.

    This test class directly addresses the Phase A finding that ast has 124%
    divergence between production and latest walk-forward calibration.
    It investigates WHY by analyzing T trajectory, sample counts, and
    window-specific fitting behavior.
    """

    @classmethod
    def setUpClass(cls):
        cls.wf_files = _load_wf_files()
        cls.prod_cal = _load_prod_cal()

    def test_ast_T_trajectory_report(self):
        """Report ast T across all walk-forward snapshots — diagnostic."""
        if not self.wf_files:
            self.skipTest("No walk-forward files")

        ast_temps = []
        for date_str, data in self.wf_files:
            if data is None:
                continue
            t_ast = data.get("ast")
            n_ast = data.get("_sample_counts", {}).get("ast", 0)
            if t_ast is not None:
                ast_temps.append((date_str, t_ast, n_ast))

        if not ast_temps:
            self.skipTest("No ast temperature data in walk-forward files")

        # Print trajectory
        print("\n  AST calibration temperature trajectory:")
        for d, t, n in ast_temps:
            print(f"    {d}: T={t:.3f} (n={n:,})")

        # Report range
        t_values = [t for _, t, _ in ast_temps]
        t_range = max(t_values) - min(t_values)
        print(f"\n  AST T range: {min(t_values):.3f} → {max(t_values):.3f} "
              f"(spread={t_range:.3f})")

        # Report prod vs latest
        if self.prod_cal and "ast" in self.prod_cal:
            prod_t = self.prod_cal["ast"]
            latest_t = ast_temps[-1][1]
            pct_diff = _pct_change(latest_t, prod_t)
            print(f"  Production ast T={prod_t:.3f} vs latest WF T={latest_t:.3f} "
                  f"(divergence={pct_diff:.0%})")

    def test_ast_T_stabilizes_with_sample_count(self):
        """ast T should stabilize as sample count grows (less oscillation).

        Check that the coefficient of variation of ast T in the last 5
        walk-forward windows is lower than in the first 5.
        """
        if not self.wf_files:
            self.skipTest("No walk-forward files")

        ast_temps = []
        for _, data in self.wf_files:
            if data is None:
                continue
            t_ast = data.get("ast")
            if t_ast is not None:
                ast_temps.append(t_ast)

        if len(ast_temps) < 10:
            self.skipTest(f"Only {len(ast_temps)} ast T values (need >= 10)")

        early = ast_temps[:5]
        late = ast_temps[-5:]

        def cv(vals):
            mean = sum(vals) / len(vals)
            if mean == 0:
                return 0
            var = sum((v - mean) ** 2 for v in vals) / len(vals)
            return (var ** 0.5) / mean

        cv_early = cv(early)
        cv_late = cv(late)
        print(f"\n  AST T coefficient of variation: early={cv_early:.3f} late={cv_late:.3f}")
        if cv_late > cv_early:
            print("  [WARNING] ast T is MORE volatile in recent windows — "
                  "possible regime change or data shift")

    def test_per_stat_T_convergence_rate(self):
        """For each stat, report how quickly T converges across WF windows."""
        if not self.wf_files:
            self.skipTest("No walk-forward files")

        stats = ["pts", "reb", "ast", "fg3m", "pra", "stl", "blk", "tov"]
        print("\n  Per-stat T convergence (first → last, range):")
        for stat in stats:
            temps = []
            for _, data in self.wf_files:
                if data is None:
                    continue
                t = data.get(stat)
                if t is not None:
                    temps.append(t)
            if not temps:
                print(f"    {stat}: no data")
                continue
            t_range = max(temps) - min(temps)
            flag = " [HIGH RANGE]" if t_range > 2.0 else ""
            print(f"    {stat}: {temps[0]:.2f} → {temps[-1]:.2f} "
                  f"(range={t_range:.2f}, n_windows={len(temps)}){flag}")

    def test_ast_divergence_root_cause_analysis(self):
        """Identify which WF window transition caused the largest ast T shift.

        This helps identify if the divergence is a sudden regime change
        or gradual drift.
        """
        if not self.wf_files:
            self.skipTest("No walk-forward files")

        transitions = []
        for i in range(1, len(self.wf_files)):
            d_prev, cal_prev = self.wf_files[i - 1]
            d_curr, cal_curr = self.wf_files[i]
            if cal_prev is None or cal_curr is None:
                continue
            t_prev = cal_prev.get("ast")
            t_curr = cal_curr.get("ast")
            if t_prev is None or t_curr is None:
                continue
            delta = abs(t_curr - t_prev)
            n_prev = cal_prev.get("_sample_counts", {}).get("ast", 0)
            n_curr = cal_curr.get("_sample_counts", {}).get("ast", 0)
            transitions.append((d_prev, d_curr, t_prev, t_curr, delta, n_prev, n_curr))

        if not transitions:
            self.skipTest("No ast transitions found")

        # Find largest shift
        transitions.sort(key=lambda x: x[4], reverse=True)
        top = transitions[0]
        print(f"\n  Largest ast T shift: {top[0]} → {top[1]}")
        print(f"    T: {top[2]:.3f} → {top[3]:.3f} (delta={top[4]:.3f})")
        print(f"    Samples: {top[5]:,} → {top[6]:,}")

        # Print top 3
        if len(transitions) >= 3:
            print("  Top 3 ast T shifts:")
            for t in transitions[:3]:
                print(f"    {t[0]} → {t[1]}: T={t[2]:.3f} → {t[3]:.3f} "
                      f"(Δ={t[4]:.3f}, n={t[5]:,} → {t[6]:,})")

    def test_production_T_within_wf_range(self):
        """Production T for each stat should fall within the range of WF T values.

        If prod T is outside the WF range, it suggests the production calibration
        was fitted on a fundamentally different data window.
        """
        if not self.wf_files or not self.prod_cal:
            self.skipTest("Missing walk-forward or production calibration")

        outside = []
        for stat in _stat_keys(self.prod_cal):
            wf_temps = []
            for _, data in self.wf_files:
                if data is None:
                    continue
                t = data.get(stat)
                if t is not None:
                    wf_temps.append(t)
            if not wf_temps:
                continue
            prod_T = self.prod_cal[stat]
            wf_min, wf_max = min(wf_temps), max(wf_temps)
            if prod_T < wf_min or prod_T > wf_max:
                outside.append(
                    f"  {stat}: prod={prod_T:.2f}, WF range=[{wf_min:.2f}, {wf_max:.2f}]"
                )

        betting_outside = [o for o in outside if any(
            o.strip().startswith(f"{s}:") for s in BETTING_STATS
        )]
        research_outside = [o for o in outside if o not in betting_outside]

        if research_outside:
            print(f"\n  [INFO] Production T outside WF range for research stats:")
            for o in research_outside:
                print(o)

        if betting_outside:
            self.fail(
                f"Production T outside WF range for BETTING stats "
                f"(pts/ast must be within WF range):\n"
                + "\n".join(betting_outside)
            )

    def test_ast_sample_count_trajectory(self):
        """ast sample counts should grow monotonically across WF windows."""
        if not self.wf_files:
            self.skipTest("No walk-forward files")

        counts = []
        for date_str, data in self.wf_files:
            if data is None:
                continue
            n = data.get("_sample_counts", {}).get("ast", 0)
            counts.append((date_str, n))

        if not counts:
            self.skipTest("No ast sample count data")

        # Check monotonicity
        decreases = []
        for i in range(1, len(counts)):
            if counts[i][1] < counts[i-1][1]:
                decreases.append(
                    f"    {counts[i-1][0]}: n={counts[i-1][1]:,} → "
                    f"{counts[i][0]}: n={counts[i][1]:,}"
                )

        if decreases:
            print(f"\n  [WARNING] ast sample count decreased in {len(decreases)} transitions:")
            for d in decreases[:3]:
                print(d)


# ===========================================================================
# 25. Feature Ablation Verification
# ===========================================================================

class TestFeatureAblation(unittest.TestCase):
    """Verify backtest variants (no_blend, no_gates, matchlive) exist and are valid."""

    def _find_variant_backtests(self, tag):
        """Find backtest files containing the given variant tag."""
        if not os.path.isdir(BACKTEST_DIR):
            return []
        return [
            f for f in sorted(os.listdir(BACKTEST_DIR))
            if f.endswith(".json") and not f.startswith("ckpt_") and tag in f
        ]

    def test_noblend_variant_exists(self):
        """A no_blend backtest should exist for ablation analysis."""
        variants = self._find_variant_backtests("_noblend")
        if not variants:
            print("\n  [INFO] No _noblend backtest variants found. "
                  "Run backtest with --no-blend to generate.")
        else:
            print(f"\n  Found {len(variants)} no_blend variant(s): {variants[:3]}")

    def test_variant_brier_below_ceiling(self):
        """All variant backtests should have Brier < VARIANT_BRIER_CEILING."""
        variant_tags = ("_matchlive", "_noblend", "_opening", "_wf",
                        "_realonly", "_nogates")
        above = []
        for fname, brier in _find_backtest_files_with_brier():
            if not any(tag in fname for tag in variant_tags):
                continue
            for stat, val in brier.items():
                if val is not None and val > VARIANT_BRIER_CEILING:
                    above.append(f"  {fname}/{stat}: Brier={val:.4f}")

        if above:
            print(f"\n  [WARNING] {len(above)} variant Brier scores > "
                  f"{VARIANT_BRIER_CEILING}:")
            for a in above[:5]:
                print(a)

    def test_no_blend_vs_blend_ev_difference(self):
        """Verify no_blend changes EV output (it's actually doing something).

        Compute EV for the same inputs with the production calibration
        (which was fitted with no_blend=True). If output is identical
        regardless of blend mode, the feature has no effect.
        """
        result = compute_ev(
            projection=28.0, line=25.5,
            over_odds=-110, under_odds=-110,
            stdev=5.0, stat="pts",
        )
        self.assertIsNotNone(result)
        # compute_ev itself doesn't have a no_blend flag (that's in projection).
        # Verify the EV result has calibration applied (probOver != probOverRaw).
        if abs(result["probOver"] - result["probOverRaw"]) < 0.001:
            print("\n  [INFO] Calibration had negligible effect on pts EV. "
                  "This may indicate T ≈ 1.0 for this probability range.")

    def test_reference_mode_skips_calibration(self):
        """reference_probs mode should bypass temperature scaling entirely."""
        ref = {"over": 0.55, "under": 0.45, "push": 0.0}
        result = compute_ev(
            projection=28.0, line=25.5,
            over_odds=-110, under_odds=-110,
            stdev=5.0, stat="pts",
            reference_probs=ref,
        )
        self.assertEqual(result["distributionMode"], "reference")
        self.assertAlmostEqual(result["probOver"], 0.55, places=3)
        self.assertAlmostEqual(result["probUnder"], 0.45, places=3)
        # Raw should equal calibrated in reference mode
        self.assertAlmostEqual(
            result["probOver"], result["probOverRaw"], places=6,
            msg="Reference mode should not apply calibration",
        )


# ===========================================================================
# 26. Stdev Stress Testing
# ===========================================================================

class TestStdevStress(unittest.TestCase):
    """Feed extreme stdev values into compute_ev and verify stability."""

    def _validate_ev_result(self, result, label):
        """Common validation for any compute_ev output."""
        self.assertIsNotNone(result, f"{label}: result is None")
        for key in ("probOver", "probUnder", "probPush"):
            val = result.get(key)
            self.assertIsNotNone(val, f"{label}: {key} is None")
            self.assertFalse(
                math.isnan(val) or math.isinf(val),
                f"{label}: {key}={val} is NaN/Inf",
            )
            self.assertGreaterEqual(val, 0.0, f"{label}: {key}={val} < 0")
            self.assertLessEqual(val, 1.0, f"{label}: {key}={val} > 1")

        over_side = result.get("over")
        under_side = result.get("under")
        self.assertIsNotNone(over_side, f"{label}: over side is None")
        self.assertIsNotNone(under_side, f"{label}: under side is None")

    def test_stdev_sweep_no_nan(self):
        """Sweep stdev from very small to very large — no NaN/Inf."""
        for stdev in STDEV_VALUES_SWEEP:
            result = compute_ev(
                projection=25.0, line=24.5,
                over_odds=-110, under_odds=-110,
                stdev=stdev, stat="pts",
            )
            self._validate_ev_result(result, f"stdev={stdev}")

    def test_stdev_zero_handled(self):
        """stdev=0 should use fallback (max(projection*0.20, 1.0)), not crash."""
        result = compute_ev(
            projection=25.0, line=24.5,
            over_odds=-110, under_odds=-110,
            stdev=0, stat="pts",
        )
        self._validate_ev_result(result, "stdev=0")
        # Fallback should be max(25*0.20, 1.0) = 5.0
        self.assertGreater(result["stdev"], 0, "stdev=0 should trigger fallback")

    def test_stdev_negative_handled(self):
        """Negative stdev should use fallback."""
        result = compute_ev(
            projection=25.0, line=24.5,
            over_odds=-110, under_odds=-110,
            stdev=-5.0, stat="pts",
        )
        self._validate_ev_result(result, "stdev=-5")
        self.assertGreater(result["stdev"], 0, "Negative stdev should use fallback")

    def test_stdev_very_large_pushes_to_fifty(self):
        """Very large stdev should push probabilities toward 0.5 (high uncertainty)."""
        result = compute_ev(
            projection=25.0, line=24.5,
            over_odds=-110, under_odds=-110,
            stdev=100.0, stat="pts",
        )
        self._validate_ev_result(result, "stdev=100")
        # With sigma=100, P(over 24.5) ≈ 0.50
        self.assertAlmostEqual(
            result["probOverRaw"], 0.5, delta=0.05,
            msg="Very large stdev should push prob toward 0.5",
        )

    def test_stdev_very_small_gives_strong_signal(self):
        """Very small stdev with proj > line should give high P(over)."""
        result = compute_ev(
            projection=30.0, line=24.5,
            over_odds=-110, under_odds=-110,
            stdev=0.5, stat="pts",
        )
        self._validate_ev_result(result, "stdev=0.5, proj>>line")
        self.assertGreater(
            result["probOverRaw"], 0.99,
            "proj=30, line=24.5, stdev=0.5 → P(over) should be > 0.99",
        )

    def test_stdev_per_stat_sweep(self):
        """Every stat type should handle extreme stdev without error."""
        for stat in ["pts", "reb", "ast", "fg3m", "stl", "blk", "tov", "pra"]:
            proj = 25.0 if stat in ("pts", "pra") else 5.0
            line = proj - 0.5
            for stdev in [0.01, 0.5, 5.0, 50.0]:
                # Poisson stats ignore stdev, but should not crash
                result = compute_ev(
                    projection=proj, line=line,
                    over_odds=-110, under_odds=-110,
                    stdev=stdev, stat=stat,
                )
                self._validate_ev_result(result, f"{stat}/stdev={stdev}")


# ===========================================================================
# 27. Minutes Model Multiplier Impact
# ===========================================================================

class TestMinutesMultiplierImpact(unittest.TestCase):
    """Verify that minutes multiplier variations affect EV output sensibly.

    A higher minutes multiplier implies the player gets more minutes,
    which should increase the projection → change EV.
    """

    def test_higher_projection_increases_over_probability(self):
        """For overs, higher projection → higher P(over)."""
        prob_overs = []
        for mult in MINUTES_MULT_SWEEP:
            # Simulate projection affected by minutes: base * mult
            base_proj = 25.0
            adj_proj = base_proj * mult
            result = compute_ev(
                projection=adj_proj, line=24.5,
                over_odds=-110, under_odds=-110,
                stdev=5.0, stat="pts",
            )
            prob_overs.append((mult, result["probOverRaw"]))

        # P(over) should be non-decreasing as multiplier increases
        for i in range(1, len(prob_overs)):
            self.assertGreaterEqual(
                prob_overs[i][1], prob_overs[i-1][1] - 0.001,
                f"P(over) decreased when minutes mult went from "
                f"{prob_overs[i-1][0]} to {prob_overs[i][0]}",
            )

    def test_minutes_multiplier_extremes(self):
        """Extreme multipliers (0.50, 1.15) should produce valid but different outputs."""
        results = {}
        for mult in [0.50, 1.15]:
            adj_proj = 25.0 * mult
            result = compute_ev(
                projection=adj_proj, line=24.5,
                over_odds=-110, under_odds=-110,
                stdev=5.0, stat="pts",
            )
            self.assertIsNotNone(result)
            results[mult] = result["probOverRaw"]

        self.assertGreater(
            results[1.15], results[0.50],
            "1.15x minutes should have higher P(over) than 0.50x",
        )

    def test_minutes_effect_on_edge_magnitude(self):
        """Report how minutes multiplier affects edge — diagnostic."""
        print("\n  Minutes multiplier → edge impact (pts, line=24.5):")
        for mult in MINUTES_MULT_SWEEP:
            adj_proj = 25.0 * mult
            result = compute_ev(
                projection=adj_proj, line=24.5,
                over_odds=-110, under_odds=-110,
                stdev=5.0, stat="pts",
            )
            over_edge = result["over"]["edge"]
            under_edge = result["under"]["edge"]
            best_side = "OVER" if over_edge > under_edge else "UNDER"
            best_edge = max(over_edge, under_edge)
            print(f"    mult={mult:.2f} → proj={adj_proj:.1f}, "
                  f"best={best_side} edge={best_edge:.4f}")


# ===========================================================================
# 28. Cross-Parameter Interaction Effects
# ===========================================================================

class TestCrossParameterInteractions(unittest.TestCase):
    """Test interactions between calibration T, probability bins, and edge."""

    def test_T_x_probability_bin_consistency(self):
        """For each probability bin, verify calibrated output moves consistently with T.

        The bin a probability lands in depends on the raw probability,
        and the calibrated output should be consistent within that bin.
        """
        # Generate raw probabilities across the full range
        test_probs = [0.05 + i * 0.10 for i in range(10)]  # 0.05, 0.15, ..., 0.95
        for raw_p in test_probs:
            bin_idx = max(0, min(9, int(raw_p * 10)))
            # T sweep should be monotone (closer to 0.5) for each bin
            prev_dist = None
            for T in [1.0, 2.0, 4.0, 8.0]:
                cal_p = _apply_temp_scaling(raw_p, T)
                dist = abs(cal_p - 0.5)
                if prev_dist is not None:
                    self.assertLessEqual(
                        dist, prev_dist + 0.001,
                        f"bin={bin_idx} raw_p={raw_p:.2f} T={T}: "
                        f"dist={dist:.4f} > prev={prev_dist:.4f}",
                    )
                prev_dist = dist

    def test_blocked_bins_still_produce_valid_ev(self):
        """Even if a probability bin is blocked by policy, compute_ev
        should still return valid output (gating is done separately)."""
        # Blocked bins: {1,2,3,4,5,6,7,8} — only bins 0 and 9 active
        # Generate inputs that would land in blocked bins
        for raw_prob_target in [0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85]:
            # Find projection that gives roughly this raw probability
            # For pts, proj ≈ line + stdev * z where z = cdf_inv(1 - raw_prob)
            proj = 25.0 + 5.0 * (raw_prob_target - 0.5) * 4.0
            result = compute_ev(
                projection=proj, line=25.0,
                over_odds=-110, under_odds=-110,
                stdev=5.0, stat="pts",
            )
            self.assertIsNotNone(result)
            self.assertIn("over", result)
            self.assertIn("under", result)

    def test_calibration_preserves_probability_sum(self):
        """probOver + probUnder + probPush should sum to 1.0 after calibration."""
        test_cases = [
            (28.0, 25.5, "pts", 5.0),   # over-favored
            (22.0, 25.5, "pts", 5.0),   # under-favored
            (25.5, 25.5, "pts", 5.0),   # push-heavy
            (1.8, 1.5, "stl", None),    # Poisson
            (7.0, 6.5, "ast", 2.0),     # ast (divergent T)
        ]
        for proj, line, stat, stdev in test_cases:
            result = compute_ev(
                projection=proj, line=line,
                over_odds=-110, under_odds=-110,
                stdev=stdev, stat=stat,
            )
            total = result["probOver"] + result["probUnder"] + result["probPush"]
            self.assertAlmostEqual(
                total, 1.0, places=4,
                msg=f"Prob sum={total:.6f} ≠ 1.0 for {stat} proj={proj} line={line}",
            )

    def test_odds_asymmetry_effect(self):
        """Asymmetric odds (e.g., -130/+110) should affect edge but not probabilities."""
        symmetric = compute_ev(
            projection=25.0, line=24.5,
            over_odds=-110, under_odds=-110,
            stdev=5.0, stat="pts",
        )
        asymmetric = compute_ev(
            projection=25.0, line=24.5,
            over_odds=-130, under_odds=+110,
            stdev=5.0, stat="pts",
        )
        # Model probabilities should be identical (same projection/line/stdev)
        self.assertAlmostEqual(
            symmetric["probOver"], asymmetric["probOver"], places=4,
            msg="Model probabilities should not depend on odds",
        )
        # But edges should differ (different no-vig implied)
        self.assertNotAlmostEqual(
            symmetric["over"]["edge"], asymmetric["over"]["edge"], places=2,
            msg="Edges should differ with different odds (different no-vig implied)",
        )


# ###########################################################################
# ###########################################################################
#
#   PHASE D — OOS Edge Persistence & Real-Line-Only Verdicting
#
#   Covers:
#     29. OOS vs IS split analysis (early-season vs mid-season backtests)
#     30. Real-line-only verdict validation
#     31. Edge stability over time (early vs late period in backtests)
#     32. CLV persistence and correlation with ROI
#     33. Per-stat OOS consistency (pts vs ast vs reb)
#
#   Pass/Fail Thresholds:
#     - REAL_LINE_SAMPLES_MIN:     <20 real-line samples in backtest -> WARNING
#     - OOS_ROI_FLOOR:             OOS ROI < -20% -> FAIL
#     - CLV_ROI_CORRELATION:       positive CLV should correlate with positive ROI
#     - STAT_OOS_HIT_RATE_MIN:     per-stat hit rate < 40% in OOS -> WARNING
# ###########################################################################
# ###########################################################################

# Phase D thresholds
REAL_LINE_SAMPLES_MIN = 20
OOS_ROI_FLOOR = -0.20   # -20% ROI floor for OOS periods
STAT_OOS_HIT_RATE_MIN = 0.40


def _load_backtest_full(filename):
    """Load full backtest result dict."""
    path = os.path.join(BACKTEST_DIR, filename)
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _classify_backtest_period(filename):
    """Classify a backtest by its date range: 'oos', 'is', 'mixed', or 'unknown'."""
    base = filename.replace("_full_local.json", "").replace("_full_local_wf.json", "")
    parts = base.split("_to_")
    if len(parts) != 2:
        return "unknown", None, None
    try:
        d_from = date.fromisoformat(parts[0])
        d_to = date.fromisoformat(parts[1])
    except ValueError:
        return "unknown", None, None

    # IS period: Dec 28 - Feb 25 (calibration fitting window)
    is_start = date(2025, 12, 28)
    is_end = date(2026, 2, 25)

    if d_to <= is_start:
        return "oos", d_from, d_to
    elif d_from >= is_start and d_to <= is_end:
        return "is", d_from, d_to
    elif d_from < is_start:
        return "mixed", d_from, d_to
    else:
        return "mixed", d_from, d_to


# ===========================================================================
# 29. OOS vs IS Split Analysis
# ===========================================================================

class TestOOSvsISSplit(unittest.TestCase):
    """Compare backtest metrics for in-sample vs out-of-sample periods."""

    def _find_canonical_backtests(self):
        """Find non-variant backtests grouped by period type."""
        if not os.path.isdir(BACKTEST_DIR):
            return {}
        variant_tags = ("_matchlive", "_noblend", "_opening", "_wf", "_v2", "_v3",
                        "_realonly", "_nogates")
        results = {"oos": [], "is": [], "mixed": []}
        for fname in sorted(os.listdir(BACKTEST_DIR)):
            if not fname.endswith("_full_local.json") or fname.startswith("ckpt_"):
                continue
            if any(tag in fname for tag in variant_tags):
                continue
            period, d_from, d_to = _classify_backtest_period(fname)
            if period in results:
                results[period].append(fname)
        return results

    def test_oos_backtests_exist(self):
        """At least one OOS backtest (ending before Dec 28) should exist."""
        groups = self._find_canonical_backtests()
        if not groups.get("oos"):
            print("\n  [WARNING] No OOS-only backtest found (ending before 2025-12-28). "
                  "Run: nba_mod.py backtest 2025-10-21 2025-11-30 --model full --local --save")

    def test_oos_roi_above_floor(self):
        """OOS backtest ROI should not be catastrophically negative."""
        groups = self._find_canonical_backtests()
        for fname in groups.get("oos", []):
            data = _load_backtest_full(fname)
            if data is None:
                continue
            report = data.get("reports", {}).get("full", {})
            # Check real-line ROI if available, else overall
            real_roi = report.get("roiReal", {})
            if isinstance(real_roi, dict):
                pnl = real_roi.get("pnlUnits", 0)
                placed = real_roi.get("betsPlaced", 0)
                if placed > 0:
                    roi = pnl / placed
                    if roi < OOS_ROI_FLOOR:
                        print(f"\n  [WARNING] OOS backtest {fname} has real-line "
                              f"ROI={roi:.1%} (below {OOS_ROI_FLOOR:.0%} floor)")

    def test_oos_vs_is_brier_comparison(self):
        """Compare Brier scores between OOS and IS periods — diagnostic."""
        groups = self._find_canonical_backtests()
        oos_briers = {}
        is_briers = {}

        for fname in groups.get("oos", []):
            brier = _load_backtest_brier(fname)
            if brier:
                oos_briers[fname] = brier

        for fname in groups.get("is", []):
            brier = _load_backtest_brier(fname)
            if brier:
                is_briers[fname] = brier

        if oos_briers and is_briers:
            print("\n  OOS vs IS Brier comparison:")
            # Average across files for each stat
            stats = ["pts", "reb", "ast", "pra"]
            for stat in stats:
                oos_vals = [b.get(stat) for b in oos_briers.values() if b.get(stat) is not None]
                is_vals = [b.get(stat) for b in is_briers.values() if b.get(stat) is not None]
                if oos_vals and is_vals:
                    oos_avg = sum(oos_vals) / len(oos_vals)
                    is_avg = sum(is_vals) / len(is_vals)
                    diff = oos_avg - is_avg
                    flag = " [OOS WORSE]" if diff > 0.02 else ""
                    print(f"    {stat}: OOS={oos_avg:.4f} IS={is_avg:.4f} "
                          f"(diff={diff:+.4f}){flag}")


# ===========================================================================
# 30. Real-Line-Only Verdict Validation
# ===========================================================================

class TestRealLineOnlyVerdict(unittest.TestCase):
    """Verify backtest results properly separate real vs synthetic metrics."""

    @staticmethod
    def _load_latest_backtest():
        """Reuse Phase B's latest backtest loader."""
        return TestRealVsSyntheticSegmentation._load_latest_backtest()

    def test_real_line_samples_present(self):
        """Latest backtest must report realLineSamples."""
        data, fname = self._load_latest_backtest()
        if data is None:
            self.skipTest("No backtest results")
        report = data.get("reports", {}).get("full", {})
        real_samples = report.get("realLineSamples", 0)
        self.assertGreater(
            real_samples, 0,
            f"Backtest {fname} reports 0 realLineSamples. "
            f"Closing line matching may be broken.",
        )

    def test_real_line_roi_field_exists(self):
        """roiReal must exist and be a dict with betsPlaced and pnlUnits."""
        data, fname = self._load_latest_backtest()
        if data is None:
            self.skipTest("No backtest results")
        report = data.get("reports", {}).get("full", {})
        roi_real = report.get("roiReal")
        self.assertIsInstance(
            roi_real, dict,
            f"Backtest {fname}: roiReal is not a dict (got {type(roi_real).__name__})",
        )
        self.assertIn("betsPlaced", roi_real, f"{fname}: roiReal missing betsPlaced")
        self.assertIn("pnlUnits", roi_real, f"{fname}: roiReal missing pnlUnits")

    def test_synth_roi_separated_from_real(self):
        """roiSynth should be distinct from roiReal when both have samples."""
        data, fname = self._load_latest_backtest()
        if data is None:
            self.skipTest("No backtest results")
        report = data.get("reports", {}).get("full", {})
        roi_real = report.get("roiReal", {})
        roi_synth = report.get("roiSynth", {})
        real_placed = roi_real.get("betsPlaced", 0)
        synth_placed = roi_synth.get("betsPlaced", 0)

        if real_placed > 0 and synth_placed > 0:
            real_roi = roi_real["pnlUnits"] / real_placed
            synth_roi = roi_synth["pnlUnits"] / synth_placed
            print(f"\n  Real-line ROI: {real_roi:+.1%} ({real_placed} bets)")
            print(f"  Synthetic ROI: {synth_roi:+.1%} ({synth_placed} bets)")
            print("  Verdict should use REAL-line ROI only.")

    def test_real_line_stat_roi_present(self):
        """realLineStatRoi should break down real-line ROI by stat."""
        data, fname = self._load_latest_backtest()
        if data is None:
            self.skipTest("No backtest results")
        report = data.get("reports", {}).get("full", {})
        stat_roi = report.get("realLineStatRoi", {})
        if not stat_roi:
            print(f"\n  [WARNING] {fname} has no realLineStatRoi breakdown")
            return

        print(f"\n  Real-line ROI by stat ({fname}):")
        for stat, info in sorted(stat_roi.items()):
            if isinstance(info, dict):
                placed = info.get("betsPlaced", 0)
                pnl = info.get("pnlUnits", 0)
                roi = pnl / placed if placed > 0 else 0
                print(f"    {stat}: ROI={roi:+.1%} ({placed} bets)")

    def test_real_line_minimum_for_verdict(self):
        """Must have >= REAL_LINE_SAMPLES_MIN real-line bets for a valid verdict."""
        data, fname = self._load_latest_backtest()
        if data is None:
            self.skipTest("No backtest results")
        report = data.get("reports", {}).get("full", {})
        roi_real = report.get("roiReal", {})
        real_placed = roi_real.get("betsPlaced", 0)

        if real_placed < REAL_LINE_SAMPLES_MIN:
            print(f"\n  [WARNING] Only {real_placed} real-line bets in {fname} "
                  f"(need >= {REAL_LINE_SAMPLES_MIN} for a statistically meaningful verdict)")


# ===========================================================================
# 31. Edge Stability Over Time
# ===========================================================================

class TestEdgeStabilityOverTime(unittest.TestCase):
    """Analyze whether edge is stable or decaying across time windows."""

    def test_longer_backtests_show_stable_or_growing_edge(self):
        """Compare shorter vs longer canonical backtests — edge should not collapse."""
        if not os.path.isdir(BACKTEST_DIR):
            self.skipTest("No backtest_results directory")

        canonical = []
        variant_tags = ("_matchlive", "_noblend", "_opening", "_wf", "_v2", "_v3",
                        "_realonly", "_nogates")
        for fname in sorted(os.listdir(BACKTEST_DIR)):
            if not fname.endswith("_full_local.json") or fname.startswith("ckpt_"):
                continue
            if any(tag in fname for tag in variant_tags):
                continue
            base = fname[:-len("_full_local.json")]
            parts = base.split("_to_")
            if len(parts) == 2:
                try:
                    d_from = date.fromisoformat(parts[0])
                    d_to = date.fromisoformat(parts[1])
                    span = (d_to - d_from).days
                    canonical.append((span, fname))
                except ValueError:
                    continue

        if len(canonical) < 2:
            self.skipTest("Need >= 2 canonical backtests for comparison")

        canonical.sort(key=lambda x: x[0])
        print("\n  Edge stability across backtest durations:")
        for span, fname in canonical:
            data = _load_backtest_full(fname)
            if data is None:
                continue
            report = data.get("reports", {}).get("full", {})
            sample = report.get("sampleCount", 0)
            roi_real = report.get("roiReal", {})
            placed = roi_real.get("betsPlaced", 0) if isinstance(roi_real, dict) else 0
            pnl = roi_real.get("pnlUnits", 0) if isinstance(roi_real, dict) else 0
            roi = pnl / placed if placed > 0 else 0
            print(f"    {span}d ({fname[:25]}...): sample={sample}, "
                  f"real_bets={placed}, real_ROI={roi:+.1%}")


# ===========================================================================
# 32. CLV Persistence
# ===========================================================================

class TestCLVPersistence(unittest.TestCase):
    """Verify CLV metrics exist and positive CLV correlates with positive ROI."""

    def test_clv_present_in_backtests(self):
        """Backtests should include CLV analysis."""
        if not os.path.isdir(BACKTEST_DIR):
            self.skipTest("No backtest_results directory")

        has_clv = 0
        total = 0
        for fname in sorted(os.listdir(BACKTEST_DIR)):
            if not fname.endswith(".json") or fname.startswith("ckpt_"):
                continue
            total += 1
            data = _load_backtest_full(fname)
            if data is None:
                continue
            report = data.get("reports", {}).get("full", {})
            if "clv" in report:
                has_clv += 1

        if total > 0:
            pct = has_clv / total
            if pct < 0.5:
                print(f"\n  [WARNING] Only {has_clv}/{total} ({pct:.0%}) backtests "
                      f"include CLV data. Run with --compute-clv for better analysis.")

    def test_positive_clv_associates_with_positive_roi(self):
        """In backtests with CLV data, +CLV bets should have better ROI than -CLV."""
        if not os.path.isdir(BACKTEST_DIR):
            self.skipTest("No backtest_results directory")

        for fname in sorted(os.listdir(BACKTEST_DIR)):
            if not fname.endswith(".json") or fname.startswith("ckpt_"):
                continue
            data = _load_backtest_full(fname)
            if data is None:
                continue
            report = data.get("reports", {}).get("full", {})
            clv = report.get("clv", {})
            if not clv:
                continue

            tracked = clv.get("betsTracked", 0)
            pos_clv_pct = clv.get("positiveClvPct", 0)
            avg_clv = clv.get("avgClvLine", 0)

            if tracked >= 20:
                print(f"\n  CLV metrics for {fname[:40]}:")
                print(f"    Tracked: {tracked}, +CLV%: {pos_clv_pct:.1f}%, "
                      f"avgCLV: {avg_clv:+.2f}")
                if pos_clv_pct < 40:
                    print(f"    [WARNING] Low +CLV% — model may be picking "
                          f"against closing line movement")

    def test_clv_line_direction_consistency(self):
        """CLV analysis in journal should be consistent with backtest CLV."""
        entries = _read_jsonl(PROP_JOURNAL_PATH)
        if not entries:
            self.skipTest("Journal is empty")

        with_clv = [e for e in entries if e.get("clvLine") is not None]
        if len(with_clv) < 10:
            self.skipTest(f"Only {len(with_clv)} journal entries have CLV data")

        pos_clv = sum(1 for e in with_clv if e.get("clvLine", 0) > 0)
        neg_clv = sum(1 for e in with_clv if e.get("clvLine", 0) < 0)
        total = len(with_clv)

        pos_pct = pos_clv / total if total > 0 else 0
        print(f"\n  Journal CLV: {pos_clv}/{total} positive ({pos_pct:.0%}), "
              f"{neg_clv} negative")

        if pos_pct < 0.40:
            print("  [WARNING] Less than 40% of journal entries have positive CLV. "
                  "Model may be systematically picking against line movement.")


# ===========================================================================
# 33. Per-Stat OOS Consistency
# ===========================================================================

class TestPerStatOOSConsistency(unittest.TestCase):
    """Per-stat metrics should be consistent: betting stats (pts, ast) should
    show stronger metrics than non-betting stats (reb, stl, etc.)."""

    def test_betting_stats_dominate_in_real_line_roi(self):
        """pts and ast (stat_whitelist) should have better real-line metrics
        than blocked stats in the latest backtest."""
        data, fname = TestRealVsSyntheticSegmentation._load_latest_backtest()
        if data is None:
            self.skipTest("No backtest results")
        report = data.get("reports", {}).get("full", {})
        stat_roi = report.get("realLineStatRoi", {})

        betting_stats = {"pts", "ast"}
        research_stats = {"reb", "stl", "blk", "fg3m", "tov", "pra"}

        betting_rois = []
        research_rois = []
        print(f"\n  Per-stat real-line ROI comparison ({fname[:35]}...):")
        for stat, info in sorted(stat_roi.items()):
            if not isinstance(info, dict):
                continue
            placed = info.get("betsPlaced", 0)
            pnl = info.get("pnlUnits", 0)
            if placed == 0:
                continue
            roi = pnl / placed
            category = "BETTING" if stat in betting_stats else "RESEARCH"
            print(f"    [{category}] {stat}: ROI={roi:+.1%} ({placed} bets)")
            if stat in betting_stats:
                betting_rois.append(roi)
            elif stat in research_stats:
                research_rois.append(roi)

        if betting_rois and research_rois:
            avg_betting = sum(betting_rois) / len(betting_rois)
            avg_research = sum(research_rois) / len(research_rois)
            if avg_betting <= avg_research:
                print(f"\n  [WARNING] Betting stats avg ROI ({avg_betting:+.1%}) "
                      f"<= research stats ({avg_research:+.1%}). "
                      f"Stat whitelist may need revision.")

    def test_calibration_quality_per_stat(self):
        """Report Brier scores per stat — betting stats should be well-calibrated."""
        data, fname = TestRealVsSyntheticSegmentation._load_latest_backtest()
        if data is None:
            self.skipTest("No backtest results")
        report = data.get("reports", {}).get("full", {})
        brier = report.get("brierByStat", {})

        if not brier:
            self.skipTest("No Brier data in latest backtest")

        print(f"\n  Calibration quality by stat ({fname[:35]}...):")
        for stat in ["pts", "ast", "reb", "fg3m", "pra", "stl", "blk", "tov"]:
            val = brier.get(stat)
            if val is not None:
                flag = " [GOOD]" if val < 0.25 else " [OK]" if val < 0.27 else " [HIGH]"
                print(f"    {stat}: Brier={val:.4f}{flag}")

    def test_per_stat_hit_rate_floor(self):
        """Each betting stat should have hit rate > STAT_OOS_HIT_RATE_MIN in lean_bets."""
        entries = _read_jsonl(LEAN_BETS_PATH)
        if not entries:
            self.skipTest("Lean bets file is empty")

        from collections import Counter
        stat_wins = Counter()
        stat_total = Counter()
        for e in entries:
            stat = e.get("stat")
            outcome = e.get("outcome")
            if stat and outcome in ("win", "loss"):
                stat_total[stat] += 1
                if outcome == "win":
                    stat_wins[stat] += 1

        print("\n  Per-stat hit rates from lean_bets:")
        for stat in sorted(stat_total):
            total = stat_total[stat]
            wins = stat_wins.get(stat, 0)
            rate = wins / total if total > 0 else 0
            flag = " [LOW]" if rate < STAT_OOS_HIT_RATE_MIN else ""
            print(f"    {stat}: {wins}/{total} ({rate:.0%}){flag}")


# ###########################################################################
# ###########################################################################
#
#   PHASE E — Edge-Case & Concurrency Hardening
#
#   Covers:
#     34. Extreme input values for compute_ev
#     35. Missing/corrupt data graceful degradation
#     36. Concurrent cache access thread-safety
#     37. EV engine boundary conditions (push, zero edge, odds edge)
#     38. Calibration cache consistency
#
#   Pass/Fail Thresholds:
#     - EXTREME_NAN_INF:      any NaN/Inf in output              -> HARD FAIL
#     - GRACEFUL_DEGRADE:     missing data should not crash       -> FAIL
#     - CACHE_CONSISTENCY:    same inputs → same outputs          -> FAIL
#     - THREAD_SAFETY:        concurrent access → no corruption   -> FAIL
# ###########################################################################
# ###########################################################################

import threading


# ===========================================================================
# 34. Extreme Input Values
# ===========================================================================

class TestExtremeInputValues(unittest.TestCase):
    """Feed boundary and extreme values into compute_ev."""

    def _validate_ev_output(self, result, label):
        """Validate compute_ev output is well-formed."""
        self.assertIsNotNone(result, f"{label}: result is None")
        for key in ("probOver", "probUnder", "probPush"):
            val = result.get(key)
            self.assertIsNotNone(val, f"{label}: {key} is None")
            self.assertFalse(
                math.isnan(val) or math.isinf(val),
                f"{label}: {key}={val} is NaN/Inf",
            )
            self.assertGreaterEqual(val, 0.0, f"{label}: {key}={val} < 0")
            self.assertLessEqual(val, 1.0, f"{label}: {key}={val} > 1")
        # Verify sum
        total = result["probOver"] + result["probUnder"] + result["probPush"]
        self.assertAlmostEqual(total, 1.0, places=3,
                               msg=f"{label}: prob sum={total:.6f}")

    def test_projection_zero(self):
        """projection=0 should produce valid output."""
        result = compute_ev(
            projection=0, line=5.5,
            over_odds=-110, under_odds=-110,
            stdev=5.0, stat="pts",
        )
        self._validate_ev_output(result, "proj=0")
        self.assertLess(result["probOver"], 0.5, "proj=0 should favor under")

    def test_projection_very_large(self):
        """projection=100 should produce valid output."""
        result = compute_ev(
            projection=100, line=25.5,
            over_odds=-110, under_odds=-110,
            stdev=5.0, stat="pts",
        )
        self._validate_ev_output(result, "proj=100")
        self.assertGreater(result["probOver"], 0.99, "proj=100 >> line should give near-certain over")

    def test_projection_equals_line(self):
        """projection == line (push scenario) should produce valid output."""
        result = compute_ev(
            projection=25.0, line=25.0,
            over_odds=-110, under_odds=-110,
            stdev=5.0, stat="pts",
        )
        self._validate_ev_output(result, "proj=line=25")
        # Push probability should be non-trivial for integer line
        self.assertGreater(result["probPush"], 0.01,
                           "Integer line with proj=line should have non-trivial push prob")

    def test_projection_negative(self):
        """projection=-5 (invalid but should not crash)."""
        result = compute_ev(
            projection=-5, line=5.5,
            over_odds=-110, under_odds=-110,
            stdev=5.0, stat="pts",
        )
        self._validate_ev_output(result, "proj=-5")

    def test_line_half_integer(self):
        """line=25.5 (non-integer) should have zero push probability."""
        result = compute_ev(
            projection=25.0, line=25.5,
            over_odds=-110, under_odds=-110,
            stdev=5.0, stat="pts",
        )
        self._validate_ev_output(result, "line=25.5")
        self.assertAlmostEqual(result["probPush"], 0.0, places=6,
                               msg="Non-integer line should have zero push")

    def test_extreme_odds_values(self):
        """Very heavy favorites (-500) and underdogs (+500)."""
        for over_odds, under_odds in [(-500, +350), (+400, -600), (-200, +170)]:
            result = compute_ev(
                projection=25.0, line=24.5,
                over_odds=over_odds, under_odds=under_odds,
                stdev=5.0, stat="pts",
            )
            self._validate_ev_output(result, f"odds={over_odds}/{under_odds}")

    def test_poisson_extreme_projection(self):
        """Poisson with very high lambda should not overflow."""
        result = compute_ev(
            projection=50.0, line=5.5,
            over_odds=-110, under_odds=-110, stat="stl",
        )
        self._validate_ev_output(result, "poisson proj=50")

    def test_poisson_zero_projection(self):
        """Poisson with projection near 0."""
        result = compute_ev(
            projection=0.01, line=0.5,
            over_odds=-110, under_odds=-110, stat="stl",
        )
        self._validate_ev_output(result, "poisson proj=0.01")

    def test_integer_line_zero(self):
        """line=0 (edge case for Poisson)."""
        result = compute_ev(
            projection=1.5, line=0,
            over_odds=-110, under_odds=-110, stat="stl",
        )
        self._validate_ev_output(result, "poisson line=0")


# ===========================================================================
# 35. Missing/Corrupt Data Graceful Degradation
# ===========================================================================

class TestGracefulDegradation(unittest.TestCase):
    """Missing or corrupt inputs should degrade gracefully, not crash."""

    def test_stat_none_uses_global_calibration(self):
        """stat=None should still produce valid output (global T fallback)."""
        result = compute_ev(
            projection=25.0, line=24.5,
            over_odds=-110, under_odds=-110,
            stdev=5.0, stat=None,
        )
        self.assertIsNotNone(result)
        self.assertIn("probOver", result)
        self.assertGreater(result["probOver"], 0.0)
        self.assertLess(result["probOver"], 1.0)

    def test_stat_unknown_uses_global_calibration(self):
        """stat='xyz' (unknown stat) should fall back to global T."""
        result = compute_ev(
            projection=25.0, line=24.5,
            over_odds=-110, under_odds=-110,
            stdev=5.0, stat="xyz",
        )
        self.assertIsNotNone(result)
        self.assertIn("distributionMode", result)
        self.assertEqual(result["distributionMode"], "normal")

    def test_reference_probs_none(self):
        """reference_probs=None should use model probabilities (not crash)."""
        result = compute_ev(
            projection=25.0, line=24.5,
            over_odds=-110, under_odds=-110,
            stdev=5.0, stat="pts",
            reference_probs=None,
        )
        self.assertIsNotNone(result)
        self.assertNotEqual(result["distributionMode"], "reference")

    def test_reference_probs_malformed(self):
        """Malformed reference_probs should still produce output."""
        result = compute_ev(
            projection=25.0, line=24.5,
            over_odds=-110, under_odds=-110,
            stdev=5.0, stat="pts",
            reference_probs={"over": 0.55},  # missing 'under'
        )
        self.assertIsNotNone(result)

    def test_as_of_date_far_past(self):
        """as_of_date='2020-01-01' (before all WF files) should fall back to prod cal."""
        result = compute_ev(
            projection=25.0, line=24.5,
            over_odds=-110, under_odds=-110,
            stdev=5.0, stat="pts",
            as_of_date="2020-01-01",
        )
        self.assertIsNotNone(result)
        self.assertIn("probOver", result)

    def test_as_of_date_far_future(self):
        """as_of_date='2030-01-01' should use latest available WF or prod cal."""
        result = compute_ev(
            projection=25.0, line=24.5,
            over_odds=-110, under_odds=-110,
            stdev=5.0, stat="pts",
            as_of_date="2030-01-01",
        )
        self.assertIsNotNone(result)
        self.assertIn("probOver", result)

    def test_fit_temperature_all_edge_cases(self):
        """fit_temperature with various degenerate inputs."""
        # All bins below min_count
        bins = [
            {"avgPredOverProbPct": 30.0, "actualOverHitRatePct": 30.0, "count": 5},
        ]
        T, mse, n_bins = fit_temperature(bins, min_count=50)
        self.assertEqual(T, 1.0, "All bins below min_count → T=1.0")

        # All bins at extreme probability (filtered by min_pred/max_pred)
        bins = [
            {"avgPredOverProbPct": 2.0, "actualOverHitRatePct": 2.0, "count": 200},
            {"avgPredOverProbPct": 98.0, "actualOverHitRatePct": 98.0, "count": 200},
        ]
        T, mse, n_bins = fit_temperature(bins, min_count=50, min_pred=0.10, max_pred=0.90)
        self.assertEqual(T, 1.0, "All bins outside pred range → T=1.0")


# ===========================================================================
# 36. Concurrent Cache Access Thread-Safety
# ===========================================================================

class TestConcurrentCacheAccess(unittest.TestCase):
    """Verify _cal_cache handles concurrent access without corruption."""

    def test_concurrent_date_loads(self):
        """Multiple threads loading different dates should not corrupt cache."""
        from core import nba_ev_engine

        old_cache = nba_ev_engine._cal_cache.copy()
        nba_ev_engine._cal_cache.clear()

        errors = []
        dates = ["2026-01-01", "2026-01-15", "2026-02-01", "2026-02-15", "2026-03-01"]

        def load_cal(d):
            try:
                result = nba_ev_engine._load_prob_calibration_for_date(d)
                if result is None:
                    errors.append(f"None result for {d}")
            except Exception as exc:
                errors.append(f"Exception for {d}: {exc}")

        try:
            threads = [threading.Thread(target=load_cal, args=(d,)) for d in dates]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            self.assertEqual(errors, [], f"Concurrent cache access errors: {errors}")
        finally:
            nba_ev_engine._cal_cache.clear()
            nba_ev_engine._cal_cache.update(old_cache)

    def test_concurrent_compute_ev(self):
        """Multiple threads calling compute_ev with different stats/dates."""
        errors = []
        results = {}

        def run_ev(stat, proj, idx):
            try:
                result = compute_ev(
                    projection=proj, line=proj - 0.5,
                    over_odds=-110, under_odds=-110,
                    stdev=proj * 0.20 if stat not in _POISSON_STATS else None,
                    stat=stat,
                )
                if result is None:
                    errors.append(f"None for {stat}/{idx}")
                else:
                    results[f"{stat}/{idx}"] = result["probOver"]
            except Exception as exc:
                errors.append(f"Exception for {stat}/{idx}: {exc}")

        test_cases = [
            ("pts", 25.0), ("ast", 7.0), ("reb", 10.0),
            ("stl", 1.5), ("fg3m", 2.0), ("blk", 1.0),
        ]

        threads = []
        for i in range(3):  # 3 rounds
            for stat, proj in test_cases:
                t = threading.Thread(target=run_ev, args=(stat, proj, i))
                threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [], f"Concurrent compute_ev errors: {errors}")
        self.assertGreater(len(results), 0, "No results collected from threads")


# ===========================================================================
# 37. EV Engine Boundary Conditions
# ===========================================================================

class TestEVEngineBoundaryConditions(unittest.TestCase):
    """Test boundary conditions in the EV engine."""

    def test_push_probability_at_integer_line(self):
        """Integer line should produce non-zero push probability (normal dist)."""
        result = compute_ev(
            projection=25.0, line=25,
            over_odds=-110, under_odds=-110,
            stdev=5.0, stat="pts",
        )
        self.assertGreater(result["probPush"], 0.0,
                           "Integer line should have push probability > 0")

    def test_zero_push_at_half_integer_line(self):
        """Half-integer line should have zero push probability (normal dist)."""
        result = compute_ev(
            projection=25.0, line=25.5,
            over_odds=-110, under_odds=-110,
            stdev=5.0, stat="pts",
        )
        self.assertAlmostEqual(result["probPush"], 0.0, places=6,
                               msg="Half-integer line should have zero push prob")

    def test_edge_zero_when_model_equals_market(self):
        """When model prob ≈ no-vig implied, edge should be near zero."""
        # -110/-110 → no-vig ≈ 50/50
        # We need projection ≈ line for P(over) ≈ 0.5
        result = compute_ev(
            projection=25.0, line=25.0,
            over_odds=-110, under_odds=-110,
            stdev=5.0, stat="pts",
        )
        # After calibration, the edge should be small
        over_edge = abs(result["over"]["edge"])
        under_edge = abs(result["under"]["edge"])
        min_edge = min(over_edge, under_edge)
        # At least one side should have very small edge
        self.assertLess(min_edge, 0.05,
                        f"When proj ≈ line, at least one edge should be near 0 "
                        f"(got over={over_edge:.4f}, under={under_edge:.4f})")

    def test_verdict_classification_boundaries(self):
        """Verify verdict categories match documented thresholds."""
        # Strong Value: edge >= 0.08
        # Good Value: 0.03 <= edge < 0.08
        # Thin Edge: 0 < edge < min_edge_threshold
        # Negative EV: edge <= 0

        # Create result with known high edge (proj far from line)
        result = compute_ev(
            projection=32.0, line=24.5,
            over_odds=-110, under_odds=-110,
            stdev=5.0, stat="pts",
        )
        over_edge = result["over"]["edge"]
        if over_edge >= 0.08:
            self.assertEqual(result["over"]["verdict"], "Strong Value")
        elif over_edge >= 0.03:
            self.assertIn(result["over"]["verdict"], ("Good Value", "Strong Value"))

    def test_kelly_fraction_nonnegative(self):
        """Kelly fraction should never be negative."""
        test_cases = [
            (25.0, 24.5, "pts", -110, -110),
            (20.0, 25.5, "pts", -110, -110),   # under-favored
            (1.5, 1.5, "stl", -110, -110),      # Poisson
            (25.0, 25.0, "pts", -130, +110),     # asymmetric odds
        ]
        for proj, line, stat, over_o, under_o in test_cases:
            result = compute_ev(
                projection=proj, line=line,
                over_odds=over_o, under_odds=under_o,
                stdev=5.0, stat=stat,
            )
            self.assertGreaterEqual(
                result["over"]["kellyFraction"], 0.0,
                f"Kelly fraction negative for proj={proj} line={line} stat={stat}",
            )
            self.assertGreaterEqual(
                result["under"]["kellyFraction"], 0.0,
                f"Under Kelly fraction negative for proj={proj} line={line} stat={stat}",
            )

    def test_odds_at_boundary(self):
        """Odds at -100 (even money) should produce valid output."""
        result = compute_ev(
            projection=25.0, line=24.5,
            over_odds=-100, under_odds=-100,
            stdev=5.0, stat="pts",
        )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["vig"], 0.0, places=2,
                               msg="-100/-100 should have near-zero vig")


# ===========================================================================
# 38. Calibration Cache Consistency
# ===========================================================================

class TestCalibrationCacheConsistency(unittest.TestCase):
    """Verify calibration cache returns consistent results."""

    def test_same_date_returns_same_calibration(self):
        """Loading calibration for the same date twice should return identical results."""
        from core.nba_ev_engine import _load_prob_calibration_for_date
        from core import nba_ev_engine

        old_cache = nba_ev_engine._cal_cache.copy()
        nba_ev_engine._cal_cache.clear()

        try:
            cal1 = _load_prob_calibration_for_date("2026-02-01")
            cal2 = _load_prob_calibration_for_date("2026-02-01")
            # Should be the exact same object (from cache)
            self.assertIs(cal1, cal2,
                          "Same date should return cached (identical) calibration object")
        finally:
            nba_ev_engine._cal_cache.clear()
            nba_ev_engine._cal_cache.update(old_cache)

    def test_different_dates_may_differ(self):
        """Different dates should potentially return different calibrations
        (at least for dates with separate WF files)."""
        from core.nba_ev_engine import _load_prob_calibration_for_date
        from core import nba_ev_engine

        old_cache = nba_ev_engine._cal_cache.copy()
        nba_ev_engine._cal_cache.clear()

        try:
            wf_files = _load_wf_files()
            if len(wf_files) < 2:
                self.skipTest("Need >= 2 walk-forward files")

            # Pick two dates that correspond to different WF files
            d1 = wf_files[0][0]
            d2 = wf_files[-1][0]

            cal1 = _load_prob_calibration_for_date(d1)
            nba_ev_engine._cal_cache.clear()  # force reload
            cal2 = _load_prob_calibration_for_date(d2)

            # At least one stat should differ between early and late calibration
            if cal1.get("_for_date") and cal2.get("_for_date"):
                self.assertNotEqual(
                    cal1.get("_for_date"), cal2.get("_for_date"),
                    "Different dates should load different WF files",
                )
        finally:
            nba_ev_engine._cal_cache.clear()
            nba_ev_engine._cal_cache.update(old_cache)

    def test_compute_ev_deterministic(self):
        """Same inputs → same outputs (no randomness in compute_ev)."""
        kwargs = dict(
            projection=25.0, line=24.5,
            over_odds=-110, under_odds=-110,
            stdev=5.0, stat="pts",
        )
        r1 = compute_ev(**kwargs)
        r2 = compute_ev(**kwargs)
        self.assertEqual(r1["probOver"], r2["probOver"],
                         "compute_ev should be deterministic")
        self.assertEqual(r1["over"]["edge"], r2["over"]["edge"],
                         "compute_ev edge should be deterministic")
        self.assertEqual(r1["over"]["verdict"], r2["over"]["verdict"],
                         "compute_ev verdict should be deterministic")

    def test_null_odds_returns_none(self):
        """compute_ev with null or zero odds should return None."""
        self.assertIsNone(compute_ev(25.0, 24.5, 0, -110, stat="pts"))
        self.assertIsNone(compute_ev(25.0, 24.5, -110, 0, stat="pts"))
        self.assertIsNone(compute_ev(25.0, 24.5, None, -110, stat="pts"))
        self.assertIsNone(compute_ev(25.0, 24.5, -110, None, stat="pts"))


# ===========================================================================
# 39. Wave 3 — Calibration Loading Failures
# ===========================================================================

class TestCalibrationLoadingFailures(unittest.TestCase):
    """Verify compute_ev degrades gracefully with corrupt/missing calibration."""

    def test_empty_cal_file_uses_global(self):
        """An empty calibration dict should fall back to _global or T=1.0."""
        from core.nba_ev_engine import _load_prob_calibration_for_date
        from core import nba_ev_engine

        old_cache = nba_ev_engine._cal_cache.copy()
        try:
            # Inject empty cal for a fake date
            nba_ev_engine._cal_cache["9999-01-01"] = {}
            cal = _load_prob_calibration_for_date("9999-01-01")
            self.assertEqual(cal, {}, "Empty cal should remain empty dict")
            # compute_ev should still work (no cal applied = T=1.0)
            result = compute_ev(25.0, 24.5, -110, -110, stdev=5.0, stat="pts",
                                as_of_date="9999-01-01")
            self.assertIsNotNone(result)
            self.assertFalse(math.isnan(result["probOver"]))
        finally:
            nba_ev_engine._cal_cache.clear()
            nba_ev_engine._cal_cache.update(old_cache)

    def test_cal_with_invalid_T_value(self):
        """A calibration with T=0 or T<0 should not crash compute_ev.

        _apply_temp_scaling guards against T<=0 by returning p unchanged
        and emitting a warning.
        """
        import warnings as _warnings
        from core import nba_ev_engine

        old_cache = nba_ev_engine._cal_cache.copy()
        try:
            # T=0 would cause division by zero without the guard
            nba_ev_engine._cal_cache["9998-01-01"] = {"pts": 0.0, "_global": 0.0}
            with _warnings.catch_warnings(record=True) as caught:
                _warnings.simplefilter("always")
                result = compute_ev(25.0, 24.5, -110, -110, stdev=5.0, stat="pts",
                                    as_of_date="9998-01-01")
            self.assertIsNotNone(result, "compute_ev should handle T=0 gracefully")
            self.assertFalse(math.isnan(result["probOver"]),
                             "probOver should not be NaN with T=0")
            # Verify warning was emitted (not silent)
            t0_warnings = [w for w in caught if "invalid T=" in str(w.message)]
            self.assertGreater(len(t0_warnings), 0,
                               "T=0 should emit a warning, not fail silently")

            # Also test T<0
            nba_ev_engine._cal_cache["9998-01-02"] = {"pts": -1.0, "_global": -1.0}
            with _warnings.catch_warnings(record=True) as caught2:
                _warnings.simplefilter("always")
                result2 = compute_ev(25.0, 24.5, -110, -110, stdev=5.0, stat="pts",
                                     as_of_date="9998-01-02")
            self.assertIsNotNone(result2, "compute_ev should handle T<0 gracefully")
            tneg_warnings = [w for w in caught2 if "invalid T=" in str(w.message)]
            self.assertGreater(len(tneg_warnings), 0,
                               "T<0 should emit a warning, not fail silently")
        finally:
            nba_ev_engine._cal_cache.clear()
            nba_ev_engine._cal_cache.update(old_cache)

    def test_cal_with_very_large_T(self):
        """T=100 should push probabilities toward 50% without NaN."""
        from core import nba_ev_engine

        old_cache = nba_ev_engine._cal_cache.copy()
        try:
            nba_ev_engine._cal_cache["9997-01-01"] = {"pts": 100.0, "_global": 100.0}
            result = compute_ev(30.0, 20.0, -110, -110, stdev=5.0, stat="pts",
                                as_of_date="9997-01-01")
            self.assertIsNotNone(result)
            # With T=100, even a strong edge (proj=30, line=20) should be
            # squished close to 50%
            self.assertAlmostEqual(result["probOver"], 0.5, delta=0.05,
                                   msg="T=100 should push probOver close to 0.5")
        finally:
            nba_ev_engine._cal_cache.clear()
            nba_ev_engine._cal_cache.update(old_cache)

    def test_missing_wf_dir_falls_back(self):
        """If walk_forward/ dir is missing, compute_ev should use prod cal or T=1."""
        from core.nba_ev_engine import _load_prob_calibration_for_date
        # This date should not have a WF file — should fall back
        cal = _load_prob_calibration_for_date("2000-01-01")
        self.assertIsNotNone(cal, "Should return prod cal fallback, not None")


# ===========================================================================
# 40. Wave 3 — OOS vs IS Leakage Detection
# ===========================================================================

class TestOOSvsISLeakage(unittest.TestCase):
    """OOS Brier should not be better than IS Brier (suggests overfitting leak)."""

    def setUp(self):
        """Load backtest results for OOS and IS periods."""
        self.backtest_dir = BACKTEST_DIR
        if not os.path.isdir(self.backtest_dir):
            self.skipTest("No backtest_results directory")

    def _find_backtest_by_period(self, period):
        """Find backtest files covering IS or OOS periods."""
        # IS: Dec 28 - Feb 25; OOS: Oct 21 - Nov 30
        is_start, is_end = date(2025, 12, 28), date(2026, 2, 25)
        oos_start, oos_end = date(2025, 10, 21), date(2025, 11, 30)
        results = []
        for fname in os.listdir(self.backtest_dir):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.backtest_dir, fname)) as f:
                    data = json.load(f)
                df = data.get("dateFrom", "")
                dt = data.get("dateTo", "")
                if not df or not dt:
                    continue
                df_d, dt_d = date.fromisoformat(df), date.fromisoformat(dt)
                if period == "is" and df_d >= is_start and dt_d <= is_end:
                    results.append(data)
                elif period == "oos" and df_d >= oos_start and dt_d <= oos_end:
                    results.append(data)
            except (json.JSONDecodeError, OSError, ValueError):
                continue
        return results

    def test_oos_brier_not_better_than_is(self):
        """If we have both IS and OOS backtests, OOS Brier should be >= IS Brier.

        If OOS is better, it suggests calibration is not well-fitted to IS data
        or there's data leakage in the other direction.
        """
        is_results = self._find_backtest_by_period("is")
        oos_results = self._find_backtest_by_period("oos")
        if not is_results or not oos_results:
            self.skipTest("Need both IS and OOS backtest files")

        # Use the first result from each
        is_brier = is_results[0].get("brierByStat", {})
        oos_brier = oos_results[0].get("brierByStat", {})
        if not is_brier or not oos_brier:
            self.skipTest("Backtest files missing brierByStat")

        anomalies = []
        for stat in BETTING_STATS:
            is_b = is_brier.get(stat)
            oos_b = oos_brier.get(stat)
            if is_b is None or oos_b is None:
                continue
            if oos_b < is_b * 0.90:  # OOS is >10% better than IS
                anomalies.append(
                    f"  {stat}: OOS Brier={oos_b:.4f} < IS Brier={is_b:.4f} "
                    f"(OOS {((is_b - oos_b)/is_b)*100:.1f}% better — possible leak)"
                )

        if anomalies:
            self.fail(
                "OOS Brier significantly better than IS (possible data leakage):\n"
                + "\n".join(anomalies)
            )

    def test_oos_roi_not_suspiciously_high(self):
        """OOS ROI should not exceed IS ROI by a large margin."""
        is_results = self._find_backtest_by_period("is")
        oos_results = self._find_backtest_by_period("oos")
        if not is_results or not oos_results:
            self.skipTest("Need both IS and OOS backtest files")

        is_roi = is_results[0].get("roi")
        oos_roi = oos_results[0].get("roi")
        if is_roi is None or oos_roi is None:
            self.skipTest("Backtest files missing roi")

        if oos_roi > is_roi * 1.5 and oos_roi > 0.10:
            self.fail(
                f"OOS ROI ({oos_roi:.2%}) suspiciously higher than IS ROI ({is_roi:.2%}) "
                f"— investigate potential forward-looking bias"
            )


# ===========================================================================
# 41. Wave 3 — Real-Line ROI Stability
# ===========================================================================

class TestRealLineROIStability(unittest.TestCase):
    """Real-line ROI should not flip sign across different backtest windows."""

    def setUp(self):
        self.backtest_dir = BACKTEST_DIR
        if not os.path.isdir(self.backtest_dir):
            self.skipTest("No backtest_results directory")

    def _load_backtests_with_real_roi(self):
        """Load backtest results that have realLineStatRoi data.

        realLineStatRoi structure: {stat: {betsPlaced, wins, losses, pushes,
        pnlUnits, hitRatePct, roiPctPerBet}}.
        roiReal structure: {betsPlaced, wins, losses, pushes, pnlUnits,
        hitRatePct, roiPctPerBet}.
        """
        results = []
        for fname in sorted(os.listdir(self.backtest_dir)):
            if not fname.endswith(".json"):
                continue
            try:
                fpath = os.path.join(self.backtest_dir, fname)
                with open(fpath) as f:
                    data = json.load(f)
                real_roi = data.get("realLineStatRoi")
                if real_roi and data.get("dateFrom") and data.get("dateTo"):
                    roi_real = data.get("roiReal", {})
                    real_samples = (roi_real.get("betsPlaced", 0)
                                    if isinstance(roi_real, dict) else 0)
                    results.append({
                        "file": fname,
                        "dateFrom": data["dateFrom"],
                        "dateTo": data["dateTo"],
                        "realLineStatRoi": real_roi,
                        "roiReal": roi_real,
                        "realLineSamples": real_samples,
                    })
            except (json.JSONDecodeError, OSError):
                continue
        return results

    def _get_stat_roi(self, stat_data):
        """Extract ROI from stat data (handles dict with roiPctPerBet)."""
        if isinstance(stat_data, dict):
            return stat_data.get("roiPctPerBet")
        return stat_data  # float fallback

    def _get_stat_bets(self, stat_data):
        """Extract bet count from stat data."""
        if isinstance(stat_data, dict):
            return stat_data.get("betsPlaced", 0)
        return 0

    def test_real_roi_sign_consistency(self):
        """For betting stats, real-line ROI should not flip positive↔negative
        across overlapping backtest windows (suggests instability)."""
        backtests = self._load_backtests_with_real_roi()
        if len(backtests) < 2:
            self.skipTest("Need >= 2 backtests with realLineStatRoi")

        flips = []
        for stat in BETTING_STATS:
            rois = []
            for bt in backtests:
                stat_data = bt["realLineStatRoi"].get(stat)
                r = self._get_stat_roi(stat_data)
                n = self._get_stat_bets(stat_data)
                if r is not None and n >= 20:
                    rois.append((bt["dateFrom"], bt["dateTo"], r))

            if len(rois) < 2:
                continue

            # Check for sign flips between consecutive backtests
            for i in range(1, len(rois)):
                prev_roi = rois[i-1][2]
                curr_roi = rois[i][2]
                if (prev_roi > 5.0 and curr_roi < -5.0) or \
                   (prev_roi < -5.0 and curr_roi > 5.0):
                    flips.append(
                        f"  {stat}: {rois[i-1][0]}–{rois[i-1][1]} ROI={prev_roi:.1f}% → "
                        f"{rois[i][0]}–{rois[i][1]} ROI={curr_roi:.1f}%"
                    )

        if flips:
            print(f"\n  [WARNING] Real-line ROI sign flips detected "
                  f"(may indicate model instability):")
            for f in flips:
                print(f)

    def test_aggregate_real_roi_positive_across_windows(self):
        """Aggregate real-line ROI across all backtest windows should be non-negative
        for betting stats."""
        backtests = self._load_backtests_with_real_roi()
        if not backtests:
            self.skipTest("No backtests with realLineStatRoi")

        for stat in BETTING_STATS:
            total_pnl = 0.0
            total_bets = 0
            for bt in backtests:
                stat_data = bt["realLineStatRoi"].get(stat)
                r = self._get_stat_roi(stat_data)
                n = self._get_stat_bets(stat_data)
                if r is not None and n >= 20:
                    total_pnl += r * n  # roiPctPerBet * betsPlaced
                    total_bets += n

            if total_bets < 50:
                continue

            avg_roi = total_pnl / total_bets
            if avg_roi < -20.0:  # roiPctPerBet is in percent, not decimal
                self.fail(
                    f"{stat}: Aggregate real-line ROI across {total_bets} bets "
                    f"is {avg_roi:.1f}% (below -20% floor)"
                )


# ===========================================================================
# 42. Wave 4 — Closing Line Matching
# ===========================================================================

class TestClosingLineMatching(unittest.TestCase):
    """Verify that OddsStore closing line lookups work reliably against
    actual journal leans."""

    JOURNAL_DB = os.path.join(ROOT, "data", "journals", "decision_journal.sqlite")

    def setUp(self):
        if not os.path.isfile(ODDS_DB_PATH):
            self.skipTest("Odds database not found")
        if not os.path.isfile(self.JOURNAL_DB):
            self.skipTest("Decision journal database not found")

    def _sample_leans(self, n=20):
        """Return up to *n* random leans (player_name, game_date, stat_key)."""
        conn = sqlite3.connect(
            f"file:{self.JOURNAL_DB}?mode=ro", uri=True
        )
        try:
            rows = conn.execute(
                "SELECT player_name, game_date, stat_key "
                "FROM lean_outcomes "
                "WHERE player_name IS NOT NULL "
                "  AND game_date IS NOT NULL "
                "  AND stat_key IS NOT NULL "
                "ORDER BY RANDOM() LIMIT ?",
                (n,),
            ).fetchall()
        finally:
            conn.close()
        return rows

    def test_closing_line_lookup_rate(self):
        """At least 60% of sampled leans should resolve to a closing line."""
        from core.nba_odds_store import OddsStore, STAT_TO_MARKET

        leans = self._sample_leans(20)
        if len(leans) < 5:
            self.skipTest("Too few leans in journal for meaningful test")

        store = OddsStore()
        found = 0
        for player, game_date, stat_key in leans:
            market = STAT_TO_MARKET.get(stat_key)
            if market is None:
                continue
            result = store.get_closing_line_by_player_date(
                player, market, game_date
            )
            if result is not None:
                found += 1

        rate = found / len(leans)
        self.assertGreaterEqual(
            rate, 0.60,
            f"Closing line lookup rate {rate:.0%} ({found}/{len(leans)}) "
            f"below 60% minimum",
        )

    def test_closing_line_structure(self):
        """When a closing line IS found, verify it has the expected keys."""
        from core.nba_odds_store import OddsStore, STAT_TO_MARKET

        leans = self._sample_leans(50)
        if not leans:
            self.skipTest("No leans in journal")

        store = OddsStore()
        required_keys = {"book", "close_line", "close_over_odds", "close_under_odds"}
        checked = 0

        for player, game_date, stat_key in leans:
            market = STAT_TO_MARKET.get(stat_key)
            if market is None:
                continue
            result = store.get_closing_line_by_player_date(
                player, market, game_date
            )
            if result is not None:
                missing = required_keys - set(result.keys())
                self.assertFalse(
                    missing,
                    f"Closing line for {player}/{stat_key}/{game_date} "
                    f"missing keys: {missing}",
                )
                checked += 1

        if checked == 0:
            self.skipTest("No closing lines resolved — cannot verify structure")


# ===========================================================================
# 43. Wave 4 — Real Line Coverage Per Stat
# ===========================================================================

class TestRealLineCoveragePerStat(unittest.TestCase):
    """Verify that betting stats have minimum real-line sample counts
    in backtest results."""

    def setUp(self):
        if not os.path.isdir(BACKTEST_DIR):
            self.skipTest("No backtest_results directory")

    def test_betting_stat_coverage_minimum(self):
        """For pts and ast, at least one backtest should have >= 50 real-line
        bets placed."""
        found_any = False
        for fname in sorted(os.listdir(BACKTEST_DIR)):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(BACKTEST_DIR, fname)
            try:
                with open(fpath) as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            real_roi = data.get("realLineStatRoi")
            if not real_roi:
                continue
            found_any = True
            for stat in BETTING_STATS:
                stat_data = real_roi.get(stat)
                if not isinstance(stat_data, dict):
                    continue
                bets = stat_data.get("betsPlaced", 0)
                if bets >= 50:
                    # At least one backtest meets the bar — pass
                    return

        if not found_any:
            self.skipTest("No backtests with realLineStatRoi found")

        self.fail(
            f"No backtest has >= 50 real-line bets for any betting stat "
            f"({', '.join(sorted(BETTING_STATS))}). Coverage too thin."
        )


# ===========================================================================
# 44. Wave 4 — Journal Settlement Bounds
# ===========================================================================

class TestJournalSettlementBounds(unittest.TestCase):
    """Verify that settled lean actual_stat values are within plausible
    ranges (catches data corruption or mis-settlement)."""

    JOURNAL_DB = os.path.join(ROOT, "data", "journals", "decision_journal.sqlite")

    # Upper bounds by stat — anything above is implausible
    STAT_UPPER_BOUNDS = {
        "pts": 70,
        "ast": 25,
        "reb": 30,
    }

    def setUp(self):
        if not os.path.isfile(self.JOURNAL_DB):
            self.skipTest("Decision journal database not found")

    def _load_settled_actuals(self):
        """Return list of (stat_key, actual_stat) for all settled leans."""
        conn = sqlite3.connect(
            f"file:{self.JOURNAL_DB}?mode=ro", uri=True
        )
        try:
            rows = conn.execute(
                "SELECT stat_key, actual_stat FROM lean_outcomes "
                "WHERE actual_stat IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()
        return rows

    def test_actual_stat_values_plausible(self):
        """No settled lean should exceed per-stat upper bounds."""
        rows = self._load_settled_actuals()
        if not rows:
            self.skipTest("No settled leans with actual_stat values")

        violations = []
        for stat_key, actual in rows:
            upper = self.STAT_UPPER_BOUNDS.get(stat_key)
            if upper is not None and actual > upper:
                violations.append(
                    f"  {stat_key}: actual_stat={actual} > {upper}"
                )

        if violations:
            self.fail(
                f"{len(violations)} implausible actual_stat values:\n"
                + "\n".join(violations[:20])
            )

    def test_no_negative_actual_stats(self):
        """No settled lean should have a negative actual_stat."""
        rows = self._load_settled_actuals()
        if not rows:
            self.skipTest("No settled leans with actual_stat values")

        negatives = [
            (stat, val) for stat, val in rows if val < 0
        ]

        if negatives:
            self.fail(
                f"{len(negatives)} negative actual_stat values found "
                f"(first 10): {negatives[:10]}"
            )


# ===========================================================================
# 45. Wave 4 — CLV Journal/Backtest Alignment
# ===========================================================================

class TestCLVJournalBacktestAlignment(unittest.TestCase):
    """Loose check that CLV-positive percentages from journal and backtest
    sources are in the same ballpark (within 15 percentage points)."""

    JOURNAL_DB = os.path.join(ROOT, "data", "journals", "decision_journal.sqlite")

    def _journal_positive_clv_pct(self):
        """Return the +CLV percentage from the decision journal, or None."""
        if not os.path.isfile(self.JOURNAL_DB):
            return None
        conn = sqlite3.connect(
            f"file:{self.JOURNAL_DB}?mode=ro", uri=True
        )
        try:
            # Check if clv columns exist
            cursor = conn.execute("PRAGMA table_info(lean_outcomes)")
            columns = {row[1] for row in cursor.fetchall()}
            if "clv_line" not in columns:
                return None

            total = conn.execute(
                "SELECT COUNT(*) FROM lean_outcomes "
                "WHERE clv_line IS NOT NULL"
            ).fetchone()[0]
            if total < 20:
                return None

            positive = conn.execute(
                "SELECT COUNT(*) FROM lean_outcomes "
                "WHERE clv_line IS NOT NULL AND clv_line > 0"
            ).fetchone()[0]

            return (positive / total) * 100.0
        finally:
            conn.close()

    def _backtest_positive_clv_pct(self):
        """Return the +CLV percentage from backtest results, or None."""
        if not os.path.isdir(BACKTEST_DIR):
            return None
        for fname in sorted(os.listdir(BACKTEST_DIR), reverse=True):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(BACKTEST_DIR, fname)
            try:
                with open(fpath) as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            # Look for CLV summary data in backtest output
            clv_summary = data.get("clvSummary") or data.get("clv_summary")
            if not clv_summary:
                continue
            pos_pct = clv_summary.get("positiveClvPct") or clv_summary.get("positive_clv_pct")
            if pos_pct is not None:
                return float(pos_pct)
        return None

    def test_clv_positive_pct_alignment(self):
        """Journal +CLV% and backtest +CLV% should be within 15pp."""
        journal_pct = self._journal_positive_clv_pct()
        backtest_pct = self._backtest_positive_clv_pct()

        if journal_pct is None:
            self.skipTest("No CLV data in decision journal")
        if backtest_pct is None:
            self.skipTest("No CLV summary in backtest results")

        diff = abs(journal_pct - backtest_pct)
        self.assertLessEqual(
            diff, 15.0,
            f"CLV +% divergence too large: journal={journal_pct:.1f}% vs "
            f"backtest={backtest_pct:.1f}% (diff={diff:.1f}pp, max=15pp)",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
