import io
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime

from ..cache import normalize
from ..ids import canonical, register
from .base import Cached, FUND, HistoryResult, Inst, NavEntry, QuoteResult, Source, in_range

PLANILLA_URL = "https://api.pub.cafci.org.ar/pb_get"
SCHEME = "ar-cafci"
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
TTL = 6 * 3600

# Daily history per class from argentinadatos.com, a community mirror of the CNV daily sheets (MIT code):
# one file per class, from January 2020, every category. Files are named after the class ("slug").
HISTORY_URL = "https://api.argentinadatos.com/v1/finanzas/fci/fondos/{slug}/historico"
# Every class with its CAFCI code, for classes renamed since the mirror first saw them (~30 MB, rarely needed).
CLASSES_URL = "https://api.argentinadatos.com/v1/finanzas/fci/fondos"
CLASSES_TTL = 24 * 3600

register(SCHEME, re.compile(r"^\d{1,6}$").match)


def slug(name: str) -> str:
    """The mirror's file name for a class, as its slugificarNombreFondo() builds it."""
    text = "".join(c for c in unicodedata.normalize("NFD", name) if not unicodedata.combining(c)).lower()
    text = re.sub(r"[^a-z0-9-]", "", re.sub(r"\s+", "-", text))
    return re.sub(r"-+", "-", text).strip("-")


