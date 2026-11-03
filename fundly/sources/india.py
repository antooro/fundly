import re
from datetime import datetime

from .. import ids
from ..cache import normalize
from .base import Cached, ISIN, FUND, PLAN, Source, Inst, QuoteResult, HistoryResult, NavEntry, in_range

AMFI, NPS = "in-amfi", "in-nps"
ids.register(AMFI, re.compile(r"^\d{4,7}$").match)
ids.register(NPS, re.compile(r"^SM\d{6}$").match, upper=True)

NAVALL_URL = "https://portal.amfiindia.com/spages/NAVAll.txt"
MFAPI_URL = "https://api.mfapi.in/mf/{code}"
NPS_API = "https://npsnav.in/api"
LIST_TTL = 6 * 3600


def _date(s: str, fmt: str) -> str | None:
    try:
        return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _matches(q: str, text: str) -> bool:
    words = normalize(q).split()
    hay = normalize(text)
    return bool(words) and all(w in hay for w in words)


class AMFISource(Source):
    """Indian mutual funds. Latest NAV from AMFI's official daily file (all schemes); history from
    api.mfapi.in, a community mirror of AMFI's NAV history."""

    name = "amfi"
    search_scope = "national"
    search_hints = frozenset({"india", "indian", "amfi", "elss", "idcw", "hdfc", "icici", "sbi", "axis", "kotak", "nippon",
                              "uti", "aditya", "birla", "mirae", "dsp", "tata", "quant", "parag", "parikh", "motilal",
                              "oswal", "idfc", "bandhan", "edelweiss", "canara", "sundaram", "baroda", "lic"})
    types = frozenset({FUND})
    schemes = frozenset({ISIN, AMFI})

    def __init__(self, min_interval: float | None = None):
        super().__init__(min_interval)
        self._navall = Cached(self._load_navall, LIST_TTL)

    def supports(self, inst: Inst) -> bool:
        if inst.scheme == ISIN and not inst.code.startswith("IN"):
            return False
        return super().supports(inst)

    def _load_navall(self) -> dict:
        r = self._get(NAVALL_URL)
        r.raise_for_status()
        by_code: dict[str, dict] = {}
        by_isin: dict[str, dict] = {}
        amc = category = None
        for line in r.text.splitlines():
            line = line.strip()
            parts = line.split(";")
            if len(parts) < 8:
                # Header lines between rows: "Open Ended Schemes(Equity Scheme - Mid Cap Fund)", then the AMC name.
                if "Schemes(" in line:
                    category = line[line.index("(") + 1:].rstrip(")").strip() or None
                elif line:
                    amc = line
                continue
            code, isin1, isin2, name, plan, option, nav, day = (p.strip() for p in parts[:8])
            if not code.isdigit():
                continue
            isins = [i for i in (isin1, isin2) if ids.ISIN_RE.match(i)]
            try:
                value = float(nav)
            except ValueError:
                value = None
            rec = {
                "code": code, "isins": isins, "amc": amc, "category": category,
                "name": " - ".join([name] + [p for p in (plan, option) if p and p not in name]),
                "nav": value, "date": _date(day, "%d-%b-%Y"),
            }
            by_code[code] = rec
            for i in isins:
                by_isin.setdefault(i, rec)
        return {"code": by_code, "isin": by_isin}

    def _record(self, scheme: str, code: str) -> dict | None:
        data = self._navall.get()
        return data["isin"].get(code) if scheme == ISIN else data["code"].get(code)

    @staticmethod
    def _hit(rec: dict) -> dict:
        return {
            "id": rec["isins"][0] if rec["isins"] else ids.canonical(AMFI, rec["code"]),
            "name": rec["name"], "type": FUND, "currency": "INR",
            "provider": rec["amc"], "category": rec["category"], "amfi_code": rec["code"],
            "aliases": [ids.canonical(AMFI, rec["code"])],
        }

    def resolve(self, scheme: str, code: str) -> dict | None:
        if scheme not in self.schemes or (scheme == ISIN and not code.startswith("IN")):
            return None
        rec = self._record(scheme, code)
        return self._hit(rec) if rec else None

    def get_quote(self, inst: Inst) -> QuoteResult | None:
        rec = self._record(inst.scheme, inst.code)
        if not rec or rec["nav"] is None or not rec["date"]:
            return None
        return QuoteResult(nav=rec["nav"], date=rec["date"], currency="INR", source=self.name)

    def get_history(self, inst: Inst, start: str, end: str) -> HistoryResult | None:
        rec = self._record(inst.scheme, inst.code)
        if not rec:
            return None
        r = self._get(MFAPI_URL.format(code=rec["code"]))
        if r.status_code == 404:
            return None
        r.raise_for_status()
        entries = []
        for p in r.json().get("data") or []:
            d = _date(p.get("date", ""), "%d-%m-%Y")
            if not d:
                continue
            try:
                entries.append(NavEntry(date=d, nav=float(p["nav"])))
            except (TypeError, ValueError, KeyError):
                continue
        entries = [e for e in entries if e.date]
        return HistoryResult(entries=in_range(entries, start, end), currency="INR", source=self.name)

    def search(self, q: str) -> list[dict]:
        recs = self._navall.get()["code"].values()
        return [self._hit(r) for r in recs if _matches(q, f"{r['name']} {r['amc'] or ''}")][:30]


