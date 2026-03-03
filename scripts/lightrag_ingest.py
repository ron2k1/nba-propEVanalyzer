#!/usr/bin/env python3
"""Ingest project knowledge into LightRAG for retrieval-augmented context.

Sources:
  lessons   — tasks/lessons.md split by ## headings
  docs      — docs/PLAN_*.md and docs/*_report.md
  claude_md — CLAUDE.md project config
  backtests — data/backtest_results/*.json (latest 30 by mtime)
  journal   — data/decision_journal/decision_journal.sqlite (last 60 days)

Idempotent: tracks content hashes in data/lightrag_storage/ingest_manifest.json.
Re-run safely; unchanged documents are skipped unless --force is passed.
"""

import argparse
import glob
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_PATH = os.path.join(REPO_ROOT, "data", "lightrag_storage", "ingest_manifest.json")
DEFAULT_BASE_URL = "http://localhost:9621"

VALID_SOURCES = ("docs", "lessons", "backtests", "journal", "claude_md", "all")


# ---------------------------------------------------------------------------
# Manifest (idempotency)
# ---------------------------------------------------------------------------

def _load_manifest():
    """Load the ingest manifest, returning {} if it doesn't exist."""
    if os.path.isfile(MANIFEST_PATH):
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_manifest(manifest):
    """Persist the manifest to disk, creating parent dirs if needed."""
    os.makedirs(os.path.dirname(MANIFEST_PATH), exist_ok=True)
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)


def _content_hash(text):
    """Return hex SHA-256 of the given text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _should_ingest(manifest, source_key, content, force=False):
    """Return True if the content has changed or force is set."""
    if force:
        return True
    h = _content_hash(content)
    entry = manifest.get(source_key)
    if entry and entry.get("hash") == h:
        return False
    return True


def _record_ingestion(manifest, source_key, content):
    """Record a successful ingestion in the manifest."""
    manifest[source_key] = {
        "hash": _content_hash(content),
        "ingested_at": datetime.utcnow().isoformat() + "Z",
    }


# ---------------------------------------------------------------------------
# LightRAG HTTP client
# ---------------------------------------------------------------------------

def _check_server(base_url):
    """Verify LightRAG is reachable. Exit 1 if not."""
    try:
        r = requests.get(f"{base_url}/health", timeout=5)
        r.raise_for_status()
    except requests.exceptions.ConnectionError:
        print(f"[ingest] ERROR: LightRAG not reachable at {base_url}", file=sys.stderr)
        sys.exit(1)
    except requests.exceptions.HTTPError:
        # /health may not exist; try a benign endpoint
        try:
            requests.get(base_url, timeout=5)
        except requests.exceptions.ConnectionError:
            print(f"[ingest] ERROR: LightRAG not reachable at {base_url}", file=sys.stderr)
            sys.exit(1)


def _insert_text(base_url, text, description=""):
    """POST a text document to LightRAG. Returns True on success."""
    url = f"{base_url}/documents/text"
    payload = {"text": text}
    if description:
        payload["description"] = description
    try:
        r = requests.post(url, json=payload, timeout=120)
        r.raise_for_status()
        return True
    except requests.exceptions.RequestException as exc:
        print(f"[ingest]   WARNING: POST failed: {exc}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Source: lessons
# ---------------------------------------------------------------------------

def _ingest_lessons(base_url, manifest, force=False):
    """Split tasks/lessons.md by ## headings and ingest each section."""
    path = os.path.join(REPO_ROOT, "tasks", "lessons.md")
    if not os.path.isfile(path):
        print("[ingest] lessons: file not found, skipping")
        return 0, 0

    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    # Split by ## headings (keep the heading with its content)
    sections = []
    current_heading = None
    current_lines = []

    for line in raw.splitlines(keepends=True):
        if line.startswith("## "):
            if current_heading is not None:
                sections.append((current_heading, "".join(current_lines).strip()))
            current_heading = line.strip().lstrip("# ").strip()
            current_lines = []
        else:
            current_lines.append(line)

    # Last section
    if current_heading is not None:
        sections.append((current_heading, "".join(current_lines).strip()))

    ingested = 0
    skipped = 0

    for heading, body in sections:
        if not body:
            continue
        source_key = f"lessons::{heading}"
        doc_text = f"[LESSON] {heading}\n\n{body}"
        if not _should_ingest(manifest, source_key, doc_text, force):
            skipped += 1
            continue
        if _insert_text(base_url, doc_text, description=f"Lesson: {heading}"):
            _record_ingestion(manifest, source_key, doc_text)
            ingested += 1
        else:
            skipped += 1

    return ingested, skipped


