"""SMV (Peru) against hand-made pages shaped like the real ASP.NET form, its grid and its series pages."""
import httpx

from fundly.sources import peru
from fundly.sources.base import FUND, Inst
from fundly.sources.peru import SMVSource, parse_grid, parse_series
from test_sources import mock

HIDDEN = ('<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="vs&amp;1" />'
          '<input type="hidden" name="__EVENTVALIDATION" id="__EVENTVALIDATION" value="ev" />')
MANAGERS = ('<select name="ctl00$MainContent$cboDenominacionSocial" id="MainContent_cboDenominacionSocial">'
            '<option value="-1">Ingrese nombre de la empresa</option>'
            '<option value="38036">BBVA ASSET MANAGEMENT S.A. SAF</option>'
            '<option value="151229">FARO CAPITAL SOCIEDAD ADMINISTRADORA DE FONDOS S.A.</option></select>')
FUNDS = {
    "38036": [("0002", "Bbva Renta Mediano Plazo Soles  Fmiv")],
    "151229": [("0004", "Faro Capital Renta Variable Global Fmiv")],
}


def funds_select(manager):
    options = "".join(f'<option value="{v}">{name}</option>' for v, name in FUNDS.get(manager, []))
    return (f'<select name="ctl00$MainContent$cboFondo" id="MainContent_cboFondo"><option value="--SELECCIONE FONDO--">'
            f'--SELECCIONE FONDO--</option><option value="%">TODOS</option>{options}</select>')


def cell(text):
    return f'<td class="grid_contenido_numero_Responsive" width="11%">{text}</td>'


def grid(heading, rows):
    body = "".join("<tr>" + cell(day) + cell("17/10/1997") + cell("Operativa") + cell(value) + cell("1,000.00") + "</tr>"
                   for day, value in rows)
    return (f'<div id="MainContent_grdEVCP"><span id="MainContent_grdEVCP_lblCabDato3_0" class="izquierda"><b>{heading}'
            f'</b></span><table id="MainContent_grdEVCP_grdDetalle_0">{body}</table></div>')


def series_link(n):
    return f'<a href="Frm_EVCPs_det.aspx?data=ABC{n}">Ver series</a>'


def series_page(day, values):
    out = ""
    for n, (letter, value) in enumerate(values.items()):
        out += (f'<span id="grdEVCP_lblCabDato3_{n}" class="izquierda"><b>FARO  CAPITAL RENTA VARIABLE GLOBAL FMIV EN '
                f'DOLARES SERIE {letter}</b></span><table id="grdEVCP_grdDetalle_{n}"><tr>'
                f'<td class="grid_contenido_numero">{day}</td><td class="grid_contenido_numero">{value}</td>'
                f'<td class="grid_contenido_numero">54,671,030.38</td></tr></table>')
    return out


GRIDS = {
    "0002": grid("BBVA RENTA MEDIANO PLAZO SOLES FMIV", [
        ("30/10/2026", "412.420573"), ("02/11/2026", "412.511002"), ("03/11/2026", "412.615249"), ("xx", "1")]),
    "0004": grid("FARO CAPITAL RENTA VARIABLE GLOBAL FMIV EN DOLARES", [
        ("02/11/2026", series_link(1)), ("03/11/2026", series_link(2))]),
}
SERIES = {
    "ABC1": series_page("02/11/2026", {"A": "187.5", "B": "183.2"}),
    "ABC2": series_page("03/11/2026", {"A": "188.790681", "B": "184.449894", "C": "177.958132"}),
}


def smv(tmp_path=None):
    state = {"sessions": 0, "cookies_on_form": []}

    def handler(request):
        if request.url.path.endswith("Frm_EVCPs_det.aspx"):
            return httpx.Response(200, text=SERIES.get(request.url.params["data"], ""))
        if request.method == "GET":
            state["cookies_on_form"].append(request.headers.get("cookie"))
            state["sessions"] += 1
            return httpx.Response(200, text=HIDDEN + MANAGERS + funds_select(""),
                                  headers={"set-cookie": f"ASP.NET_SessionId=s{state['sessions']}; path=/"})
        form = dict(httpx.QueryParams(request.content.decode()))
        assert form["__VIEWSTATE"] == "vs&1"  # hidden fields go back unescaped
        manager, fund = form["ctl00$MainContent$cboDenominacionSocial"], form["ctl00$MainContent$cboFondo"]
        if fund == "--SELECCIONE FONDO--":
            return httpx.Response(200, text=HIDDEN + MANAGERS + funds_select(manager))
        return httpx.Response(200, text=HIDDEN + MANAGERS + funds_select(manager) + GRIDS.get(fund, ""))

    src = mock(SMVSource, [(lambda r: True, handler)])
    src.files_dir = tmp_path
    src.state = state
    return src


