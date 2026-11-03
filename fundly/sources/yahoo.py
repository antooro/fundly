from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

from .base import ISIN, Source, Inst, QuoteResult, HistoryResult, NavEntry, FUND, ETF, in_range
from ..ids import TICKER

SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart"


def day_of(ts: int, meta: dict) -> str:
    """Calendar day of a bar in the exchange's own time zone. Daily FX bars are stamped at local midnight,
    which in UTC falls on the previous day; the current gmtoffset alone is wrong across DST changes."""
    tz: tzinfo
    try:
        tz = ZoneInfo(meta["exchangeTimezoneName"])
    except Exception:
        tz = timezone(timedelta(seconds=meta.get("gmtoffset") or 0))
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")


class YahooSource(Source):
    """Last resort. Fund tickers (0P...) are Morningstar ids, so for funds this is not an independent source."""

    name = "yahoo"
    types = frozenset({FUND, ETF})
    schemes = frozenset({ISIN, TICKER})

    def __init__(self, min_interval: float | None = None):
        super().__init__(min_interval)
        self._tickers: dict[str, tuple[str, str] | None] = {}

    def _ticker(self, inst: Inst) -> tuple[str, str] | None:
        """-> (Yahoo symbol, price_type)."""
        if inst.id not in self._tickers:
            r = self._get(SEARCH_URL, params={"q": inst.code, "quotesCount": 5, "newsCount": 0})
            r.raise_for_status()
            quotes = r.json().get("quotes", [])
            if inst.scheme == TICKER:
                # A ticker search returns look-alikes too; only the exact symbol is the instrument asked for.
                quotes = [x for x in quotes if x.get("symbol", "").upper() == inst.code]
            pick = quotes[0] if quotes else None
            self._tickers[inst.id] = (
                (pick["symbol"], "nav" if pick.get("quoteType") == "MUTUALFUND" else "market") if pick else None
            )
        return self._tickers[inst.id]

    def _chart(self, ticker: str, params: dict) -> dict | None:
        r = self._get(f"{CHART_URL}/{ticker}", params=params)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        result = r.json().get("chart", {}).get("result") or []
        return result[0] if result else None

    def get_quote(self, inst: Inst) -> QuoteResult | None:
        t = self._ticker(inst)
        if not t:
            return None
        meta = (self._chart(t[0], {"range": "5d", "interval": "1d"}) or {}).get("meta", {})
        ts = meta.get("regularMarketTime")
        if meta.get("regularMarketPrice") is None or not meta.get("currency") or not ts:
            return None
        return QuoteResult(nav=float(meta["regularMarketPrice"]), date=day_of(ts, meta),
                           currency=meta["currency"], source=self.name, price_type=t[1])

    def get_history(self, inst: Inst, start: str, end: str) -> HistoryResult | None:
        t = self._ticker(inst)
        if not t:
            return None
        p1 = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
        p2 = int(datetime.fromisoformat(end).replace(tzinfo=timezone.utc).timestamp()) + 86400
        res = self._chart(t[0], {"period1": p1, "period2": p2, "interval": "1d"})
        if not res or not res.get("meta", {}).get("currency"):
            return None
        closes = res.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        entries = [NavEntry(date=day_of(ts, res["meta"]), nav=round(float(c), 6))
                   for ts, c in zip(res.get("timestamp", []), closes, strict=True) if c is not None]
        return HistoryResult(entries=in_range(entries, start, end), currency=res["meta"]["currency"],
                             source=self.name, price_type=t[1])