class NPSSource(Source):
    """India's National Pension System schemes, via npsnav.in (a community API over NPS Trust data)."""

    name = "npsnav"
    search_scope = "national"
    search_hints = frozenset({"nps", "pfrda", "tier", "atal"})
    types = frozenset({PLAN})
    schemes = frozenset({NPS})

    def __init__(self, min_interval: float | None = None):
        super().__init__(min_interval)
        self._latest = Cached(self._load_latest, LIST_TTL)

    def supports(self, inst: Inst) -> bool:
        # The scheme alone says it's a pension plan, even before the catalogue knows the type.
        return inst.scheme in self.schemes and inst.type in (None, PLAN)

    def _load_latest(self) -> dict:
        r = self._get(f"{NPS_API}/latest")
        r.raise_for_status()
        return {row["Scheme Code"]: row for row in r.json().get("data") or [] if row.get("Scheme Code")}

    @staticmethod
    def _hit(row: dict) -> dict:
        return {"id": ids.canonical(NPS, row["Scheme Code"]), "name": row.get("Scheme Name"), "type": PLAN,
                "currency": "INR", "provider": row.get("PFM Name")}

    def resolve(self, scheme: str, code: str) -> dict | None:
        row = self._latest.get().get(code) if scheme == NPS else None
        return self._hit(row) if row else None

    def get_quote(self, inst: Inst) -> QuoteResult | None:
        row = self._latest.get().get(inst.code)
        if not row:
            return None
        day = _date(row.get("Last Updated", ""), "%d-%m-%Y")
        try:
            nav = float(row["NAV"])
        except (TypeError, ValueError, KeyError):
            return None
        return QuoteResult(nav=nav, date=day, currency="INR", source=self.name) if day else None

    def get_history(self, inst: Inst, start: str, end: str) -> HistoryResult | None:
        r = self._get(f"{NPS_API}/historical/{inst.code}")
        if r.status_code in (400, 404):
            return None
        r.raise_for_status()
        entries = []
        for p in r.json().get("data") or []:
            d = _date(str(p.get("date", "")), "%d-%m-%Y")
            if d and p.get("nav") is not None:
                entries.append(NavEntry(date=d, nav=float(p["nav"])))
        return HistoryResult(entries=in_range(entries, start, end), currency="INR", source=self.name) if entries else None

    def search(self, q: str) -> list[dict]:
        rows = self._latest.get().values()
        return [self._hit(r) for r in rows if _matches(q, f"{r.get('Scheme Name', '')} {r.get('PFM Name', '')}")][:30]