# ---------------------------------------------------------------------------
# Source: docs
# ---------------------------------------------------------------------------

def _ingest_docs(base_url, manifest, force=False):
    """Glob docs/PLAN_*.md and docs/*_report.md, ingest each file."""
    patterns = [
        os.path.join(REPO_ROOT, "docs", "PLAN_*.md"),
        os.path.join(REPO_ROOT, "docs", "*_report.md"),
    ]
    paths = set()
    for pat in patterns:
        paths.update(glob.glob(pat))

    ingested = 0
    skipped = 0

    for fpath in sorted(paths):
        fname = os.path.basename(fpath)
        with open(fpath, "r", encoding="utf-8") as f:
            content = f.read()
        if not content.strip():
            continue

        source_key = f"docs::{fname}"
        doc_text = f"[DOC] {fname}\n\n{content}"
        if not _should_ingest(manifest, source_key, doc_text, force):
            skipped += 1
            continue
        if _insert_text(base_url, doc_text, description=f"Doc: {fname}"):
            _record_ingestion(manifest, source_key, doc_text)
            ingested += 1
        else:
            skipped += 1

    return ingested, skipped


# ---------------------------------------------------------------------------
# Source: claude_md
# ---------------------------------------------------------------------------

def _ingest_claude_md(base_url, manifest, force=False):
    """Ingest CLAUDE.md as a single document."""
    path = os.path.join(REPO_ROOT, "CLAUDE.md")
    if not os.path.isfile(path):
        print("[ingest] claude_md: CLAUDE.md not found, skipping")
        return 0, 0

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    source_key = "claude_md::CLAUDE.md"
    doc_text = f"[CONFIG] CLAUDE.md\n\n{content}"
    if not _should_ingest(manifest, source_key, doc_text, force):
        return 0, 1
    if _insert_text(base_url, doc_text, description="Project config: CLAUDE.md"):
        _record_ingestion(manifest, source_key, doc_text)
        return 1, 0
    return 0, 1


# ---------------------------------------------------------------------------
# Source: backtests
# ---------------------------------------------------------------------------

