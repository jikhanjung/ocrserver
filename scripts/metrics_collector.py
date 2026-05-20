#!/usr/bin/env python3
"""Single-shot host metrics collector.

Reads load average, memory usage and per-GPU stats, then appends one row
to ``metrics.db`` (SQLite). Designed to be fired by a systemd timer once
per minute. Self-contained — only stdlib + nvidia-smi.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    ts               INTEGER PRIMARY KEY,
    load1            REAL,
    load5            REAL,
    load15           REAL,
    mem_used_mb      INTEGER,
    mem_total_mb     INTEGER,
    gpu0_util        REAL,
    gpu0_temp        REAL,
    gpu0_mem_used_mb INTEGER,
    gpu0_mem_total_mb INTEGER,
    gpu1_util        REAL,
    gpu1_temp        REAL,
    gpu1_mem_used_mb INTEGER,
    gpu1_mem_total_mb INTEGER
);
"""

COLS = [
    "ts", "load1", "load5", "load15", "mem_used_mb", "mem_total_mb",
    "gpu0_util", "gpu0_temp", "gpu0_mem_used_mb", "gpu0_mem_total_mb",
    "gpu1_util", "gpu1_temp", "gpu1_mem_used_mb", "gpu1_mem_total_mb",
]


def read_loadavg() -> tuple[float, float, float]:
    parts = Path("/proc/loadavg").read_text().split()
    return float(parts[0]), float(parts[1]), float(parts[2])


def read_meminfo_mb() -> tuple[int, int]:
    fields: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, _, rest = line.partition(":")
        v = rest.strip().split()
        if not v:
            continue
        fields[k] = int(v[0])  # kB
    total_kb = fields.get("MemTotal", 0)
    avail_kb = fields.get("MemAvailable", fields.get("MemFree", 0))
    used_kb = max(total_kb - avail_kb, 0)
    return used_kb // 1024, total_kb // 1024


def read_gpus() -> dict[int, dict]:
    """Return {gpu_index: {util, temp, mem_used_mb, mem_total_mb}}.
    Empty dict on nvidia-smi failure."""
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=index,utilization.gpu,temperature.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            check=True, capture_output=True, text=True, timeout=10,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"[metrics] nvidia-smi failed: {e}", file=sys.stderr)
        return {}

    result: dict[int, dict] = {}
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 5:
            continue
        try:
            idx = int(parts[0])
            result[idx] = {
                "util": float(parts[1]),
                "temp": float(parts[2]),
                "mem_used_mb": int(parts[3]),
                "mem_total_mb": int(parts[4]),
            }
        except ValueError:
            continue
    return result


def collect() -> dict:
    load1, load5, load15 = read_loadavg()
    mem_used, mem_total = read_meminfo_mb()
    gpus = read_gpus()
    g0 = gpus.get(0, {})
    g1 = gpus.get(1, {})
    return {
        "ts": int(time.time()),
        "load1": load1, "load5": load5, "load15": load15,
        "mem_used_mb": mem_used, "mem_total_mb": mem_total,
        "gpu0_util": g0.get("util"), "gpu0_temp": g0.get("temp"),
        "gpu0_mem_used_mb": g0.get("mem_used_mb"),
        "gpu0_mem_total_mb": g0.get("mem_total_mb"),
        "gpu1_util": g1.get("util"), "gpu1_temp": g1.get("temp"),
        "gpu1_mem_used_mb": g1.get("mem_used_mb"),
        "gpu1_mem_total_mb": g1.get("mem_total_mb"),
    }


def write_row(db_path: Path, row: dict) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        placeholders = ",".join("?" for _ in COLS)
        conn.execute(
            f"INSERT OR REPLACE INTO metrics ({','.join(COLS)}) VALUES ({placeholders})",
            [row[c] for c in COLS],
        )
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get(
        "METRICS_DB", "/srv/ocrserver/data/metrics.db"))
    ap.add_argument("--print", action="store_true",
                    help="print collected row to stdout (for debugging)")
    args = ap.parse_args()

    row = collect()
    write_row(Path(args.db), row)
    if args.print:
        for k in COLS:
            print(f"{k}={row[k]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
