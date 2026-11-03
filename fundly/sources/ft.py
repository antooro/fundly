import re
from datetime import date

from ..ids import canonical, register
from .base import ISIN, Source, Inst, QuoteResult, HistoryResult, NavEntry, FUND, ETF, in_range

SEARCH_URL = "https://markets.ft.com/data/searchapi/searchsecurities"
SERIES_URL = "https://markets.ft.com/data/chartapi/series"
SYMBOL = "ft"  # FT's own symbol, e.g. "ft:0P00009VB8" for UK pension/life funds that have no ISIN

register(SYMBOL, re.compile(r"^[A-Z0-9][A-Z0-9.:\-]{1,30}$").match, upper=True)


class FTSource(Source):
    """Financial Times markets data. Covers Spanish funds; fund NAVs match Morningstar to the decimal."""

    name = "ft"
    types = frozenset({FUND, ETF})
    schemes = frozenset({ISIN, SYMBOL})

    def _search(self, query: str) -> list[dict]:
        r = self._get(SEARCH_URL, params={"query": query})
        r.raise_for_status()
        return (r.json().get("data") or {}).get("security") or []

    def _by_symbol(self, symbol: str) -> dict | None:
        # Search is fuzzy: only the exact symbol is the instrument asked for.
        return next((s for s in self._search(symbol) if s.get("symbol", "").upper() == symbol), None)

    def resolve(self, scheme: str, code: str) -> dict | None:
        if scheme != SYMBOL:
            return None
        s = self._by_symbol(code)
        return {"id": canonical(SYMBOL, code), "name": s.get("name"), "type": FUND} if s else None

    def _security(self, inst: Inst) -> tuple[str, str] | None:
        """-> (FT xid, price_type)."""
        if inst.scheme == SYMBOL:
            s = self._by_symbol(inst.code)
            return (s["xid"], "nav") if s else None
        secs = self._search(inst.id)
        # Fund symbols are "ISIN:CCY". Search is fuzzy, so anything else is not trusted for funds.
        funds = [s for s in secs if s.get("symbol", "").startswith(inst.id + ":")]
        if funds:
            pick = next((s for s in funds if s["symbol"].endswith(":" + str(inst.currency))), funds[0])
            return pick["xid"], "nav"
        if inst.type != ETF or not secs:
            return None
        # ETF listings are "TICKER:VENUE:CCY".
        same_ccy = [s for s in secs if s["symbol"].endswith(":" + str(inst.currency))]
        listing = same_ccy[0] if same_ccy else next((s for s in secs if s.get("isPrimary")), secs[0])
        return listing["xid"], "market"

    def _series(self, inst: Inst, days: int) -> HistoryResult | None:
        sec = self._security(inst)
        if not sec:
            return None
        xid, price_type = sec
        r = self._post(SERIES_URL, json={
            "days": days, "dataNormalized": False, "dataPeriod": "Day", "dataInterval": 1,
            "realtime": False, "yFormat": "0.###", "timeServiceFormat": "JSON",
            "returnDateType": "ISO8601", "elements": [{"Type": "price", "Symbol": xid}],
        })
        r.raise_for_status()
        d = r.json()
        el = (d.get("Elements") or [None])[0]
        if not el:
            return None
        close: list = next((c["Values"] for c in el["ComponentSeries"] if c["Type"] == "Close"), [])
        entries = [NavEntry(date=dt[:10], nav=float(v)) for dt, v in zip(d.get("Dates", []), close, strict=True) if v is not None]
        return HistoryResult(entries=entries, currency=el["Currency"], source=self.name, price_type=price_type)

    def get_quote(self, inst: Inst) -> QuoteResult | None:
        h = self._series(inst, 10)
        if not h or not h.entries:
            return None
        last = h.entries[-1]
        return QuoteResult(nav=last.nav, date=last.date, currency=h.currency, source=self.name, price_type=h.price_type)

    def get_history(self, inst: Inst, start: str, end: str) -> HistoryResult | None:
        h = self._series(inst, (date.today() - date.fromisoformat(start)).days + 5)
        if h:
            h.entries = in_range(h.entries, start, end)
        return h
