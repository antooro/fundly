import html
import json
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta

from ..cache import normalize
from ..ids import canonical, register
from .base import Cached, FUND, HistoryResult, Inst, NavEntry, QuoteResult, Source, in_range

log = logging.getLogger("fundly")

SCHEME = "pe-smv"
# "<manager code>-<fund code>", plus "-<series>" for one series of a fund that has several.
register(SCHEME, re.compile(r"^\d{1,7}-\d{4}(-[A-Z0-9]{1,3})?$").match)

BASE = "https://www.smv.gob.pe/SIMV/"
# SMV's public form "Evolución del valor cuota": one fund's daily values over any range, back to launch.
FORM_URL = BASE + "Frm_EVCP?data=5A959494701B26421F184C081CACF55BFA328E8EBC"
F = "ctl00$MainContent$"
CATALOGUE_TTL = 24 * 3600
CATALOGUE_DISK_TTL = 7 * 86400
QUOTE_LOOKBACK_DAYS = 20
# A fund with several series only links one page per day with all its series, so a series' history costs
# one request per day; it is limited to the most recent days of the range asked for.
MAX_SERIES_DAYS = 62
CURRENCIES = (("dolar", "USD"), ("euro", "EUR"), ("sol", "PEN"))


def _clean(text: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())


def _hidden(page: str) -> dict[str, str]:
    return {k: html.unescape(v) for k, v in re.findall(r'<input type="hidden" name="([^"]+)" id="[^"]*" value="([^"]*)"', page)}


def _options(page: str, name: str) -> list[tuple[str, str]]:
    m = re.search(rf'<select name="{re.escape(F + name)}".*?</select>', page, re.S)
    if not m:
        return []
    return [(v, _clean(label)) for v, label in re.findall(r'<option[^>]*value="([^"]*)"[^>]*>([^<]*)</option>', m.group(0))]


def _number(text: str) -> float | None:
    try:
        value = float(text.replace(",", ""))
    except ValueError:
        return None
    return value if value > 0 else None


