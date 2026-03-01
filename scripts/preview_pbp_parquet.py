#!/usr/bin/env python3
"""
Preview a parquet file and optionally export a CSV sample.

Usage:
  .venv/Scripts/python.exe scripts/preview_pbp_parquet.py
  .venv/Scripts/python.exe scripts/preview_pbp_parquet.py --rows 200 --out .tmp/pbp_preview.csv
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _try_pyarrow(path: Path, rows: int):
    import pyarrow.parquet as pq  # type: ignore

    pf = pq.ParquetFile(str(path))
    cols = list(pf.schema.names or [])
    batches = pf.iter_batches(batch_size=rows)
    first = next(batches, None)
    if first is None:
        import pandas as pd

        df = pd.DataFrame()
    else:
        df = first.to_pandas()
    return {
        "engine": "pyarrow",
        "row_count_total": int(pf.metadata.num_rows) if pf.metadata else None,
        "column_count": len(cols),
        "columns": cols,
        "df": df,
    }


def _try_duckdb(path: Path, rows: int):
    import duckdb  # type: ignore

    con = duckdb.connect()
    safe = str(path).replace("\\", "/").replace("'", "''")
    cols_meta = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{safe}')").fetchall()
    cols = [c[0] for c in cols_meta]
    total = con.execute(f"SELECT COUNT(*) FROM read_parquet('{safe}')").fetchone()[0]
    df = con.execute(f"SELECT * FROM read_parquet('{safe}') LIMIT {int(rows)}").fetchdf()
    return {
        "engine": "duckdb",
        "row_count_total": int(total),
        "column_count": len(cols),
        "columns": cols,
        "df": df,
    }


def _try_polars(path: Path, rows: int):
    import polars as pl  # type: ignore

    lf = pl.scan_parquet(str(path))
    schema = lf.collect_schema()
    cols = list(schema.names())
    df = lf.limit(rows).collect().to_pandas()
    # Full row count via lazy count.
    total = int(lf.select(pl.len()).collect().item())
    return {
        "engine": "polars",
        "row_count_total": total,
        "column_count": len(cols),
        "columns": cols,
        "df": df,
    }


def preview(path: Path, rows: int):
    errors: list[str] = []
    for fn in (_try_pyarrow, _try_duckdb, _try_polars):
        try:
            return fn(path, rows), errors
        except Exception as e:  # noqa: BLE001
            errors.append(f"{fn.__name__}: {e!r}")
    raise RuntimeError(
        "No parquet engine available. Install one of: pyarrow, duckdb, polars.\n"
        + "\n".join(errors)
    )


def main() -> int:
    default_path = Path("NBA Database (1947 - Present)") / "PlayByPlay.parquet"
    parser = argparse.ArgumentParser(description="Preview PlayByPlay parquet and export CSV sample.")
    parser.add_argument("--path", default=str(default_path), help="Path to parquet file.")
    parser.add_argument("--rows", type=int, default=50, help="Number of sample rows.")
    parser.add_argument("--out", default=".tmp/pbp_preview.csv", help="CSV path for sample rows.")
    args = parser.parse_args()

    path = Path(args.path)
    if not path.exists():
        print(json.dumps({"success": False, "error": f"file not found: {path}"}, indent=2))
        return 2

    try:
        result, engine_errors = preview(path, args.rows)
    except Exception as e:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "success": False,
                    "path": str(path),
                    "error": str(e),
                    "next_step": "Install pyarrow in .venv, then rerun this script.",
                    "install_cmd": ".\\.venv\\Scripts\\python.exe -m pip install pyarrow",
                },
                indent=2,
            )
        )
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    result["df"].to_csv(out, index=False)
    sample = result["df"].head(min(5, len(result["df"]))).to_dict(orient="records")
    print(
        json.dumps(
            {
                "success": True,
                "path": str(path),
                "engine": result["engine"],
                "rowCountTotal": result["row_count_total"],
                "columnCount": result["column_count"],
                "columnsFirst20": result["columns"][:20],
                "sampleRows": sample,
                "csvPreviewPath": str(out),
                "engineErrors": engine_errors,
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
