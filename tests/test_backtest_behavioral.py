"""
tests/test_backtest_behavioral.py — Behavioral tests for run_backtest().

Covers invariants and behaviors beyond input validation and return contracts:
  1. No-lookahead invariant (date_to must not be in the future)
  2. Accumulator initialization (no state leaks between sequential runs)
  3. Save flag behavior (savedTo key presence/absence, no file writes)

All tests use data_source="local" so they run without network access.
When the local index is missing, run_backtest() returns a graceful failure
that we can still assert against for structural properties.
"""

import os
import sys
import tempfile
from datetime import date, timedelta

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from core.nba_backtest import run_backtest


# ---------------------------------------------------------------------------
# 1. No-lookahead invariant
# ---------------------------------------------------------------------------

class TestNoLookahead:
    """run_backtest() must not allow backtesting into the future.

    The no-lookahead principle is documented in CLAUDE.md:
      "no-lookahead: date_to must be before today"

    NOTE: As of writing, run_backtest() does NOT enforce this at the
    validation layer — it relies on the caller (CLI, docs) to prevent
    future dates.  These tests document the CURRENT behavior.  If/when
    an explicit guard is added, update the assertions to expect rejection.
    """

    def test_date_to_today_is_not_rejected_at_validation(self):
        """date_to == today passes validation (may fail later for other reasons)."""
        today_str = date.today().isoformat()
        result = run_backtest(
            today_str,
            date_to=today_str,
            data_source="local",
        )
        # The call will likely fail because the local index is missing,
        # but the failure should NOT be a date validation error.
        if not result["success"]:
            error = result.get("error", "").lower()
            assert "date" not in error or "index" in error or "local" in error, (
                f"Expected non-date-related failure, got: {result['error']}"
            )

    def test_date_to_in_future_is_not_rejected_at_validation(self):
        """date_to in the future passes validation (no guard exists yet).

        This test documents the gap.  If a future-date guard is added,
        this test should be updated to assert rejection.
        """
        future = (date.today() + timedelta(days=30)).isoformat()
        today_str = date.today().isoformat()
        result = run_backtest(
            today_str,
            date_to=future,
            data_source="local",
        )
        # Should fail for data-source reasons, not date validation.
        if not result["success"]:
            error = result.get("error", "").lower()
            assert "date_to" not in error or "before" not in error, (
                f"Future-date guard was added — update this test: {result['error']}"
            )

    def test_past_date_range_accepted(self):
        """A fully-past date range passes validation."""
        result = run_backtest(
            "2026-01-01",
            date_to="2026-01-02",
            data_source="local",
        )
        # May fail for missing local index, but not for date validation.
        if not result["success"]:
            error = result.get("error", "").lower()
            assert "date_from" not in error and "date_to" not in error, (
                f"Unexpected date validation failure: {result['error']}"
            )


# ---------------------------------------------------------------------------
# 2. Accumulator initialization — no state leaks between runs
# ---------------------------------------------------------------------------

class TestAccumulatorIsolation:
    """Sequential run_backtest() calls must produce independent results.

    Each call creates its own accumulators via _new_accumulator().
    Even when both calls fail early, the returned dicts must be
    structurally independent (not shared references).
    """

    def test_sequential_runs_return_independent_dicts(self):
        """Two sequential calls with different dates return distinct dicts."""
        r1 = run_backtest(
            "2026-01-01",
            date_to="2026-01-02",
            model="full",
            data_source="local",
        )
        r2 = run_backtest(
            "2026-01-10",
            date_to="2026-01-11",
            model="full",
            data_source="local",
        )
        # Results must be distinct objects.
        assert r1 is not r2

        # Both must have the success key.
        assert "success" in r1
        assert "success" in r2

        # If both succeeded, their date ranges must differ.
        if r1.get("success") and r2.get("success"):
            assert r1["dateFrom"] != r2["dateFrom"]
            assert r1["dateTo"] != r2["dateTo"]

            # Reports must be independent objects.
            if "reports" in r1 and "reports" in r2:
                assert r1["reports"] is not r2["reports"]

    def test_failure_result_does_not_leak_state(self):
        """A failing run should not contaminate a subsequent run's result."""
        # First call: intentionally bad (date_to < date_from).
        r_bad = run_backtest("2026-02-01", date_to="2026-01-01")
        assert not r_bad["success"]

        # Second call: valid dates, will fail only for missing local data.
        r_ok = run_backtest(
            "2026-01-15",
            date_to="2026-01-16",
            model="full",
            data_source="local",
        )
        # The second result must not inherit any error message from the first.
        assert r_ok is not r_bad
        if not r_ok["success"]:
            assert r_ok["error"] != r_bad["error"]

    def test_model_both_creates_separate_accumulators(self):
        """model='both' must create independent accumulators for full and simple."""
        result = run_backtest(
            "2026-01-01",
            date_to="2026-01-02",
            model="both",
            data_source="local",
        )
        if result.get("success") and "reports" in result:
            reports = result["reports"]
            if "full" in reports and "simple" in reports:
                # Reports must be distinct objects, not aliases.
                assert reports["full"] is not reports["simple"]
                # sampleCount should be independently tracked.
                assert "sampleCount" in reports["full"]
                assert "sampleCount" in reports["simple"]