def _summarize_backtest(data, filename):
    """Produce a 2-3 paragraph prose summary from backtest JSON."""
    rep = data.get("reports", {}).get("full") or data
    if not rep:
        return None

    date_from = data.get("dateFrom", "?")
    date_to = data.get("dateTo", "?")
    sample = rep.get("sampleCount", 0)
    errors = rep.get("projectionErrors", 0)

    # Real-line ROI
    roi_real = rep.get("roiReal") or {}
    real_bets = roi_real.get("betsPlaced", 0)
    real_hit = roi_real.get("hitRatePct")
    real_roi = roi_real.get("roiPctPerBet")
    real_wins = roi_real.get("wins", 0)
    real_losses = roi_real.get("losses", 0)

    # Synthetic ROI
    roi_synth = rep.get("roiSynth") or {}
    synth_bets = roi_synth.get("betsPlaced", 0)
    synth_roi = roi_synth.get("roiPctPerBet")

    # Policy
    policy = data.get("bettingPolicy") or {}
    whitelist = policy.get("statWhitelist", [])
    blocked = policy.get("blockedProbBins", [])

    # Per-stat breakdown
    stat_lines = []
    for stat, d in (rep.get("realLineStatRoi") or {}).items():
        n = d.get("betsPlaced", 0)
        if n > 0:
            hr = d.get("hitRatePct", "?")
            sr = d.get("roiPctPerBet", 0)
            stat_lines.append(f"{stat}: {n} bets, {hr}% hit, {sr:+.1f}% ROI")

    # Calibration bins
    bin_lines = []
    for b in rep.get("realLineCalibBins") or []:
        n = b.get("betsPlaced", 0)
        if n > 0:
            bin_lines.append(
                f"{b['bin']}: {n} bets, {b.get('hitRatePct', '?')}% hit, "
                f"{b.get('roiPctPerBet', 0):+.1f}% ROI"
            )

    # Build paragraphs
    para1 = (
        f"Backtest '{filename}' covers {date_from} to {date_to} with {sample} total "
        f"projection samples and {errors} projection errors. "
    )
    if real_bets > 0:
        para1 += (
            f"On real closing lines, {real_bets} bets were placed with a "
            f"{real_hit}% hit rate and {real_roi:+.1f}% ROI per bet "
            f"({real_wins}W / {real_losses}L). "
        )
    if synth_bets > 0:
        para1 += (
            f"Synthetic-line bets: {synth_bets} placed, "
            f"{synth_roi:+.1f}% ROI. "
        )

    para2 = ""
    if stat_lines:
        para2 = "Per-stat real-line performance: " + "; ".join(stat_lines) + ". "
    if bin_lines:
        para2 += "Calibration bin performance: " + "; ".join(bin_lines) + ". "

    para3 = ""
    if whitelist or blocked:
        para3 = (
            f"Policy used: stat whitelist {whitelist}, "
            f"blocked probability bins {blocked}."
        )

    summary = para1.strip()
    if para2:
        summary += "\n\n" + para2.strip()
    if para3:
        summary += "\n\n" + para3.strip()

    return summary


def _ingest_backtests(base_url, manifest, force=False):
    """Ingest latest 30 backtest result JSONs by modification time."""
    bt_dir = os.path.join(REPO_ROOT, "data", "backtest_results")
    if not os.path.isdir(bt_dir):
        print("[ingest] backtests: directory not found, skipping")
        return 0, 0

    files = glob.glob(os.path.join(bt_dir, "*.json"))
    if not files:
        print("[ingest] backtests: no JSON files found, skipping")
        return 0, 0

    # Sort by mtime descending, take latest 30
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    files = files[:30]

    ingested = 0
    skipped = 0

    for fpath in files:
        fname = os.path.basename(fpath)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[ingest]   WARNING: could not read {fname}: {exc}", file=sys.stderr)
            skipped += 1
            continue

        summary = _summarize_backtest(data, fname)
        if not summary:
            skipped += 1
            continue

        source_key = f"backtests::{fname}"
        doc_text = f"[BACKTEST] {fname}\n\n{summary}"
        if not _should_ingest(manifest, source_key, doc_text, force):
            skipped += 1
            continue
        if _insert_text(base_url, doc_text, description=f"Backtest: {fname}"):
            _record_ingestion(manifest, source_key, doc_text)
            ingested += 1
        else:
            skipped += 1

    return ingested, skipped


# ---------------------------------------------------------------------------
# Source: journal
# ---------------------------------------------------------------------------

