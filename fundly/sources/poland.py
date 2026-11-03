import re

from ..ids import canonical, register
from .base import FUND, PLAN, HistoryResult, Inst, NavEntry, QuoteResult, Source, in_range

API = "https://www.analizy.pl/api/quotation/fio"
SCHEME = "pl-analizy"
PENSION_RE = re.compile(r"\b(PPK|OFE|PPE|IKE|IKZE)\b")

register(SCHEME, re.compile(r"^[A-Z0-9]{2,12}$").match, upper=True)


class AnalizySource(Source):
    """Polish funds (FIO/SFIO), PPK and OFE pension funds from analizy.pl, with full daily history.

    Its robots.txt disallows /api/; the maintainer chose to keep such sources and documents it in the README.
    The "fio" path segment works for every product type. There is no search endpoint: codes come from
    analizy.pl URLs (e.g. https://www.analizy.pl/fundusze-inwestycyjne-otwarte/PZU50/...).
    """

    name = "analizy"
    types = frozenset({FUND, PLAN})
    schemes = frozenset({SCHEME})

    def _quotation(self, code: str) -> dict | None:
        r = self._get(f"{API}/{code}")
        if r.status_code in (400, 404):
            return None
        r.raise_for_status()
        return r.json()

    def _history(self, inst: Inst) -> HistoryResult | None:
        d = self._quotation(inst.code)
        series = (d or {}).get("series") or []
        if not d or not series or not d.get("currency"):
            return None
        entries = [NavEntry(date=p["date"][:10], nav=float(p["value"]))
                   for p in series[0].get("price") or [] if p.get("date") and p.get("value") is not None]
        return HistoryResult(entries=sorted(entries, key=lambda e: e.date), currency=d["currency"], source=self.name)

    def get_quote(self, inst: Inst) -> QuoteResult | None:
        h = self._history(inst)
        if not h or not h.entries:
            return None
        last = h.entries[-1]
        return QuoteResult(nav=last.nav, date=last.date, currency=h.currency, source=self.name)

    def get_history(self, inst: Inst, start: str, end: str) -> HistoryResult | None:
        h = self._history(inst)
        if h:
            h.entries = in_range(h.entries, start, end)
        return h

    def resolve(self, scheme: str, code: str) -> dict | None:
        if scheme != SCHEME:
            return None
        d = self._quotation(code)
        if not d or not d.get("label"):
            return None
        return {"id": canonical(SCHEME, code), "name": d["label"], "currency": d.get("currency"),
                "type": PLAN if PENSION_RE.search(d["label"]) else FUND}
