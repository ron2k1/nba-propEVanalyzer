from datetime import date

from core import nba_backtest as bt


def test_normalized_backtest_args_capture_effective_flags():
    args = bt._normalized_backtest_args(
        start=date(2026, 2, 1),
        end=date(2026, 2, 7),
        model_key="full",
        source_key="local",
        save_results=True,
        fast=True,
        bref_dir=None,
        local_index="data/reference/kaggle_nba/index.pkl",
        odds_key="local_history",
        odds_db="data/reference/odds_history/odds_history.sqlite",
        odds_only=True,
        compute_clv=True,
        walk_forward=True,
        emit_bets=True,
        emit_all=True,
        match_live=True,
        no_blend=True,
        no_gates=True,
        line_timing="opening",
    )

    assert args[:5] == ["nba_mod.py", "backtest", "2026-02-01", "2026-02-07", "--model"]
    assert "--save" in args
    assert "--local" in args
    assert "--odds-source" in args
    assert "--real-only" in args
    assert "--walk-forward" in args
    assert "--emit-all" in args
    assert "--match-live" in args
    assert "--line-timing" in args


def test_build_artifact_metadata_includes_provenance(monkeypatch):
    monkeypatch.setattr(bt, "_utc_now_iso", lambda: "2026-03-07T21:00:00Z")
    monkeypatch.setattr(bt, "_git_head", lambda: "abc123")
    monkeypatch.setattr(bt, "_hash_file", lambda path: f"hash:{path.split('/')[-1].split('\\\\')[-1]}")

    betting_policy = {"statWhitelist": ["ast", "pts"], "blockedProbBins": [1, 2, 3, 4, 5, 6, 7, 8]}
    metadata = bt._build_artifact_metadata(
        start=date(2026, 2, 1),
        end=date(2026, 2, 7),
        model_key="full",
        source_key="local",
        save_results=True,
        fast=False,
        bref_dir=None,
        local_index=None,
        odds_key="local_history",
        odds_db=None,
        odds_only=True,
        compute_clv=False,
        walk_forward=False,
        emit_bets=False,
        emit_all=False,
        match_live=False,
        no_blend=False,
        no_gates=False,
        line_timing="closing",
        betting_policy=betting_policy,
        checkpoint=False,
        checkpoint_date_to=None,
    )

    assert metadata["generatedAtUtc"] == "2026-03-07T21:00:00Z"
    assert metadata["gitHead"] == "abc123"
    assert metadata["normalizedCliArgs"][:4] == ["nba_mod.py", "backtest", "2026-02-01", "2026-02-07"]
    assert metadata["policyHash"] == bt._stable_json_hash(betting_policy)
    assert metadata["checkpoint"] is False
    assert metadata["calibrationHash"].startswith("hash:")
