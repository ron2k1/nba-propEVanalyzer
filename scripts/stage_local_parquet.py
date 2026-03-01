#!/usr/bin/env python3
"""
Stage an external parquet file into the repo and emit a manifest.

This keeps raw datasets under data/reference/local_parquet so local workflows are:
1) reproducible
2) independent from ad-hoc user download folders
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import shutil
from pathlib import Path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _probe_parquet(path: Path) -> dict:
    out = {
        "ok": False,
        "engine": None,
        "rowCount": None,
        "columnCount": None,
        "columns": [],
        "warning": None,
    }

    try:
        import pyarrow.parquet as pq  # type: ignore

        pf = pq.ParquetFile(str(path))
        out["ok"] = True
        out["engine"] = "pyarrow"
        out["rowCount"] = int(pf.metadata.num_rows) if pf.metadata else None
        cols = [str(c) for c in (pf.schema.names or [])]
        out["columnCount"] = len(cols)
        out["columns"] = cols
        return out
    except Exception as e:
        out["warning"] = f"pyarrow probe failed: {e}"

    try:
        import fastparquet as fp  # type: ignore

        pf = fp.ParquetFile(str(path))
        cols = [str(c) for c in (pf.columns or [])]
        out["ok"] = True
        out["engine"] = "fastparquet"
        out["rowCount"] = int(pf.info.get("rows")) if isinstance(pf.info, dict) and pf.info.get("rows") is not None else None
        out["columnCount"] = len(cols)
        out["columns"] = cols
        out["warning"] = None
        return out
    except Exception as e:
        out["warning"] = f"{out['warning']}; fastparquet probe failed: {e}" if out["warning"] else f"fastparquet probe failed: {e}"

    return out


def stage(source_path: str, dest_dir: str) -> dict:
    src = Path(source_path).expanduser()
    if not src.exists():
        raise FileNotFoundError(f"source file not found: {src}")
    if src.suffix.lower() != ".parquet":
        raise ValueError(f"source must be a .parquet file: {src}")

    dst_dir = Path(dest_dir)
    raw_dir = dst_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    staged_path = raw_dir / src.name
    shutil.copy2(src, staged_path)

    stat = staged_path.stat()
    probe = _probe_parquet(staged_path)

    manifest = {
        "success": True,
        "stagedAtUtc": _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "sourcePath": str(src.resolve()),
        "stagedPath": str(staged_path.resolve()),
        "fileName": src.name,
        "bytes": stat.st_size,
        "sha256": _sha256(staged_path),
        "parquetProbe": probe,
    }

    manifest_path = dst_dir / "manifest.json"
    latest_path = dst_dir / "latest.json"
    with manifest_path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    with latest_path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    manifest["manifestPath"] = str(manifest_path.resolve())
    return manifest


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    default_dest = repo_root / "data" / "reference" / "local_parquet"

    parser = argparse.ArgumentParser(description="Stage local parquet file into repo data/reference/local_parquet")
    parser.add_argument("source", help="Path to parquet file (e.g. C:\\Users\\you\\Downloads\\full_dataset_clean.parquet)")
    parser.add_argument("--dest-dir", default=str(default_dest), help=f"Destination root (default: {default_dest})")
    args = parser.parse_args()

    result = stage(args.source, args.dest_dir)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
