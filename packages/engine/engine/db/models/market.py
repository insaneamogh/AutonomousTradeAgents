"""Market data the system RECORDS because it cannot be bought back later.

    iv_history   one constant-maturity ATM IV snapshot per underlying per day

An IV rank is a percentile of a symbol's own IV history. Alpaca's free tier
gives only the current snapshot, so the history must be recorded daily,
starting before anyone needs it. Every day not recorded is a day that
never counts toward the 60 observations `iv_rank` requires.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from engine.db.base import Base


class IvHistory(Base):
    __tablename__ = "iv_history"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    """The ET trading date of the snapshot. One row per (symbol, day); a
    re-run the same day overwrites rather than duplicating."""
    atm_iv_30d: Mapped[Decimal | None] = mapped_column(Numeric(8, 5), nullable=True)
    atm_iv_60d: Mapped[Decimal | None] = mapped_column(Numeric(8, 5), nullable=True)
    n_expiries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    feed: Mapped[str] = mapped_column(String(20), nullable=False)
    """'indicative' or 'opra'. A rank computed across a feed change mixes
    two different IV sources, so the feed is recorded with every row."""
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
