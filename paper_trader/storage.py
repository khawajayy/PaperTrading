"""Reads and writes the published record in docs/data."""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
EQUITY_FIELDS = ["time", "equity", "cash", "exposure", "drawdown_pct", "benchmark", "open_positions"]


def load_config() -> dict:
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def read_json(name: str, default=None):
    path = DATA / name
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(name: str, value) -> None:
    """Writes through a temp file so a crash can't leave a half-written file."""
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = DATA / f"{name}.tmp"
    tmp.write_text(json.dumps(value, indent=1), encoding="utf-8")
    os.replace(tmp, DATA / name)


def read_csv(name: str) -> list[dict]:
    path = DATA / name
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_csv(name: str, fields: list[str], rows: list[dict]) -> None:
    path = DATA / name
    DATA.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        if new:
            w.writeheader()
        w.writerows(rows)
