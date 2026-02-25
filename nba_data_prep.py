#!/usr/bin/env python3
"""Compatibility facade for prep logic.

Primary implementations now live in:
- nba_prep_projection.py
- nba_prep_usage.py
"""

from nba_prep_projection import compute_projection, compute_projection_simple
from nba_prep_usage import _USG_STAT_ELASTICITY, compute_usage_adjustment

__all__ = [
    "_USG_STAT_ELASTICITY",
    "compute_projection",
    "compute_projection_simple",
    "compute_usage_adjustment",
]
