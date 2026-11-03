import re
import time
from datetime import date

from .. import ids
from ..cache import normalize
from .base import Source, Inst, QuoteResult, HistoryResult, NavEntry, PLAN, in_range

SCHEME = "cl-afp"
URL = "https://www.spensiones.cl/apps/valoresCuotaFondo/vcfAFPxls.php"
FUNDS = "ABCDE"
TTL = 12 * 3600
ids.register(SCHEME, re.compile(r"^[A-Z][A-Z0-9 .]*-[A-E]$").match, upper=True)


def words_match(query: str, text: str) -> bool:
    """Every word of the query starts some word of the text (accent- and case-insensitive)."""
    words = normalize(text).split()
    return all(any(w.startswith(t) for w in words) for t in normalize(query).split())


def _num(s: str) -> float:
    return float(s.replace(".", "").replace(",", "."))


class ChileAFPSource(Source):
    """Chilean pension funds (AFP, funds A–E): official daily unit values from the Superintendencia de Pensiones."""

    name = "cl-afp"
    search_scope = "national"
    search_hints = frozenset({"afp", "chile", "chilean", "habitat", "provida", "cuprum", "planvital"})
    types = frozenset({PLAN})
    schemes = frozenset({SCHEME})
    timeout = 60

    def __init__(self, min_interval: float | None = None):
        super().__init__(min_interval)
        self._years: dict[tuple[str, int], tuple[float, dict[str, list[NavEntry]]]] = {}

    def _year(self, fund: str, year: int) -> dict[str, list[NavEntry]]:
        """AFP name -> daily values of one fund letter for one year (past years are final, kept for good)."""
        cached = self._years.get((fund, year))
        if cached and (year < date.today().year or time.time() - cached[0] < TTL):
            return cached[1]
        r = self._get(URL, params={"aaaaini": year, "aaaafin": year, "tf": fund,
                                   "fecconf": date.today().strftime("%Y%m%d")})
        r.raise_for_status()
        series = self._parse(r.content.decode("latin-1"))
        self._years[(fund, year)] = (time.time(), series)
        return series

    @staticmethod
    def _parse(text: str) -> dict[str, list[NavEntry]]:
        afps: list[str] = []
        series: dict[str, list[NavEntry]] = {}
        last: dict[str, tuple[str, str]] = {}
        for line in text.splitlines():
            cells = line.split(";")
            if cells[0] == "Fecha":
                afps = [c.strip().upper() for c in cells[1::2]]
                series.update({a: series.get(a, []) for a in afps})
                continue
            if not afps or not re.match(r"^\d{4}-\d{2}-\d{2}$", cells[0]):
                continue
            # Weekends and holidays are filled with the previous day's values: skip them.
            if date.fromisoformat(cells[0]).weekday() >= 5:
                continue
            for i, afp in enumerate(afps):
                cuota, patrimonio = [*cells[1 + 2 * i:3 + 2 * i], "", ""][:2]
                if not cuota.strip() or last.get(afp) == (cuota, patrimonio):
                    continue
                last[afp] = (cuota, patrimonio)
                series[afp].append(NavEntry(cells[0], _num(cuota)))
        return series

    @staticmethod
    def _split(code: str) -> tuple[str, str]:
        afp, fund = code.rsplit("-", 1)
        return afp, fund

    def get_history(self, inst: Inst, start: str, end: str) -> HistoryResult | None:
        afp, fund = self._split(inst.code)
        entries: list[NavEntry] = []
        for year in range(int(start[:4]), min(int(end[:4]), date.today().year) + 1):
            for e in self._year(fund, year).get(afp, []):
                # 1 January repeats 31 December; the per-year parse can't see across files.
                if not entries or e.nav != entries[-1].nav:
                    entries.append(e)
        entries = in_range(entries, start, end)
        return HistoryResult(entries=entries, currency="CLP", source=self.name) if entries else None

    def get_quote(self, inst: Inst) -> QuoteResult | None:
        afp, fund = self._split(inst.code)
        year = date.today().year
        values = self._year(fund, year).get(afp) or self._year(fund, year - 1).get(afp)
        if not values:
            return None
        return QuoteResult(nav=values[-1].nav, date=values[-1].date, currency="CLP", source=self.name)

    def _catalogue(self) -> list[dict]:
        year = date.today().year
        afps = list(self._year("A", year)) or list(self._year("A", year - 1))
        return [{
            "id": ids.canonical(SCHEME, f"{afp}-{f}"), "name": f"AFP {afp.title()} Fondo {f}", "type": PLAN,
            "currency": "CLP", "provider": f"AFP {afp.title()}", "category": f"Fondo {f}",
        } for afp in afps for f in FUNDS]

    def search(self, q: str) -> list[dict]:
        return [h for h in self._catalogue() if words_match(q, f"{h['name']} chile afp pensiones")]

    def resolve(self, scheme: str, code: str) -> dict | None:
        if scheme != SCHEME:
            return None
        return next((h for h in self._catalogue() if h["id"] == ids.canonical(SCHEME, code)), None)
