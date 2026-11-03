import json
import re
from datetime import datetime, timedelta, timezone

from .. import ids
from .base import FUND, Source, Inst, QuoteResult, HistoryResult, NavEntry, in_range

CN = "cn"
ids.register(CN, re.compile(r"^\d{6}$").match)

SEARCH_URL = "https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx"
DATA_URL = "https://fund.eastmoney.com/pingzhongdata/{code}.js"
TREND_RE = re.compile(r"var Data_netWorthTrend\s*=\s*(\[.*?\]);", re.S)
INCOME_RE = re.compile(r"var Data_millionCopiesIncome\s*=\s*(\[.*?\]);", re.S)
MONEY_MARKET_RE = re.compile(r"var ishb\s*=\s*true")
CHINA_TZ = timezone(timedelta(hours=8))
FUND_CATEGORY = 700  # search results also include stocks, managers, etc.


def _day(ms: int) -> str:
    # Timestamps are midnight China time.
    return datetime.fromtimestamp(ms / 1000, CHINA_TZ).strftime("%Y-%m-%d")


class EastmoneySource(Source):
    """Chinese public funds (6-digit codes) from Eastmoney / Tiantian Fund.

    Money-market funds keep their unit value at 1 and publish the daily income per 10,000 units instead
    of a NAV; for those, the series is an index of compounded income (1.0 at the first published day),
    returned with price_type "index".
    """

    name = "eastmoney"
    search_scope = "national"
    search_scripts = frozenset({"han"})
    search_hints = frozenset({"china", "chinese", "cny"})
    types = frozenset({FUND})
    schemes = frozenset({CN})

    def _search_raw(self, key: str) -> list[dict]:
        r = self._get(SEARCH_URL, params={"m": 1, "key": key})
        r.raise_for_status()
        return [d for d in r.json().get("Datas") or [] if d.get("CATEGORY") == FUND_CATEGORY and d.get("CODE")]

    @staticmethod
    def _hit(d: dict) -> dict:
        info = d.get("FundBaseInfo") or {}
        return {"id": ids.canonical(CN, d["CODE"]), "name": d.get("NAME"), "type": FUND, "currency": "CNY",
                "provider": info.get("JJGS"), "category": info.get("FTYPE")}

    def _exact(self, code: str) -> dict | None:
        return next((d for d in self._search_raw(code) if d["CODE"] == code), None)

    def resolve(self, scheme: str, code: str) -> dict | None:
        d = self._exact(code) if scheme == CN else None
        return self._hit(d) if d else None

    def search(self, q: str) -> list[dict]:
        return [self._hit(d) for d in self._search_raw(q)]

    def get_quote(self, inst: Inst) -> QuoteResult | None:
        info = ((self._exact(inst.code) or {}).get("FundBaseInfo") or {})
        # For money-market funds DWJZ is the income per 10,000 units, not a NAV.
        if (info.get("DWJZ") is not None and info.get("FSRQ")
                and not str(info.get("FTYPE") or "").startswith("货币")):
            return QuoteResult(nav=float(info["DWJZ"]), date=info["FSRQ"][:10], currency="CNY", source=self.name)
        h = self.get_history(inst, "1900-01-01", "2999-12-31")
        if not h or not h.entries:
            return None
        return QuoteResult(nav=h.entries[-1].nav, date=h.entries[-1].date, currency="CNY", source=self.name,
                           price_type=h.price_type)

    def get_history(self, inst: Inst, start: str, end: str) -> HistoryResult | None:
        r = self._get(DATA_URL.format(code=inst.code))
        if r.status_code == 404:
            return None
        r.raise_for_status()
        r.encoding = "utf-8"
        if MONEY_MARKET_RE.search(r.text):
            m = INCOME_RE.search(r.text)
            if not m:
                return None
            entries, level = [], 1.0
            for ms, income in json.loads(m.group(1)):
                if ms is None or income is None:
                    continue
                level *= 1 + float(income) / 10_000
                entries.append(NavEntry(date=_day(ms), nav=round(level, 8)))
            return HistoryResult(entries=in_range(entries, start, end), currency="CNY", source=self.name,
                                 price_type="index")
        m = TREND_RE.search(r.text)
        if not m:
            return None
        entries = [NavEntry(date=_day(p["x"]), nav=float(p["y"]))
                   for p in json.loads(m.group(1)) if p.get("x") is not None and p.get("y") is not None]
        return HistoryResult(entries=in_range(entries, start, end), currency="CNY", source=self.name)
