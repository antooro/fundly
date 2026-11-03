import json
import logging
import re

import httpx

from .. import ids
from ..cache import normalize
from .base import Cached, ETF, FUND, ISIN, HistoryResult, Inst, NavEntry, QuoteResult, Source, in_range
from .id_managers import MANAGERS, Manager, manager_key

SCHEME = "id-pasardana"
ids.register(SCHEME, re.compile(r"^\d{1,7}$").match)

GETALL_URL = "https://pasardana.id/api/FundSearchResult/GetAll"
# The public fund page's own data call; its NetAssetValues cover the last ~12 months.
SNAPSHOT_URL = "https://pasardana.id/api/FundService/GetSnapshot"
LIST_TTL = 6 * 3600
MANAGER_LIST_TTL = 24 * 3600
log = logging.getLogger("fundly")
# Pasardana's Currency enum: 0 for rupiah funds, 1 for the dollar ones ("... USD ...", "Obligasi Dollar"),
# whose NAVs sit around 1 instead of around 1,000.
CURRENCIES = {0: "IDR", 1: "USD"}


class PasardanaSource(Source):
    """Indonesian mutual funds (reksa dana) and ETFs from pasardana.id.

    History: the fund page's last ~12 months (older data is a paid product), plus the full history since launch
    that some managers publish on their own sites (Batavia, Trimegah, Avrist, Syailendra; see id_managers). With a
    cache folder, every history
    and every daily list fundly downloads is also kept on disk, so a fund's history keeps growing past those
    12 months on each machine instead of sliding along with them.
    The full list (~6.6 MB) is downloaded at most once every 6 hours per process.
    """

    name = "pasardana"
    search_scope = "national"
    search_hints = frozenset({"indonesia", "indonesian", "reksa", "dana", "syariah", "idr", "sucorinvest", "mandiri", "bni",
                               "batavia", "trimegah", "bahana", "danareksa"})
    types = frozenset({FUND, ETF})
    schemes = frozenset({ISIN, SCHEME})
    timeout = 60

    def __init__(self, min_interval: float | None = None):
        super().__init__(min_interval)
        self._lists = Cached(self._fetch, LIST_TTL)
        self._managers = {m.name: Cached(self._manager_list(m), MANAGER_LIST_TTL) for m in MANAGERS}

    def _manager_list(self, m: Manager):
        return lambda: {manager_key(name, m.provider): key for name, key in m.catalogue(self._get).items()}

    def supports(self, inst: Inst) -> bool:
        if inst.scheme == ISIN and not inst.code.startswith("ID"):
            return False
        return super().supports(inst)

    def _fetch(self) -> tuple[dict[str, dict], dict[str, dict]]:
        """-> (by ISIN, by Pasardana id)."""
        r = self._get(GETALL_URL)
        r.raise_for_status()
        by_isin, by_id = {}, {}
        for x in r.json():
            if x.get("Id") is None or not x.get("Name"):
                continue
            isin = (x.get("IsinCode") or "").strip().upper()
            f = {
                "id": str(x["Id"]), "name": x["Name"].strip(),
                # Some rows carry internal 16-character codes in IsinCode; only real ISINs count.
                "isin": isin if ids.is_valid_isin(isin) else None,
                "nav": x.get("NetAssetValue"), "date": (x.get("LastUpdate") or "")[:10],
                "currency": CURRENCIES.get(x.get("Currency")),
                "provider": x.get("InvestmentManagerName"), "etf": bool(x.get("ExchangeTradedFund")),
            }
            by_id[f["id"]] = f
            if f["isin"]:
                by_isin[f["isin"]] = f
        self._keep_list(by_id)
        return by_isin, by_id

    def _keep_list(self, by_id: dict[str, dict]):
        """Each fund's latest NAV from the list goes into its kept history (one file per day for all funds)."""
        if self.files_dir is None:
            return
        by_day: dict[str, dict[str, float]] = {}
        for f in by_id.values():
            if isinstance(f["nav"], int | float) and f["nav"] > 0 and len(f["date"]) == 10:
                by_day.setdefault(f["date"], {})[f["id"]] = float(f["nav"])
        for day, values in by_day.items():
            kept = self._read_json(f"list-{day}.json")
            if not values.items() <= kept.items():
                self._write_atomic(self.files_dir / f"list-{day}.json", json.dumps({**kept, **values}).encode())

    def _read_json(self, name: str) -> dict:
        path = self.files_dir / name if self.files_dir else None
        try:
            data = json.loads(path.read_text()) if path is not None and path.exists() else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def _find(self, scheme: str, code: str) -> dict | None:
        by_isin, by_id = self._lists.get()
        return (by_isin if scheme == ISIN else by_id).get(code)

    def _hit(self, f: dict) -> dict:
        return {"id": f["isin"] or ids.canonical(SCHEME, f["id"]), "name": f["name"],
                "type": ETF if f["etf"] else FUND, "currency": f["currency"], "provider": f["provider"],
                "aliases": [ids.canonical(SCHEME, f["id"])]}

    def get_quote(self, inst: Inst) -> QuoteResult | None:
        f = self._find(inst.scheme, inst.code)
        if not f or f["nav"] is None or not f["currency"] or not f["date"]:
            return None
        return QuoteResult(nav=float(f["nav"]), date=f["date"], currency=f["currency"], source=self.name)

    def get_history(self, inst: Inst, start: str, end: str) -> HistoryResult | None:
        f = self._find(inst.scheme, inst.code)
        if not f or not f["currency"]:
            return None
        r = self._get(SNAPSHOT_URL, params={"fundId": f["id"], "username": "anonymous"})
        r.raise_for_status()
        data = r.json()  # unknown ids answer a bare null
        rows = data.get("NetAssetValues") if isinstance(data, dict) else None
        found = {}
        for x in rows if isinstance(rows, list) else []:
            if not isinstance(x, dict):
                continue
            day, value = (x.get("Date") or "")[:10], x.get("Value")
            if len(day) == 10 and isinstance(value, int | float) and value > 0:
                found[day] = float(value)
        found = {**self._manager_history(f), **self._kept_history(f["id"], found)}
        entries = in_range([NavEntry(d, v) for d, v in found.items()], start, end)
        return HistoryResult(entries=entries, currency=f["currency"], source=self.name) if entries else None

    def _manager_history(self, f: dict) -> dict[str, float]:
        """The manager's own full history, when it publishes one; {} otherwise or if its site fails."""
        provider = (f["provider"] or "").lower()
        m = next((m for m in MANAGERS if m.provider in provider), None)
        if m is None:
            return {}
        try:
            key = self._managers[m.name].get().get(manager_key(f["name"], m.provider))
            return m.history(self._get, key) if key else {}
        except (httpx.HTTPError, ValueError) as e:
            log.info("%s: %s history unavailable (%s)", self.name, m.name, e)
            return {}

    def _kept_history(self, fund_id: str, fetched: dict[str, float]) -> dict[str, float]:
        """Merge what was just fetched into this fund's history on disk (fetched values win) and return it all,
        with the NAVs kept from daily lists filling any day in between."""
        if self.files_dir is None:
            return fetched
        name = f"history-{fund_id}.json"
        kept = {d: v for d, v in self._read_json(name).items() if isinstance(v, int | float)}
        merged = {**kept, **fetched}
        if merged != kept:
            self._write_atomic(self.files_dir / name, json.dumps(merged).encode())
        for path in self.files_dir.glob("list-*.json"):
            day = path.stem[len("list-"):]
            if day not in merged and isinstance(value := self._read_json(path.name).get(fund_id), int | float):
                merged[day] = value
        return merged

    def search(self, q: str) -> list[dict]:
        words = normalize(q).split()
        if not words:
            return []
        return [self._hit(f) for f in self._lists.get()[1].values()
                if all(w in normalize(f"{f['name']} {f['provider'] or ''} {f['isin'] or ''}") for w in words)]

    def resolve(self, scheme: str, code: str) -> dict | None:
        if scheme not in self.schemes:
            return None
        f = self._find(scheme, code)
        return self._hit(f) if f else None
