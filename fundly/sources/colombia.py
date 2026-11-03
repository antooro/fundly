import re
import time
from datetime import date, timedelta

from .. import ids
from ..cache import normalize
from .base import Source, Inst, QuoteResult, HistoryResult, NavEntry, FUND, PLAN

PENSION, FIC = "co-pension", "co-fic"
API = "https://www.datos.gov.co/resource"
# Superfinanciera open data (CC BY-SA 4.0): obligatory pensions + cesantías, and collective investment funds.
DATASETS = {
    PENSION: {"id": "uawh-cjvi", "date": "fecha", "value": "valor_unidad",
              "keys": ("codigo_entidad", "codigo_patrimonio")},
    FIC: {"id": "qhpu-8ixx", "date": "fecha_corte", "value": "valor_unidad_operaciones",
          "keys": ("codigo_entidad", "codigo_negocio", "tipo_participacion")},
}
TTL = 12 * 3600
ids.register(PENSION, re.compile(r"^\d+-\d+$").match)
ids.register(FIC, re.compile(r"^\d+-\d+-\d+$").match)


def words_match(query: str, text: str) -> bool:
    """Every word of the query starts some word of the text (accent- and case-insensitive)."""
    words = normalize(text).split()
    return all(any(w.startswith(t) for w in words) for t in normalize(query).split())


def _clean(name: str | None) -> str:
    return " ".join((name or "").replace('"', "").split())


class ColombiaSource(Source):
    """Colombian pension funds, cesantías and collective investment funds (FIC), from datos.gov.co."""

    name = "datos-gov-co"
    search_scope = "national"
    search_hints = frozenset({"colombia", "colombian", "cesantias", "porvenir", "proteccion", "colfondos", "skandia", "fic",
                               "fiduciaria", "fiducia", "bancolombia", "davivienda", "colpatria", "alianza"})
    types = frozenset({FUND, PLAN})
    schemes = frozenset({PENSION, FIC})
    timeout = 60

    def __init__(self, min_interval: float | None = None):
        super().__init__(min_interval)
        self._catalogue_cache: tuple[float, list[dict]] | None = None

    def _query(self, scheme: str, **params) -> list[dict]:
        r = self._get(f"{API}/{DATASETS[scheme]['id']}.json", params={"$limit": 50000, **params})
        r.raise_for_status()
        return r.json()

    def _filters(self, inst: Inst) -> dict:
        return dict(zip(DATASETS[inst.scheme]["keys"], inst.code.split("-"), strict=True))

    def _series(self, inst: Inst, rows: list[dict]) -> list[NavEntry]:
        ds = DATASETS[inst.scheme]
        by_date = {r[ds["date"]][:10]: float(r[ds["value"]]) for r in rows if r.get(ds["value"]) and r.get(ds["date"])}
        return [NavEntry(d, v) for d, v in sorted(by_date.items())]

    def get_quote(self, inst: Inst) -> QuoteResult | None:
        ds = DATASETS[inst.scheme]
        rows = self._query(inst.scheme, **self._filters(inst), **{"$order": f"{ds['date']} DESC", "$limit": 1})
        entries = self._series(inst, rows)
        if not entries:
            return None
        return QuoteResult(nav=entries[-1].nav, date=entries[-1].date, currency="COP", source=self.name)

    def get_history(self, inst: Inst, start: str, end: str) -> HistoryResult | None:
        ds = DATASETS[inst.scheme]
        rows = self._query(inst.scheme, **self._filters(inst), **{
            "$where": f"{ds['date']} between '{start}T00:00:00' and '{end}T23:59:59'", "$order": ds["date"]})
        entries = self._series(inst, rows)
        return HistoryResult(entries=entries, currency="COP", source=self.name) if entries else None

    def _catalogue(self) -> list[dict]:
        if self._catalogue_cache and time.time() - self._catalogue_cache[0] < TTL:
            return self._catalogue_cache[1]
        since = (date.today() - timedelta(days=30)).isoformat()
        hits = []
        cols = "codigo_entidad,nombre_entidad,codigo_patrimonio,nombre_fondo"
        for r in self._query(PENSION, **{"$select": cols, "$group": cols, "$where": f"fecha > '{since}'"}):
            provider = _clean(r.get("nombre_entidad"))
            hits.append({
                "id": ids.canonical(PENSION, f"{r['codigo_entidad']}-{r['codigo_patrimonio']}"),
                "name": f"{provider} {_clean(r.get('nombre_fondo'))}", "type": PLAN, "currency": "COP",
                "provider": provider, "category": _clean(r.get("nombre_fondo")),
            })
        cols = "codigo_entidad,nombre_entidad,codigo_negocio,nombre_patrimonio,tipo_participacion,nombre_subtipo_patrimonio"
        for r in self._query(FIC, **{"$select": cols, "$group": cols, "$where": f"fecha_corte > '{since}'"}):
            hits.append({
                "id": ids.canonical(FIC, f"{r['codigo_entidad']}-{r['codigo_negocio']}-{r['tipo_participacion']}"),
                "name": f"{_clean(r.get('nombre_patrimonio'))} (participación {r['tipo_participacion']})",
                "type": FUND, "currency": "COP", "provider": _clean(r.get("nombre_entidad")),
                "category": _clean(r.get("nombre_subtipo_patrimonio")),
            })
        self._catalogue_cache = (time.time(), hits)
        return hits

    def search(self, q: str) -> list[dict]:
        return [h for h in self._catalogue() if words_match(q, f"{h['name']} {h['provider']} colombia")]

    def resolve(self, scheme: str, code: str) -> dict | None:
        if scheme not in self.schemes:
            return None
        return next((h for h in self._catalogue() if h["id"] == ids.canonical(scheme, code)), None)
