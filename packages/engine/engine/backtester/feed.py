"""Bar feeds — iterators over ``Bar`` events.

The protocol is intentionally minimal: one ``__iter__`` returning Bars in
chronological order. Multiple-symbol feeds merge multiple single-symbol
sources downstream of this contract.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Protocol

from engine.backtester.events import Bar


class BarFeed(Protocol):
    def __iter__(self) -> Iterator[Bar]: ...


class CsvBarFeed:
    """CSV with columns: timestamp,open,high,low,close,volume.

    Symbol is fixed per feed instance (single-symbol). Phase 1 will add a
    multi-symbol merger that round-robins by timestamp.
    """

    def __init__(self, path: str | Path, symbol: str) -> None:
        self.path = Path(path)
        self.symbol = symbol

    def __iter__(self) -> Iterator[Bar]:
        with self.path.open(newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                yield Bar(
                    symbol=self.symbol,
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(float(row["volume"])),
                )


class InMemoryBarFeed:
    """For unit tests + the smoke synthetic generator. Phase 1 only."""

    def __init__(self, bars: Iterable[Bar]) -> None:
        self._bars = list(bars)

    def __iter__(self) -> Iterator[Bar]:
        return iter(self._bars)