def _ingest_journal(base_url, manifest, force=False):
    """Read decision_journal.sqlite, produce daily prose summaries for last 60 days."""
    db_path = os.path.join(REPO_ROOT, "data", "decision_journal", "decision_journal.sqlite")
    if not os.path.isfile(db_path):
        print("[ingest] journal: database not found, skipping")
        return 0, 0

    cutoff = (datetime.utcnow() - timedelta(days=60)).strftime("%Y-%m-%dT00:00:00Z")

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Group signals by date (extract YYYY-MM-DD from ts_utc)
        cur.execute("""
            SELECT
                substr(ts_utc, 1, 10)                  AS signal_date,
                COUNT(*)                               AS cnt,
                GROUP_CONCAT(DISTINCT stat)             AS stats,
                GROUP_CONCAT(DISTINCT player_name)      AS players,
                AVG(recommended_edge)                   AS avg_edge,
                AVG(confidence)                         AS avg_conf,
                SUM(CASE WHEN recommended_side = 'over' THEN 1 ELSE 0 END)  AS overs,
                SUM(CASE WHEN recommended_side = 'under' THEN 1 ELSE 0 END) AS unders,
                AVG(projection)                        AS avg_proj,
                AVG(line)                              AS avg_line
            FROM signals
            WHERE ts_utc >= ?
            GROUP BY signal_date
            ORDER BY signal_date DESC
        """, (cutoff,))

        rows = cur.fetchall()
        conn.close()
    except sqlite3.Error as exc:
        print(f"[ingest] journal: SQLite error: {exc}", file=sys.stderr)
        return 0, 0

    if not rows:
        print("[ingest] journal: no signals in last 60 days, skipping")
        return 0, 0

    ingested = 0
    skipped = 0

    for row in rows:
        date_str = row["signal_date"]
        cnt = row["cnt"]
        stats_raw = row["stats"] or ""
        players_raw = row["players"] or ""
        avg_edge = row["avg_edge"]
        avg_conf = row["avg_conf"]
        overs = row["overs"]
        unders = row["unders"]

        # Build top stats and top players (limit to 5 most common)
        stat_list = [s.strip() for s in stats_raw.split(",") if s.strip()]
        player_list = [p.strip() for p in players_raw.split(",") if p.strip()]
        top_players = player_list[:8] if len(player_list) > 8 else player_list

        summary = (
            f"On {date_str}, the decision journal logged {cnt} qualifying signals. "
            f"Stats covered: {', '.join(stat_list) if stat_list else 'none'}. "
            f"Direction split: {overs} over / {unders} under."
        )

        if avg_edge is not None:
            summary += f" Average recommended edge: {avg_edge:.3f}."
        if avg_conf is not None:
            summary += f" Average confidence: {avg_conf:.3f}."

        if top_players:
            summary += f"\n\nTop players with signals: {', '.join(top_players)}."

        source_key = f"journal::{date_str}"
        doc_text = f"[JOURNAL] {date_str}\n\n{summary}"
        if not _should_ingest(manifest, source_key, doc_text, force):
            skipped += 1
            continue
        if _insert_text(base_url, doc_text, description=f"Journal: {date_str}"):
            _record_ingestion(manifest, source_key, doc_text)
            ingested += 1
        else:
            skipped += 1

    return ingested, skipped


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

SOURCE_HANDLERS = {
    "lessons":   _ingest_lessons,
    "docs":      _ingest_docs,
    "claude_md": _ingest_claude_md,
    "backtests": _ingest_backtests,
    "journal":   _ingest_journal,
}


def main():
    parser = argparse.ArgumentParser(
        description="Ingest project knowledge into LightRAG."
    )
    parser.add_argument(
        "--source",
        choices=VALID_SOURCES,
        default="all",
        help="Which source to ingest (default: all)",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"LightRAG base URL (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-ingest even if content hash is unchanged",
    )
    args = parser.parse_args()

    # Verify LightRAG is up
    _check_server(args.base_url)

    manifest = _load_manifest()

    if args.source == "all":
        sources = list(SOURCE_HANDLERS.keys())
    else:
        sources = [args.source]

    total_ingested = 0
    total_skipped = 0

    for src in sources:
        handler = SOURCE_HANDLERS[src]
        ing, skip = handler(args.base_url, manifest, force=args.force)
        print(f"[ingest] {src}: {ing} documents ingested, {skip} skipped (unchanged)")
        total_ingested += ing
        total_skipped += skip

    # Persist manifest after all sources
    _save_manifest(manifest)

    print(f"[ingest] TOTAL: {total_ingested} ingested, {total_skipped} skipped")
    return 0 if total_ingested >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