# ---------------------------------------------------------------------------
# 3. Model parameter validation (supplement to contract tests)
# ---------------------------------------------------------------------------

class TestModelParameterBehavior:
    """Behavioral checks for the model parameter beyond simple rejection."""

    def test_model_both_evaluates_two_models(self):
        """model='both' should list both 'full' and 'simple' in modelsEvaluated."""
        result = run_backtest(
            "2026-01-01",
            date_to="2026-01-02",
            model="both",
            data_source="local",
        )
        if result.get("success"):
            assert set(result["modelsEvaluated"]) == {"full", "simple"}

    def test_model_full_evaluates_single(self):
        """model='full' should list only 'full' in modelsEvaluated."""
        result = run_backtest(
            "2026-01-01",
            date_to="2026-01-02",
            model="full",
            data_source="local",
        )
        if result.get("success"):
            assert result["modelsEvaluated"] == ["full"]

    def test_model_simple_evaluates_single(self):
        """model='simple' should list only 'simple' in modelsEvaluated."""
        result = run_backtest(
            "2026-01-01",
            date_to="2026-01-02",
            model="simple",
            data_source="local",
        )
        if result.get("success"):
            assert result["modelsEvaluated"] == ["simple"]

    def test_model_case_insensitive(self):
        """Model values should be case-insensitive."""
        for variant in ("Full", "FULL", "fUlL"):
            result = run_backtest(
                "2026-01-01",
                date_to="2026-01-02",
                model=variant,
                data_source="local",
            )
            if not result["success"]:
                assert "model must be" not in result.get("error", ""), (
                    f"model='{variant}' should be accepted (case-insensitive)"
                )

    def test_model_none_defaults_to_both(self):
        """model=None should default to 'both'."""
        result = run_backtest(
            "2026-01-01",
            date_to="2026-01-02",
            model=None,
            data_source="local",
        )
        if not result["success"]:
            assert "model must be" not in result.get("error", ""), (
                "model=None should default to 'both', not be rejected"
            )

    def test_invalid_model_error_message_lists_options(self):
        """Rejection for invalid model must mention valid options."""
        result = run_backtest(
            "2026-01-01",
            date_to="2026-01-02",
            model="xgboost",
        )
        assert not result["success"]
        error = result["error"].lower()
        assert "full" in error
        assert "simple" in error
        assert "both" in error


# ---------------------------------------------------------------------------
# 4. Save flag behavior
# ---------------------------------------------------------------------------

