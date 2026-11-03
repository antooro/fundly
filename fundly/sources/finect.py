import logging
import os
import re
import threading
import time
from datetime import date, timedelta

import httpx

from .base import DGS, ISIN, Source, Inst, QuoteResult, HistoryResult, NavEntry, FUND, ETF, PLAN, in_range

log = logging.getLogger("fundly")

API = "https://api.finect.com/v4"
# finect.com's own web frontend sends a public key with every API request. It is read from the frontend's
# script bundle on first use (see _rediscover), and again whenever the API answers 401 because it rotated.
# FUNDLY_FINECT_KEY sets it by hand.
# Any page links the bundle; a missing page is the smallest one.
DISCOVERY_PAGE = "https://www.finect.com/fundly-key-discovery"
BUNDLE_RE = re.compile(r'<script[^>]+src="(https://www\.finect\.com/[^"]+/vendors-[0-9a-f]+\.js)"')
KEY_RES = (re.compile(r'FINECT_API_KEY\s*:\s*"([A-Za-z0-9]{16,64})"'),
           re.compile(r'append\(\s*"key"\s*,\s*""\.concat\(\s*\w+\s*\|\|\s*"([A-Za-z0-9]{16,64})"'))
REDISCOVER_EVERY = 3600.0


class FinectSource(Source):
    """Spanish fund portal. Its NAVs come through VDOS (not Morningstar); it also covers pension plans."""

    name = "finect"
    search_scope = "global"
    types = frozenset({FUND, ETF, PLAN})
    schemes = frozenset({ISIN, DGS})

    def __init__(self, min_interval: float | None = None):
        super().__init__(min_interval)
        if os.environ.get("FUNDLY_FINECT_KEY"):
            self.http.headers["key"] = os.environ["FUNDLY_FINECT_KEY"]
        self._resolved: dict[str, tuple[str, str, str | None] | None] = {}
        self._key_lock = threading.Lock()
        self._last_discovery = -REDISCOVER_EVERY

    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        if url.startswith(API) and "key" not in self.http.headers:
            self._rediscover(sent=None)
        r = super()._request(method, url, **kw)
        if r.status_code == 401 and url.startswith(API) and self._rediscover(sent=r.request.headers.get("key")):
            r = super()._request(method, url, **kw)
        return r

    def _rediscover(self, sent: str | None) -> bool:
        """Read the current key from finect.com's frontend. True if a different key was found."""
        with self._key_lock:
            if self.http.headers.get("key") != sent:
                return True  # another thread already refreshed it
            if time.monotonic() - self._last_discovery < REDISCOVER_EVERY:
                return False
            self._last_discovery = time.monotonic()
            try:
                page = super()._request("GET", DISCOVERY_PAGE)
                bundle = BUNDLE_RE.search(page.text)
                js = super()._request("GET", bundle.group(1)).text if bundle else ""
                key = next((m.group(1) for rx in KEY_RES if (m := rx.search(js))), None)
            except Exception as e:
                log.warning("finect: could not rediscover its API key: %r", e)
                return False
            if not key or key == sent:
                log.warning("finect: the API rejected the key and no new one was found")
                return False
            log.info("finect: using the API key from its frontend")
            self.http.headers["key"] = key
            return True

    def _search_raw(self, q: str) -> list[dict]:
        r = self._get(f"{API}/search", params={"q": q})
        r.raise_for_status()
        return r.json().get("data") or []

    def _fund_detail(self, isin: str) -> dict | None:
        r = self._get(f"{API}/products/collectives/funds/{isin}")
        if r.status_code in (400, 404):
            return None
        r.raise_for_status()
        return r.json().get("data")

    def _resolve(self, inst: Inst) -> tuple[str, str, str | None] | None:
        """id -> (collection, finect id, currency)."""
        if inst.id in self._resolved:
            return self._resolved[inst.id]
        res = None
        if inst.type == PLAN:
            # Some plans have no product page (404) but their timeseries works with the search id.
            for it in self._search_raw(inst.id):
                e = it.get("entity") or {}
                if it.get("type") == "plan" and e.get("isin") == inst.id:
                    res = ("plans", e["id"], e.get("currency") or "EUR")
                    break
        else:
            d = self._fund_detail(inst.id)
            if d and d.get("id"):
                res = ("funds", d["id"], (d.get("currency") or {}).get("code"))
        self._resolved[inst.id] = res
        return res

    def get_quote(self, inst: Inst) -> QuoteResult | None:
        if inst.type != PLAN:
            d = self._fund_detail(inst.id) or {}
            q = d.get("lastQuote") or {}
            currency = (d.get("currency") or {}).get("code")
            if q.get("price") is None or not currency:
                return None
            return QuoteResult(nav=float(q["price"]), date=q["datetime"][:10], currency=currency, source=self.name)
        today = date.today()
        h = self.get_history(inst, (today - timedelta(days=40)).isoformat(), today.isoformat())
        if not h or not h.entries:
            return None
        last = h.entries[-1]
        return QuoteResult(nav=last.nav, date=last.date, currency=h.currency, source=self.name)

    def get_history(self, inst: Inst, start: str, end: str) -> HistoryResult | None:
        res = self._resolve(inst)
        if not res or not res[2]:
            return None
        coll, fid, currency = res
        if not currency:
            return None
        r = self._get(f"{API}/products/collectives/{coll}/{fid}/timeseries", params={"start": start, "end": end})
        if r.status_code in (400, 404):
            return None
        r.raise_for_status()
        entries = [
            NavEntry(date=p["datetime"][:10], nav=float(p["price"]))
            for p in r.json().get("data") or [] if p.get("datetime") and p.get("price") is not None
        ]
        return HistoryResult(entries=in_range(entries, start, end), currency=currency, source=self.name)

    def search(self, q: str) -> list[dict]:
        hits = []
        for it in self._search_raw(q):
            e = it.get("entity") or {}
            kind = it.get("type")
            if kind not in (FUND, ETF, PLAN) or not e.get("isin"):
                continue
            hits.append({
                "id": e["isin"], "name": e.get("fund_class_name") or it.get("title"), "type": kind,
                "currency": e.get("currency"), "provider": e.get("management_organization"),
                "finect_id": e.get("id"), "alt_name": it.get("title"),
            })
        return hits

    def resolve(self, scheme: str, code: str) -> dict | None:
        if scheme == DGS:
            return next((h for h in self.search(code) if h["id"] == code and h["type"] == PLAN), None)
        if scheme == ISIN:
            d = self._fund_detail(code)
            if d and d.get("id") and d.get("name"):
                return {"id": code, "name": d["name"], "type": ETF if d.get("subtype") == "etf" else FUND,
                        "currency": (d.get("currency") or {}).get("code"), "finect_id": d["id"],
                        "provider": (d.get("managementCompany") or {}).get("name")}
        return None
