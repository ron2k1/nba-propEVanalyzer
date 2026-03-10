#!/usr/bin/env python3
"""
Lightweight shared policy constants — zero external imports.

Single source of truth for BETTING_POLICY and SIGNAL_SPEC values.
Both gates.py and nba_data_collection.py import from here.
"""

# ---------------------------------------------------------------------------
# BETTING_POLICY — controls which signals become real-money bets
# ---------------------------------------------------------------------------
STAT_WHITELIST = {"pts", "ast"}   # reb removed 2026-02-28: -5.34% ROI; pra removed 2026-03-01
BLOCKED_PROB_BINS = {1, 2, 3, 4, 5, 6, 7, 8}   # Active: 0 (0-10%) + 9 (90-100%)
MIN_EV_PCT = 0.0

# ---------------------------------------------------------------------------
# SIGNAL_SPEC — controls which signals are logged (broader than BETTING_POLICY)
# ---------------------------------------------------------------------------
ELIGIBLE_STATS = {"pts", "reb", "ast"}   # pra removed 2026-03-01: -3.81% ROI
MIN_EDGE = 0.08          # raised 2026-03-01: 0.05→0.08
MIN_EDGE_BY_STAT = {"reb": 0.08, "ast": 0.09}
MIN_CONFIDENCE = 0.60    # raised 2026-03-01: 0.55→0.60
REAL_LINE_REQUIRED_STATS = {"reb"}

# ---------------------------------------------------------------------------
# Pinnacle confirmation
# ---------------------------------------------------------------------------
PINNACLE_THRESHOLDS = {0: 0.75, 9: 0.75}
PINNACLE_MIN_NO_VIG_BY_STAT = {"pts": 0.62, "ast": 0.67, "reb": 0.62}
