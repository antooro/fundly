import datetime as dt
from dataclasses import dataclass, field, fields
from datetime import date
from typing import Literal

# "nav": official net asset value; "market": exchange close; "index": series rebuilt from published
# returns (some pension systems publish returns, not unit prices).
PriceType = Literal["nav", "market", "index"]


def _plain(value):
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


class _Plain:
    def to_dict(self) -> dict:
        """JSON-ready dict: dates as ISO strings, nested results as dicts."""
        return {f.name: _plain(getattr(self, f.name)) for f in fields(self)}  # type: ignore[arg-type]


@dataclass(frozen=True)
class Instrument(_Plain):
    id: str  # ISIN, DGS code (N####/S####) for Spanish pension plans, or "scheme:code" (see fundly.ids)
    id_type: str  # "isin", "dgs", "ticker", "ms", ...
    name: str
    type: Literal["etf", "fund", "plan"]
    currency: str | None = None
    domicile: str | None = None
    provider: str | None = None  # fund manager / issuer
    category: str | None = None
    ter: float | None = None
    size_eur: int | None = None
    identifiers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Quote(_Plain):
    id: str
    name: str | None
    price: float
    date: date
    currency: str
    price_type: PriceType  # "nav": official net asset value; "market": exchange close
    source: str
    cached: bool
    # Set only when a conversion was asked for (quote(..., currency=...)): the source's own figure and the rate used.
    original_price: float | None = None
    original_currency: str | None = None
    fx_rate: float | None = None
    fx_date: dt.date | None = None
    fx_source: str | None = None
    # Data-quality notes, each starting with a code: "stale: ...", "outlier: ...", "replaced: ...".
    warnings: tuple[str, ...] = ()

    @property
    def age_days(self) -> int:
        return (dt.date.today() - self.date).days

    @property
    def stale(self) -> bool:
        """The last published price is old for this kind of product (a fund that stopped reporting?)."""
        return any(w.startswith("stale") for w in self.warnings)

    @property
    def suspicious(self) -> bool:
        """The price is far from what other data says about this instrument."""
        return any(w.startswith("outlier") for w in self.warnings)


@dataclass(frozen=True)
class PricePoint(_Plain):
    date: date
    price: float


@dataclass(frozen=True)
class History(_Plain):
    id: str
    name: str | None
    currency: str
    price_type: PriceType
    source: str
    cached: bool
    points: list[PricePoint]
    # Set only when converted (history(..., currency=...)): each point uses the rate in force on its date.
    original_currency: str | None = None
    fx_source: str | None = None
    # Data-quality notes: isolated spikes and level jumps that look like data errors (points are kept as published).
    warnings: tuple[str, ...] = ()

    def to_pandas(self):
        """Series indexed by date (requires pandas)."""
        try:
            import pandas as pd
        except ImportError as e:
            raise ImportError("to_pandas() needs pandas: pip install pandas") from e
        return pd.Series(
            [p.price for p in self.points],
            index=pd.DatetimeIndex([p.date for p in self.points], name="date"),
            name=self.id,
        )


@dataclass(frozen=True)
class Comparison(_Plain):
    id: str
    name: str | None
    quotes: list[Quote]
    missing: dict[str, str]  # sources that support this type but returned nothing, with the reason
    nav_spread_pct: float | None  # (max-min)/min of the NAVs sharing the most common date and currency