def _day(text: str | None) -> str | None:
    try:
        return datetime.strptime(text or "", "%d/%m/%y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _past_points(cells: dict, dates: dict[str, str], nav: float) -> dict[str, float]:
    """The sheet's older values, rebuilt from the % change since the last month-end (J), the last year-end (K)
    and the month-end a year earlier (L). Column dates come from the second header row."""
    points = {}
    for col in ("J", "K", "L"):
        try:
            change = float(cells.get(col) or "")
        except ValueError:
            continue
        if dates.get(col) and change > -100:
            points[dates[col]] = round(nav / (1 + change / 100), 6)
    return points


def _float(text: str | None) -> float | None:
    try:
        return float(text or "")
    except ValueError:
        return None


def _parse_planilla(content: bytes) -> dict[str, dict]:
    """CAFCI "Planilla Diaria" XLSX -> {class code: row}. Strings are inline, so no sharedStrings."""
    sheet = zipfile.ZipFile(io.BytesIO(content)).read("xl/worksheets/sheet1.xml")
    funds: dict[str, dict] = {}
    category: str | None = None
    dates: dict[str, str] = {}
    for row in ET.fromstring(sheet).iter(f"{NS}row"):
        cells: dict[str, str | None] = {}
        for c in row.iter(f"{NS}c"):
            col = re.sub(r"\d", "", c.get("r", ""))
            if c.get("t") == "inlineStr":
                cells[col] = "".join(t.text or "" for t in c.iter(f"{NS}t"))
            else:
                v = c.find(f"{NS}v")
                cells[col] = v.text if v is not None else None
        name, code, value = cells.get("A"), cells.get("U"), cells.get("F")
        if not dates and cells.get("F") == "Actual":  # second header row: the dates of columns G, J, K, L
            dates = {col: d for col in ("G", "J", "K", "L") if (d := _day(cells.get(col)))}
            continue
        if name and not any(cells.get(k) for k in ("B", "E", "F", "U")):
            category = name  # section header, e.g. "Renta Variable Peso Argentina"
            continue
        if not (name and code and code.isdigit() and value):
            continue
        nav, day = _float(value), _day(cells.get("E"))
        if not nav or nav <= 0 or not day:
            continue
        nav = round(nav / 1000, 9)  # the planilla quotes the value of 1,000 cuotapartes
        exact = {day: nav}
        previous = _float(cells.get("G"))
        if dates.get("G") and previous and previous > 0:
            exact[dates["G"]] = round(previous / 1000, 9)
        funds[code] = {"name": name, "nav": nav, "date": day, "currency": cells.get("B") or "ARS",
                       "provider": cells.get("X"), "category": category,
                       "exact": exact, "derived": _past_points(cells, dates, nav)}
    return funds


class CAFCISource(Source):
    """Argentine mutual funds (FCI) from CAFCI's public daily sheet.

    History comes from argentinadatos.com, one file per class from 2020 (kept on disk, asked again at most hourly).
    On top go the sheets this machine has downloaded and the sheet's own points (today, the previous day, and
    the month-ends rebuilt from its % changes), which also cover classes the mirror lacks."""

    name = "cafci"
    search_scope = "national"
    search_hints = frozenset({"argentina", "argentino", "fci", "cafci", "superfondo", "fima", "balanz", "pellegrini",
                               "consultatio", "cuotaparte", "pesos", "ars"})
    types = frozenset({FUND})
    schemes = frozenset({SCHEME})
    timeout = 60

    def __init__(self, min_interval: float | None = None):
        super().__init__(min_interval)
        self._planilla = Cached(self._load_planilla, TTL)
        self._classes = Cached(self._load_classes, CLASSES_TTL)

    def _load_planilla(self) -> dict[str, dict]:
        r = self._get(PLANILLA_URL)
        r.raise_for_status()
        funds = _parse_planilla(r.content)
        self._keep_sheet(funds)
        return funds

    def _keep_sheet(self, funds: dict[str, dict]):
        """Every sheet downloaded is kept on disk by date ({class code: value}), so each machine builds its own
        gap-free history of every class from the day it first used fundly, whatever the mirror lacks."""
        if self.files_dir is None:
            return
        by_day: dict[str, dict[str, float]] = {}
        for code, f in funds.items():
            for day, value in f["exact"].items():
                by_day.setdefault(day, {})[code] = value
        for day, values in by_day.items():
            kept = self._kept_sheet(day)
            if not values.keys() <= kept.keys():
                self._write_atomic(self.files_dir / f"sheet-{day}.json", json.dumps({**kept, **values}).encode())

    def _kept_sheet(self, day: str) -> dict[str, float]:
        path = self.files_dir / f"sheet-{day}.json" if self.files_dir else None
        try:
            kept = json.loads(path.read_text()) if path is not None and path.exists() else {}
        except ValueError:
            kept = {}
        return kept if isinstance(kept, dict) else {}

    def _funds(self) -> dict[str, dict]:
        return self._planilla.get()

    def _hit(self, code: str, f: dict) -> dict:
        return {"id": canonical(SCHEME, code), "name": f["name"], "type": FUND, "currency": f["currency"],
                "provider": f["provider"], "category": f["category"]}

    def get_quote(self, inst: Inst) -> QuoteResult | None:
        f = self._funds().get(inst.code)
        if not f:
            return None
        return QuoteResult(nav=f["nav"], date=f["date"], currency=f["currency"], source=self.name)

    def get_history(self, inst: Inst, start: str, end: str) -> HistoryResult | None:
        f = self._funds().get(inst.code)
        if not f:
            return None
        # Rebuilt month-end points < the mirror's daily values < sheets kept on disk < today's sheet.
        found: dict[str, float] = dict(f["derived"])
        found.update(self._mirror(inst.code, f["name"]))
        if self.files_dir is not None and self.files_dir.exists():
            for path in self.files_dir.glob("sheet-*.json"):
                kept_day = path.stem[len("sheet-"):]
                if start <= kept_day <= end and isinstance(value := self._kept_sheet(kept_day).get(inst.code), int | float):
                    found[kept_day] = value
        found.update(f["exact"])
        entries = in_range([NavEntry(d, v) for d, v in found.items()], start, end)
        return HistoryResult(entries=entries, currency=f["currency"], source=self.name) if entries else None

    def _mirror(self, code: str, name: str) -> dict[str, float]:
        """{date: value} from the mirror's file for this class. The file is found by the class's current name;
        if that misses (renamed class) or belongs to another class, by the slug the mirror's list gives the code."""
        values = self._mirror_file(code, slug(name))
        if values is None:
            other = self._classes.get().get(code)
            if other and other != slug(name):
                values = self._mirror_file(code, other)
        return values or {}

    def _mirror_file(self, code: str, name: str) -> dict[str, float] | None:
        if not name:
            return None
        content = self._fetch_file(HISTORY_URL.format(slug=name), f"history-{name}.json")
        try:
            data = json.loads(content) if content else None
        except ValueError:
            return None
        if not isinstance(data, dict) or str(data.get("claseId")) != code:
            return None
        values = {}
        for x in data.get("historico") or []:
            if not isinstance(x, dict):
                continue
            day, value = str(x.get("fecha") or "")[:10], x.get("valorCuotaparte")
            if len(day) == 10 and isinstance(value, int | float) and value > 0:
                values[day] = round(value / 1000, 9)  # per 1,000 cuotapartes, like the sheet
        return values

    def _load_classes(self) -> dict[str, str]:
        """{CAFCI class code: the mirror's slug}."""
        r = self._get(CLASSES_URL)
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        data = r.json()
        rows = data.get("fondos") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return {}
        return {str(x["claseId"]): slug(x["nombre"]) for x in rows
                if isinstance(x, dict) and x.get("claseId") is not None and isinstance(x.get("nombre"), str)}

    def resolve(self, scheme: str, code: str) -> dict | None:
        if scheme != SCHEME:
            return None
        f = self._funds().get(code)
        return self._hit(code, f) if f else None

    def search(self, q: str) -> list[dict]:
        tokens = normalize(q).split()
        if not tokens:
            return []
        hits = [self._hit(code, f) for code, f in self._funds().items()
                if all(t in normalize(f["name"]) for t in tokens)]
        return hits[:30]
