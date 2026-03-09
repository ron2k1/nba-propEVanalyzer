# Scripts Directory

## Core Operations
| Script | Purpose |
|--------|---------|
| `quality_gate.py` | CI/commit gate — Python compile + JS syntax + hallucination scan |
| `mcp_data_server.py` | FastMCP read-only data server (9 tools, wired as `nba-data` in .mcp.json) |
| `fit_calibration.py` | Temperature-scaling calibration fitting from backtest results |
| `collect_lines.py` | Line collection from multiple sportsbooks |
| `build_closing_lines.py` | Closing line derivation from odds snapshots |
| `line_to_odds_bridge.py` | Bridge line store to odds history |
| `index_local_data.py` | Build local backtest index from Kaggle/parquet data |
| `extend_local_index.py` | Safely extend the local backtest index with newer completed NBA API games |
| `walk_forward_cal.py` | Walk-forward calibration validation |
| `lean_analysis.py` | Lean signal analysis pipeline |

## Data Backfill
| Script | Purpose |
|--------|---------|
| `backfill_odds_history.py` | Odds API historical backfill with credit budgeting |
| `backfill_sportsdataio.py` | SportsDataIO NBA feed backfill |
| `bref_ingest.py` | Basketball Reference data ingestion |
| `stage_local_parquet.py` | Stage external parquet into project data dir |
| `preview_pbp_parquet.py` | Preview parquet dataset contents |
| `lightrag_ingest.py` | RAG document ingestion (requires lightrag server) |

## Validation & Analysis
| Script | Purpose |
|--------|---------|
| `validate_line_bridge.py` | Validate line/odds bridge integrity |
| `validate_shrink_k.py` | Shrinkage parameter (K) validation |
| `validate_stdev_shrink.py` | Stdev shrinkage validation |
| `calibration_blend_check.py` | Blend vs raw calibration comparison |
| `compare_backtests.py` | Backtest comparison and diffing |
| `compare_models.py` | Saved-model comparison and markdown report generation |
| `compare_factorial.py` | Factorial experiment comparison (blend effects) |
| `analyze_real_lines.py` | Real-line performance analysis |
| `e1_summary.py` | Recompute the strict E1 real-line policy subset from saved backtest artifacts |
| `bin0_killswitch.py` | Bin 0 emergency shutdown |
| `backtest_summary.py` | Backtest result summarization |
| `overfitting_chart.py` | Overfit detection visualization |
| `parity_local_vs_nba.py` | Local vs NBA API parity check |

## Scanning & Automation
| Script | Purpose |
|--------|---------|
| `nba_agent.py` | Autonomous fixed-workflow agent runner |
| `scheduled_pipeline.py` | Scheduler-safe wrapper for collect-only or full pipeline runs |
| `scheduled_settle.py` | Scheduler-safe wrapper for paper settlement + summary |
| `register_scheduled_tasks.ps1` | Register Windows Task Scheduler jobs for pipeline automation |
| `autocheck_daemon.py` | Continuous monitoring daemon |
| `betmgm_scan.py` | BetMGM prop scanning |
| `dk_sweep_batch.py` | DraftKings batch prop sweep |
| `offline_scan.py` | Offline prop scanning |
| `injury_monitor.py` | Injury tracking monitor |

## PowerShell / Batch Launchers
| Script | Purpose |
|--------|---------|
| `settle_all.ps1` | Settle all pending bets |
| `run_odds_backfill_chunks.ps1` | Chunked odds backfill runner |
| `run_coverage_and_backtest.ps1` | Full coverage + backtest pipeline |
| `start_lightrag.ps1` / `.bat` | RAG service startup (currently broken — embedding dim mismatch) |
| `start_autocheck.ps1` | Autocheck daemon startup |
| `register_autocheck_startup.ps1` | Register autocheck as Windows startup task |
| `unregister_autocheck_startup.ps1` | Remove autocheck from startup |
| `daily_scan.bat` | Daily scanning batch file |
