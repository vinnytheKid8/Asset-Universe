"""Tiny output layer: partitioned gzip-CSV (always) or Parquet (if pyarrow)."""

from __future__ import annotations

import csv
import gzip
from pathlib import Path
from typing import Iterable, Sequence

try:                                    # optional
    import pyarrow  # noqa: F401
    _HAS_PARQUET = True
except Exception:
    _HAS_PARQUET = False


def write_table(records: Sequence[dict], out_dir: Path, table: str,
                exchange: str, day: str, fmt: str = "auto") -> Path | None:
    """out_dir/table/exchange=<ex>/date=<YYYY-MM-DD>.{parquet|csv.gz}"""
    if not records:
        return None
    part = Path(out_dir) / table / f"exchange={exchange}"
    part.mkdir(parents=True, exist_ok=True)
    use_parquet = fmt == "parquet" or (fmt == "auto" and _HAS_PARQUET)
    if use_parquet:
        import pandas as pd
        path = part / f"{day}.parquet"
        pd.DataFrame.from_records(records).to_parquet(path, index=False,
                                                      compression="zstd")
        return path
    path = part / f"{day}.csv.gz"
    cols = list(records[0].keys())
    with gzip.open(path, "wt", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(records)
    return path


def read_table(out_dir: Path, table: str, exchange: str | None = None):
    """Load a whole table back as a pandas DataFrame."""
    import pandas as pd
    root = Path(out_dir) / table
    pat = f"exchange={exchange}/*" if exchange else "*/*"
    frames = []
    for p in sorted(root.glob(pat)):
        if p.suffix == ".parquet":
            frames.append(pd.read_parquet(p))
        elif p.name.endswith(".csv.gz"):
            frames.append(pd.read_csv(p))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def iter_paths(out_dir: Path, table: str) -> Iterable[Path]:
    yield from sorted((Path(out_dir) / table).glob("*/*"))
