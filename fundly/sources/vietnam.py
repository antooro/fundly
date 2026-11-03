import re
from datetime import date, timedelta

from .. import ids
from ..cache import normalize
from .base import Cached, FUND, HistoryResult, Inst, NavEntry, QuoteResult, Source, in_range

SCHEME = "vn-fmarket"
ids.register(SCHEME, re.compile(r"^[A-Z0-9]{2,12}$").match, upper=True)

API = "https://api.fmarket.vn/res"
LIST_TTL = 12 * 3600
PAGE = 100
MAX_PAGES = 50  # the platform lists ~70 funds; a server that ignores `page` must not loop forever


def _norm(text: str | None) -> str:
    # NFKD leaves the Vietnamese "đ" intact, so "dau tu" would never match "đầu tư".
    return normalize(text).replace("đ", "d")


class FmarketSource(Source):
    """Vietnamese open-ended funds as distributed on fmarket.vn, with daily NAV since inception."""

    name = "fmarket"
    search_scope = "national"
    search_scripts = frozenset({"vietnamese"})
    search_hints = frozenset({"vietnam", "viet", "quy", "vnd", "vinacapital", "fmarket"})
    types = frozenset({FUND})
    schemes = frozenset({SCHEME})

    def __init__(self, min_interval: float | None = None):
        super().__init__(min_interval)
        self._funds = Cached(self._fetch_list, LIST_TTL)

    def _list(self) -> dict[str, dict]:
        return self._funds.get()

    def _fetch_list(self) -> dict[str, dict]:
        funds: dict[str, dict] = {}
        for page in range(1, MAX_PAGES + 1):
            before = len(funds)
            r = self._post(f"{API}/products/filter", json={
                "types": ["NEW_FUND", "TRADING_FUND"], "page": page, "pageSize": PAGE,
                "sortField": "navTo6Months", "sortOrder": "DESC",
            })
            r.raise_for_status()
            rows = (r.json().get("data") or {}).get("rows") or []
            for row in rows:
                if row.get("code") and row.get("id") is not None:
                    funds[row["code"].upper()] = {
                        "product_id": row["id"], "code": row["code"].upper(),
                        "name": row.get("name") or row.get("shortName") or row["code"],
                        "provider": (row.get("owner") or {}).get("name"),
                        "category": (row.get("dataFundAssetType") or {}).get("name"),
                    }
            if len(rows) < PAGE or len(funds) == before:
                break
        return funds

    def _hit(self, f: dict) -> dict:
        return {"id": ids.canonical(SCHEME, f["code"]), "name": f["name"], "type": FUND, "currency": "VND",
                "provider": f["provider"], "category": f["category"], "alt_name": f["code"]}

    def _history(self, inst: Inst, start: str | None, end: str) -> HistoryResult | None:
        f = self._list().get(inst.code)
        if not f:
            return None
        r = self._post(f"{API}/product/get-nav-history", json={
            "isAllData": 0 if start else 1, "productId": f["product_id"],
            "fromDate": start.replace("-", "") if start else None, "toDate": end.replace("-", ""),
        })
        r.raise_for_status()
        entries = [NavEntry(p["navDate"][:10], float(p["nav"]))
                   for p in r.json().get("data") or [] if p.get("navDate") and p.get("nav") is not None]
        return HistoryResult(entries=sorted(entries, key=lambda e: e.date), currency="VND", source=self.name)

    def get_quote(self, inst: Inst) -> QuoteResult | None:
        today = date.today()
        h = self._history(inst, (today - timedelta(days=40)).isoformat(), today.isoformat())
        if not h or not h.entries:
            return None
        last = h.entries[-1]
        return QuoteResult(nav=last.nav, date=last.date, currency="VND", source=self.name)

    def get_history(self, inst: Inst, start: str, end: str) -> HistoryResult | None:
        h = self._history(inst, start, end)
        if h:
            h.entries = in_range(h.entries, start, end)
        return h

    def search(self, q: str) -> list[dict]:
        words = _norm(q).split()
        if not words:
            return []
        return [self._hit(f) for f in self._list().values()
                if all(w in _norm(f"{f['code']} {f['name']} {f['provider'] or ''}") for w in words)]

    def resolve(self, scheme: str, code: str) -> dict | None:
        if scheme != SCHEME:
            return None
        f = self._list().get(code.upper())
        return self._hit(f) if f else None