def _day(text: str) -> str | None:
    try:
        return datetime.strptime(text.strip(), "%d/%m/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _currency(text: str) -> str | None:
    t = normalize(text)
    return next((ccy for word, ccy in CURRENCIES if word in t), None)


def parse_grid(page: str) -> tuple[str | None, list[tuple[str, float | None, str | None]]]:
    """Fund heading (name + currency, e.g. "... FMIV EN DOLARES") and its rows: (date, value, series page).
    Funds with several series show a link to that day's series page instead of a value."""
    grid = page[page.find("grdEVCP"):]
    head = re.search(r'lblCabDato3_\d+"[^>]*>(.*?)</span>', grid, re.S)
    rows = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", grid, re.S):
        cells = re.findall(r'<td class="grid_contenido_numero_Responsive"[^>]*>(.*?)</td>', row, re.S)
        if len(cells) < 4 or not (day := _day(_clean(cells[0]))):
            continue
        link = re.search(r'href="(Frm_EVCPs_det\.aspx\?data=[0-9A-F]+)"', cells[3])
        rows.append((day, None if link else _number(_clean(cells[3])), html.unescape(link.group(1)) if link else None))
    return (_clean(head.group(1)) if head else None), rows


def parse_series(page: str) -> dict[str, tuple[str, float]]:
    """A fund's series page for one day -> {series letter: (date, value)}. Each series is a heading
    ("... SERIE A") followed by its own table (grdDetalle_N) of date, value, assets..."""
    out = {}
    for n, heading in re.findall(r'lblCabDato3_(\d+)"[^>]*>(.*?)</span>', page, re.S):
        m = re.search(r"\bSERIE\s+([A-Z0-9]{1,3})$", _clean(heading))
        table = re.search(rf'id="grdEVCP_grdDetalle_{n}"(.*?)</table>', page, re.S)
        if not m or not table:
            continue
        cells = [_clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", table.group(1), re.S)]
        if len(cells) >= 2 and (day := _day(cells[0])) and (value := _number(cells[1])):
            out[m.group(1)] = (day, value)
    return out


class SMVSource(Source):
    """Peruvian mutual funds (fondos mutuos) from the SMV, the securities regulator: daily values per fund back to
    launch (1990s for the oldest). Funds with several series (A, B, I...) are asked for one series at a time,
    e.g. "pe-smv:151229-0004-A"; the fund's own id answers with its series A. A series' history costs one request
    per day and covers at most the last MAX_SERIES_DAYS days of the range."""

    name = "smv"
    search_scope = "national"
    search_hints = frozenset({"peru", "peruano", "peruana", "soles", "pen", "fondos mutuos", "fmiv", "credicorp",
                              "interfondos", "scotia", "sura", "bbva", "blum", "faro", "prudential", "smv"})
    types = frozenset({FUND})
    schemes = frozenset({SCHEME})
    timeout = 120

    def __init__(self, min_interval: float | None = None):
        super().__init__(min_interval)
        self._catalogue = Cached(self._load_catalogue, CATALOGUE_TTL)
        self._session = threading.RLock()  # one form conversation at a time (they share the session cookie)

    # --- the form -----------------------------------------------------------------------------------

    def _form(self) -> tuple[str, dict[str, str]]:
        # Each query starts a new ASP.NET session: after one search, the same session answers the next with an
        # empty list.
        self.http.cookies.clear()
        r = self._get(FORM_URL)
        r.raise_for_status()
        return r.text, dict(_options(r.text, "cboDenominacionSocial"))

    def _submit(self, page: str, fields: dict) -> str:
        data = {**_hidden(page), "ctl00$rbNuevo": "rbEmpresa", "ctl00$txtSearch": "", **fields}
        r = self._post(FORM_URL, data=data)
        r.raise_for_status()
        return r.text

    def _manager_page(self, page: str, managers: dict[str, str], manager: str, start: str, end: str) -> str:
        """The form with this manager chosen, so its funds are listed. The site sometimes answers with an empty
        list; it is asked once more then."""
        fields = {F + "cboDenominacionSocial": manager, F + "TextBox1": managers.get(manager, ""),
                  F + "txtFechDesde": start, F + "txtFechHasta": end,
                  F + "cboFondo": "--SELECCIONE FONDO--", F + "btnBuscar": "Buscar"}
        for _ in range(2):
            listed = self._submit(page, fields)
            if _options(listed, "cboFondo"):
                return listed
        return listed

    def _grid(self, manager: str, fund: str, start: str, end: str) -> tuple[str | None, list]:
        """GET the form, choose the manager, then the fund and the range: three requests for any range."""
        d1, d2 = (datetime.strptime(d, "%Y-%m-%d").strftime("%d/%m/%Y") for d in (start, end))
        with self._session:
            page, managers = self._form()
            if manager not in managers:
                return None, []
            listed = self._manager_page(page, managers, manager, d1, d2)
            if fund not in dict(_options(listed, "cboFondo")):
                return None, []
            result = self._submit(listed, {F + "cboDenominacionSocial": manager, F + "TextBox1": managers[manager],
                                         F + "txtFechDesde": d1, F + "txtFechHasta": d2,
                                         F + "cboFondo": fund, F + "btnBuscar": "Buscar"})
        return parse_grid(result)

    def _series_page(self, link: str) -> dict[str, tuple[str, float]]:
        r = self._get(BASE + link)
        r.raise_for_status()
        return parse_series(r.text)

    # --- catalogue ----------------------------------------------------------------------------------

    def _load_catalogue(self) -> dict[str, dict]:
        """{"manager-fund": {name, manager}} for every fund the form lists: one request per manager (~20).
        Kept on disk for a week when there is a cache folder."""
        path = self.files_dir / "catalogue.json" if self.files_dir else None
        if path is not None and path.exists() and time.time() - path.stat().st_mtime < CATALOGUE_DISK_TTL:
            try:
                kept = json.loads(path.read_text())
                if isinstance(kept, dict) and kept:
                    return kept
            except ValueError:
                pass
        today = date.today().strftime("%d/%m/%Y")
        catalogue = {}
        with self._session:
            page, managers = self._form()
            for manager, manager_name in managers.items():
                if not manager.isdigit():
                    continue
                listed = self._manager_page(page, managers, manager, today, today)
                for fund, fund_name in _options(listed, "cboFondo"):
                    if re.fullmatch(r"\d{4}", fund):
                        catalogue[f"{manager}-{fund}"] = {"name": fund_name, "manager": manager_name}
        if path is not None and catalogue:
            self._write_atomic(path, json.dumps(catalogue, ensure_ascii=False).encode())
        return catalogue

    def _hit(self, code: str, f: dict) -> dict:
        return {"id": canonical(SCHEME, code), "name": f["name"], "type": FUND, "currency": _currency(f["name"]),
                "provider": f["manager"]}

    # --- Source -------------------------------------------------------------------------------------

    @staticmethod
    def _split(code: str) -> tuple[str, str, str | None]:
        manager, fund, *series = code.split("-")
        return manager, fund, series[0] if series else None

    def get_quote(self, inst: Inst) -> QuoteResult | None:
        manager, fund, series = self._split(inst.code)
        end = date.today()
        head, rows = self._grid(manager, fund, (end - timedelta(days=QUOTE_LOOKBACK_DAYS)).isoformat(), end.isoformat())
        currency = _currency(head or "")
        if not rows or not currency:
            return None
        day, value, link = rows[-1]
        if link:
            got = self._series_page(link).get(series or "A")
            if not got:
                return None
            day, value = got
        elif series:
            return None  # a single-series fund has no series ids
        if value is None:
            return None
        return QuoteResult(nav=value, date=day, currency=currency, source=self.name)

    def get_history(self, inst: Inst, start: str, end: str) -> HistoryResult | None:
        manager, fund, series = self._split(inst.code)
        head, rows = self._grid(manager, fund, start, min(end, date.today().isoformat()))
        currency = _currency(head or "")
        if not rows or not currency:
            return None
        entries = [NavEntry(day, value) for day, value, link in rows if value is not None and not link and not series]
        linked = [(day, link) for day, _, link in rows if link]
        if linked:
            if len(linked) > MAX_SERIES_DAYS:
                log.info("smv: %s has several series; history limited to its last %d days", inst.id, MAX_SERIES_DAYS)
            for _, link in linked[-MAX_SERIES_DAYS:]:
                got = self._series_page(link).get(series or "A")
                if got:
                    entries.append(NavEntry(*got))
        entries = in_range(entries, start, end)
        return HistoryResult(entries=entries, currency=currency, source=self.name) if entries else None

    def search(self, q: str) -> list[dict]:
        tokens = normalize(q).split()
        if not tokens:
            return []
        hits = [self._hit(code, f) for code, f in self._catalogue.get().items()
                if all(t in normalize(f"{f['name']} {f['manager']}") for t in tokens)]
        return hits[:30]

    def resolve(self, scheme: str, code: str) -> dict | None:
        if scheme != SCHEME:
            return None
        manager, fund, series = self._split(code)
        f = self._catalogue.get().get(f"{manager}-{fund}")
        if not f:
            return None
        hit = self._hit(f"{manager}-{fund}", f)
        if series:
            hit.update(id=canonical(SCHEME, code), name=f"{f['name']} Serie {series}")
        return hit