def test_parsers_on_their_own():
    head, rows = parse_grid(GRIDS["0002"])
    assert head == "BBVA RENTA MEDIANO PLAZO SOLES FMIV"
    assert rows[-1] == ("2026-11-03", 412.615249, None) and len(rows) == 3  # the bad date row is skipped
    assert parse_grid(GRIDS["0004"])[1][0] == ("2026-11-02", None, "Frm_EVCPs_det.aspx?data=ABC1")
    assert parse_series(SERIES["ABC2"])["C"] == ("2026-11-03", 177.958132)
    assert parse_grid("<html>maintenance</html>") == (None, [])


def test_single_series_fund_quote_and_history():
    src = smv()
    q = src.get_quote(Inst("pe-smv:38036-0002", FUND))
    assert (q.nav, q.date, q.currency, q.source) == (412.615249, "2026-11-03", "PEN", "smv")
    h = src.get_history(Inst("pe-smv:38036-0002", FUND), "2026-11-01", "2026-11-30")
    assert [(e.date, e.nav) for e in h.entries] == [("2026-11-02", 412.511002), ("2026-11-03", 412.615249)]
    assert src.get_quote(Inst("pe-smv:38036-0002-B", FUND)) is None  # a single-series fund has no series ids


def test_series_of_a_multi_series_fund():
    src = smv()
    assert src.get_quote(Inst("pe-smv:151229-0004", FUND)).nav == 188.790681  # the fund's own id is series A
    q = src.get_quote(Inst("pe-smv:151229-0004-C", FUND))
    assert (q.nav, q.currency) == (177.958132, "USD")
    h = src.get_history(Inst("pe-smv:151229-0004-B", FUND), "2026-10-01", "2026-11-30")
    assert [(e.date, e.nav) for e in h.entries] == [("2026-11-02", 183.2), ("2026-11-03", 184.449894)]
    assert src.get_quote(Inst("pe-smv:151229-0004-Z", FUND)) is None


def test_series_history_is_limited_to_the_latest_days(monkeypatch):
    monkeypatch.setattr(peru, "MAX_SERIES_DAYS", 1)
    h = smv().get_history(Inst("pe-smv:151229-0004-A", FUND), "2026-10-01", "2026-11-30")
    assert [e.date for e in h.entries] == ["2026-11-03"]


def test_every_query_starts_a_new_session():
    src = smv()
    for _ in range(2):
        src.get_quote(Inst("pe-smv:38036-0002", FUND))
    # the site answers a second search in the same session with an empty list, so no old cookie is sent
    assert src.state["cookies_on_form"] == [None, None]


def test_unknown_manager_or_fund():
    src = smv()
    assert src.get_quote(Inst("pe-smv:99999-0001", FUND)) is None
    assert src.get_history(Inst("pe-smv:38036-0009", FUND), "2026-01-01", "2026-11-30") is None


def test_catalogue_search_and_resolve_kept_on_disk(tmp_path):
    src = smv(tmp_path)
    assert [h["id"] for h in src.search("bbva renta")] == ["pe-smv:38036-0002"]
    hit = src.resolve("pe-smv", "151229-0004-B")
    assert hit["id"] == "pe-smv:151229-0004-B" and hit["name"].endswith("Serie B")
    assert hit["provider"].startswith("FARO CAPITAL")
    assert src.resolve("pe-smv", "38036-0007") is None and src.resolve("other", "1") is None
    again = smv(tmp_path)  # a new session reads the catalogue from disk
    assert again.search("faro") and again.state["sessions"] == 0