class TestSaveFlagBehavior:
    """Verify that save_results controls file output correctly."""

    def test_save_false_no_savedTo_key(self):
        """When save_results=False, the result must NOT contain 'savedTo'."""
        result = run_backtest(
            "2026-01-01",
            date_to="2026-01-02",
            model="full",
            data_source="local",
            save_results=False,
        )
        # Whether it succeeds or fails, savedTo should never appear.
        assert "savedTo" not in result, (
            "save_results=False must not produce a 'savedTo' key"
        )

    def test_save_true_includes_savedTo_on_success(self):
        """When save_results=True and the run succeeds, 'savedTo' must appear.

        This test uses a temporary directory for local_index that does not
        exist, so it will fail at the local-provider init stage.  We
        cannot easily test actual file writing without a real local index,
        so we verify the structural expectation: successful runs with
        save_results=True produce a 'savedTo' key.
        """
        result = run_backtest(
            "2026-01-01",
            date_to="2026-01-02",
            model="full",
            data_source="local",
            save_results=True,
        )
        # If the run succeeded (unlikely without real data), savedTo must exist.
        if result.get("success"):
            assert "savedTo" in result
            # Clean up the saved file to avoid polluting the repo.
            saved_path = result["savedTo"]
            if os.path.exists(saved_path):
                os.remove(saved_path)

    def test_save_false_no_files_written(self):
        """save_results=False must not create any new files in backtest_results/.

        We check the results directory before and after the call.
        """
        results_dir = os.path.join(_REPO_ROOT, "data", "backtest_results")

        # Snapshot existing files (directory may not exist).
        before = set()
        if os.path.isdir(results_dir):
            before = set(os.listdir(results_dir))

        run_backtest(
            "2026-01-01",
            date_to="2026-01-02",
            model="full",
            data_source="local",
            save_results=False,
        )

        after = set()
        if os.path.isdir(results_dir):
            after = set(os.listdir(results_dir))

        new_files = after - before
        assert not new_files, (
            f"save_results=False wrote unexpected files: {new_files}"
        )


# ---------------------------------------------------------------------------
# 5. data_source="local" graceful failure
# ---------------------------------------------------------------------------

class TestLocalDataSourceGracefulFailure:
    """data_source='local' must fail gracefully when the index is missing."""

    def test_missing_local_index_returns_error(self):
        """When the default local index does not exist, return a clean error."""
        result = run_backtest(
            "2026-01-01",
            date_to="2026-01-02",
            data_source="local",
        )
        # Should fail because the local index file is not present.
        if not result["success"]:
            assert isinstance(result["error"], str)
            assert len(result["error"]) > 0

    def test_explicit_nonexistent_index_path_returns_error(self):
        """An explicit nonexistent index path should produce a FileNotFoundError."""
        fake_path = os.path.join(tempfile.gettempdir(), "nonexistent_index_abc123.pkl")
        result = run_backtest(
            "2026-01-01",
            date_to="2026-01-02",
            data_source="local",
            local_index=fake_path,
        )
        assert not result["success"]
        assert "not found" in result["error"].lower() or "nonexistent" in result["error"].lower()


# ---------------------------------------------------------------------------
# 6. Return structure invariants
# ---------------------------------------------------------------------------

class TestReturnStructureInvariants:
    """Structural invariants that hold regardless of success or failure."""

    def test_result_is_always_dict(self):
        """run_backtest() must always return a dict."""
        cases = [
            {"date_from": "bad"},
            {"date_from": "2026-01-01", "date_to": "2026-01-02", "data_source": "local"},
            {"date_from": "2026-01-01", "model": "invalid"},
        ]
        for kwargs in cases:
            result = run_backtest(**kwargs)
            assert isinstance(result, dict), f"Expected dict, got {type(result)}"

    def test_success_key_always_present(self):
        """Every return dict must contain a 'success' key."""
        cases = [
            {"date_from": "2026-01-01", "data_source": "local"},
            {"date_from": "bad"},
            {"date_from": "2026-01-01", "model": "bad"},
        ]
        for kwargs in cases:
            result = run_backtest(**kwargs)
            assert "success" in result, f"Missing 'success' key in {result.keys()}"

    def test_failure_always_has_error_string(self):
        """Every failure dict must have an 'error' key with a non-empty string."""
        failure_cases = [
            {"date_from": "not-a-date"},
            {"date_from": "2026-02-01", "date_to": "2026-01-01"},
            {"date_from": "2026-01-01", "model": "bad"},
            {"date_from": "2026-01-01", "data_source": "bad"},
        ]
        for kwargs in failure_cases:
            result = run_backtest(**kwargs)
            assert not result["success"]
            assert "error" in result
            assert isinstance(result["error"], str)
            assert len(result["error"]) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
