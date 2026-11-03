import json
import re

from .base import DGS, Source, Inst, QuoteResult, HistoryResult, NavEntry, PLAN, in_range

BASE = "https://comparadorfondos.com/plans"


def _first_array(html: str, key: str) -> list | None:
    # The page embeds data as an escaped React Server Components payload; the first array is the
    # requested plan, later ones belong to "similar plans".
    m = re.search(r'\\"' + key + r'\\":\[', html)
    if not m:
        return None
    i, depth = m.end() - 1, 0
    for j in range(i, len(html)):
        if html[j] == "[":
            depth += 1
        elif html[j] == "]":
            depth -= 1
            if depth == 0:
                break
    try:
        return json.loads(html[i:j + 1].replace('\\"', '"').replace("\\\\", "\\"))
    except ValueError:
        return None


class ComparadorFondosSource(Source):
    """Only used for pension plans: for funds it republishes Morningstar data."""

    name = "comparadorfondos"
    types = frozenset({PLAN})
    schemes = frozenset({DGS})
    timeout = 40

    def _history(self, inst: Inst) -> HistoryResult | None:
        r = self._get(f"{BASE}/{inst.id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        prices = _first_array(r.text, "prices")
        if not prices:
            return None
        cur = re.search(r'\\"currency\\":\\"([A-Z]{3})\\"', r.text)
        entries = [NavEntry(date=p["date"][:10], nav=float(p["performance"]))
                   for p in prices if p.get("date") and p.get("performance") is not None]
        return HistoryResult(entries=sorted(entries, key=lambda e: e.date),
                             currency=cur.group(1) if cur else "EUR", source=self.name)

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
