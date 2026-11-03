import calendar
import json
import re
from datetime import date, timedelta

from ..ids import canonical, register
from .base import PLAN, HistoryResult, Inst, NavEntry, QuoteResult, Source

API = "https://data.gov.il/api/3/action/datastore_search"
GEMEL, PENSIA = "il-gemel", "il-pensia"
# Each dataset is split by period: 1999–2022, 2023, 2024–today (newest last).
RESOURCES = {
    GEMEL: ["91c849ed-ddc4-472b-bd09-0f5486cea35c", "2016d770-f094-4a2e-983e-797c26479720",
            "a30dcbea-a1d2-482c-ae29-8f781f5025fb"],
    PENSIA: ["a66926f3-e396-4984-a4db-75486751c2f7", "4694d5a7-5284-4f3d-a2cb-5887f43fb55e",
             "6d47d6b5-cb08-488b-b333-f1e717b1e1bd"],
}
HEBREW_OR_DIGITS = re.compile(r"[֐-׿0-9]")

for _scheme in RESOURCES:
    register(_scheme, re.compile(r"^\d{1,6}$").match)


def _month_end(period: int) -> str:
    y, m = divmod(int(period), 100)
    return date(y, m, calendar.monthrange(y, m)[1]).isoformat()


class GemelNetSource(Source):
    """Israeli provident (Gemel-net) and pension (Pensia-net) funds, official open data from data.gov.il.

    They publish monthly returns, not unit prices, so the series is an index built by chaining those
    returns: 100 at the end of the month before the fund's first published month. The base never moves,
    so values stay comparable across requests and cached ranges. Months with no reported return are
    skipped (the chain carries on from the next one).
    """

    name = "gemelnet"
    search_scope = "national"
    search_scripts = frozenset({"hebrew"})
    search_hints = frozenset({"israel", "israeli", "gemel", "pensia", "hishtalmut", "gemelnet"})
    types = frozenset({PLAN})
    schemes = frozenset(RESOURCES)
    timeout = 60

    def _records(self, scheme: str, fund_id: str) -> list[dict]:
        records = []
        for rid in RESOURCES[scheme]:
            r = self._get(API, params={"resource_id": rid, "filters": json.dumps({"FUND_ID": int(fund_id)}),
                                       "sort": "REPORT_PERIOD", "limit": 1000})
            r.raise_for_status()
            records += r.json().get("result", {}).get("records", [])
        return sorted(records, key=lambda x: int(x["REPORT_PERIOD"]))

    def _index(self, inst: Inst) -> HistoryResult | None:
        records = self._records(inst.scheme, inst.code)
        if not records:
            return None
        first = int(records[0]["REPORT_PERIOD"])
        before = date(first // 100, first % 100, 1) - timedelta(days=1)
        level, entries = 100.0, [NavEntry(before.isoformat(), 100.0)]
        for rec in records:
            try:
                level *= 1 + float(rec["MONTHLY_YIELD"]) / 100
            except (TypeError, ValueError):
                continue
            entries.append(NavEntry(_month_end(rec["REPORT_PERIOD"]), round(level, 6)))
        return HistoryResult(entries=entries, currency="ILS", source=self.name, price_type="index")

    def get_quote(self, inst: Inst) -> QuoteResult | None:
        h = self._index(inst)
        if not h or len(h.entries) < 2:
            return None
        last = h.entries[-1]
        return QuoteResult(nav=last.nav, date=last.date, currency="ILS", source=self.name, price_type="index")

    def get_history(self, inst: Inst, start: str, end: str) -> HistoryResult | None:
        h = self._index(inst)
        if h:
            h.entries = [e for e in h.entries if start <= e.date <= end]
        return h

    @staticmethod
    def _hit(scheme: str, rec: dict) -> dict:
        return {"id": canonical(scheme, str(rec["FUND_ID"])), "name": rec.get("FUND_NAME"), "type": PLAN,
                "currency": "ILS", "provider": rec.get("MANAGING_CORPORATION")}

    def resolve(self, scheme: str, code: str) -> dict | None:
        if scheme not in RESOURCES:
            return None
        for rid in reversed(RESOURCES[scheme]):
            r = self._get(API, params={"resource_id": rid, "filters": json.dumps({"FUND_ID": int(code)}),
                                       "sort": "REPORT_PERIOD desc", "limit": 1})
            r.raise_for_status()
            records = r.json().get("result", {}).get("records", [])
            if records:
                return self._hit(scheme, records[0])
        return None

    def search(self, q: str) -> list[dict]:
        # Names are in Hebrew; skip the two requests for queries that can't match a name or a fund id.
        if not HEBREW_OR_DIGITS.search(q):
            return []
        hits = []
        for scheme, resources in RESOURCES.items():
            r = self._get(API, params={"resource_id": resources[-1], "q": q, "distinct": "true",
                                       "fields": "FUND_ID,FUND_NAME,MANAGING_CORPORATION", "limit": 20})
            r.raise_for_status()
            hits += [self._hit(scheme, rec) for rec in r.json().get("result", {}).get("records", [])]
        return hits
