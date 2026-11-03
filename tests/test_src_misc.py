"""CAFCI (AR), FT symbols, analizy.pl (PL) and Gemel-net/Pensia-net (IL) against hand-made responses."""
import io
import json
import zipfile

import httpx
import pytest

from fundly.ids import parse
from fundly.sources.argentina import CAFCISource
from fundly.sources.base import FUND, PLAN, Inst
from fundly.sources.ft import FTSource
from fundly.sources.israel import GemelNetSource
from fundly.sources.poland import AnalizySource
from test_sources import mock, path


# --- ids ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("ar-cafci:148", ("ar-cafci", "148")),
    ("ft:0p00009vb8", ("ft", "0P00009VB8")),
    ("ft:GB00B09CCX34:GBP", ("ft", "GB00B09CCX34:GBP")),
    ("pl-analizy:pzu50", ("pl-analizy", "PZU50")),
    ("il-gemel:880", ("il-gemel", "880")),
    ("il-pensia:2009", ("il-pensia", "2009")),
])
def test_new_schemes_parse(raw, expected):
    assert parse(raw) == expected


# --- CAFCI ----------------------------------------------------------------------------------------

def _cell(ref, value):
    if isinstance(value, str):
        return f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>'
    return f'<c r="{ref}"><v>{value}</v></c>'


def planilla(rows: list[dict]) -> bytes:
    body = "".join(f'<row r="{i}">' + "".join(_cell(f"{col}{i}", v) for col, v in r.items()) + "</row>"
                   for i, r in enumerate(rows, start=1))
    sheet = ('<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/'
             f'spreadsheetml/2006/main"><sheetData>{body}</sheetData></worksheet>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


PLANILLA = planilla([
    {"A": "CAMARA ARGENTINA DE FONDOS COMUNES DE INVERSION"},
    {"A": "Fondo", "B": "Clasificación", "E": "Fecha", "F": "Valor (mil cuotapartes)", "U": "Código CAFCI"},
    {"B": "Moneda", "F": "Actual"},
    {"A": "Renta Variable Peso Argentina"},
    {"A": "Ejemplo Acciones - Clase A", "B": "ARS", "E": "26/10/26", "F": 486533.021, "U": 9001,
     "X": "Gestora Ejemplo S.A."},
    {"A": "Ejemplo Acciones - Clase B", "B": "ARS", "E": "26/10/26", "F": 1000.0, "U": 9002, "X": "Gestora Ejemplo S.A."},
    {"A": "Renta Fija Dólares"},
    {"A": "Ejemplo Dólar Ahorro", "B": "USD", "E": "26/10/26", "F": 1234.5, "U": 9003, "X": "Otra Gestora"},
    {"A": "Fila rota", "B": "ARS", "E": "no es fecha", "F": 1.0, "U": 9004},
])


def cafci():
    return mock(CAFCISource, [(path("pb_get"), httpx.Response(200, content=PLANILLA))])


def test_cafci_quote_is_per_cuotaparte_not_per_thousand():
    q = cafci().get_quote(Inst("ar-cafci:9001"))
    assert (q.nav, q.date, q.currency) == (486.533021, "2026-10-26", "ARS")


def test_cafci_reads_currency_category_and_provider():
    src = cafci()
    hit = src.resolve("ar-cafci", "9003")
    assert hit == {"id": "ar-cafci:9003", "name": "Ejemplo Dólar Ahorro", "type": "fund", "currency": "USD",
                   "provider": "Otra Gestora", "category": "Renta Fija Dólares"}
    assert src.get_quote(Inst("ar-cafci:9004")) is None and src.get_quote(Inst("ar-cafci:1")) is None
    assert src.get_history(Inst("ar-cafci:9001"), "2020-01-01", "2020-12-31") is None


def test_cafci_downloads_the_sheet_once_and_searches_it():
    src = cafci()
    assert [h["id"] for h in src.search("ejemplo acciones")] == ["ar-cafci:9001", "ar-cafci:9002"]
    assert [h["id"] for h in src.search("dolar")] == ["ar-cafci:9003"]
    src.get_quote(Inst("ar-cafci:9001"))
    assert len(src.calls) == 1


# CAFCI history: the sheet's own points, sheets kept on disk, and argentinadatos' file for each class.
SHEET = planilla([
    {"A": "Fondo", "F": "Valor (mil cuotapartes)", "U": "Código CAFCI"},
    {"B": "Moneda", "F": "Actual", "G": "23/10/26", "H": "Variac. %", "J": "30/09/26", "K": "30/12/25", "L": "30/09/25"},
    {"A": "Renta Variable Peso Argentina"},
    {"A": "Ejemplo Acciones - Clase A", "B": "ARS", "E": "26/10/26", "F": 500000.0, "G": 490000.0, "J": 25.0,
     "K": 100.0, "L": -100.0, "U": 9001},
    {"A": "PyMes Peso Argentina"},
    {"A": "Ejemplo PyMEs - Clase A", "B": "ARS", "E": "26/10/26", "F": 2000.0, "G": 1990.0, "J": 0.0, "U": 9010},
])


def historico(code, points):
    return httpx.Response(200, json={"fondoId": "1", "claseId": code, "nombre": "x", "historico": [
        {"fecha": d, "valorCuotaparte": v, "origen": "cnv-excel"} for d, v in points]})


MIRROR = {
    "ejemplo-acciones-clase-a": historico("9001", [
        ("2025-03-03", 300000.0), ("2026-10-21", 480000.0), ("2026-10-22", 485000.0),
        ("2026-10-23", 489999.0),  # the sheet's exact value wins
        ("2026-10-24", None), ("", 1.0), ("2026-10-25", -3)]),
    # the PyMEs class was renamed: its current name's file belongs to another class
    "ejemplo-pymes-clase-a": historico("7777", [("2026-10-20", 9.0)]),
    "ejemplo-pymes-antes-clase-a": historico("9010", [("2026-10-20", 1980.0)]),
}
CLASSES = {"fondos": [{"claseId": "9010", "nombre": "Ejemplo PyMEs (antes) - Clase A"}, {"claseId": None}, "junk"]}


def cafci_history(tmp_path=None):
    def mirror(request):
        name = request.url.path.split("/fondos/")[-1].removesuffix("/historico")
        return MIRROR.get(name, httpx.Response(404, json={"error": "Not found"}))

    src = mock(CAFCISource, [(path("pb_get"), httpx.Response(200, content=SHEET)),
                             (lambda r: r.url.path.endswith("/historico"), mirror),
                             (lambda r: r.url.path.endswith("/fci/fondos"), httpx.Response(200, json=CLASSES))])
    src.files_dir = tmp_path
    return src


def mirror_calls(src):
    return [r.url.path.split("/fci/")[-1] for r in src.calls if "/fci/" in r.url.path]


def test_cafci_slug_matches_the_mirrors():
    from fundly.sources.argentina import slug
    assert slug("Superfondo Acciones - Clase A") == "superfondo-acciones-clase-a"
    assert slug("Adcap Acciones - Clase Ley Nº 27.743") == "adcap-acciones-clase-ley-n-27743"
    assert slug("  Fondo Ñandú   Ahorro (USD) ") == "fondo-nandu-ahorro-usd"


def test_cafci_history_merges_the_mirror_with_the_sheets_exact_values():
    src = cafci_history()
    h = src.get_history(Inst("ar-cafci:9001"), "2026-10-19", "2026-10-26")
    assert [(e.date, e.nav) for e in h.entries] == [
        ("2026-10-21", 480.0), ("2026-10-22", 485.0), ("2026-10-23", 490.0), ("2026-10-26", 500.0)]
    assert (h.currency, h.source) == ("ARS", "cafci")
    assert mirror_calls(src) == ["fondos/ejemplo-acciones-clase-a/historico"]  # one request, whole history


def test_cafci_history_rebuilds_month_end_points_from_the_sheets_changes():
    src = cafci_history()
    h = src.get_history(Inst("ar-cafci:9001"), "2025-01-01", "2025-12-31")
    assert [(e.date, e.nav) for e in h.entries] == [("2025-03-03", 300.0), ("2025-12-30", 250.0)]  # 500 / (1 + 100%)
    # -100% (30/09/25) would divide by zero: skipped


def test_cafci_renamed_class_is_found_through_the_mirrors_list():
    src = cafci_history()
    h = src.get_history(Inst("ar-cafci:9010"), "2026-10-01", "2026-10-31")
    assert [(e.date, e.nav) for e in h.entries] == [("2026-10-20", 1.98), ("2026-10-23", 1.99), ("2026-10-26", 2.0)]
    assert mirror_calls(src) == ["fondos/ejemplo-pymes-clase-a/historico", "fondos",
                                 "fondos/ejemplo-pymes-antes-clase-a/historico"]


def test_cafci_class_missing_from_the_mirror_keeps_the_sheets_points():
    sheet = planilla([{"B": "Moneda", "F": "Actual", "G": "23/10/26", "J": "30/09/26"}, {"A": "ASG Peso Argentina"},
                      {"A": "Nuevo Fondo - Clase A", "B": "ARS", "E": "26/10/26", "F": 1000.0, "G": 990.0, "J": 0.0,
                       "U": 9020}])
    src = mock(CAFCISource, [(path("pb_get"), httpx.Response(200, content=sheet))])  # mirror answers 404
    h = src.get_history(Inst("ar-cafci:9020"), "2025-01-01", "2026-12-31")
    assert [(e.date, e.nav) for e in h.entries] == [("2026-09-30", 1.0), ("2026-10-23", 0.99), ("2026-10-26", 1.0)]


def test_cafci_mirror_files_are_kept_on_disk(tmp_path):
    first = cafci_history(tmp_path).get_history(Inst("ar-cafci:9001"), "2025-01-01", "2026-12-31")
    again = cafci_history(tmp_path)  # a new session
    assert again.get_history(Inst("ar-cafci:9001"), "2025-01-01", "2026-12-31") == first
    assert mirror_calls(again) == []


def test_cafci_every_sheet_downloaded_is_kept_and_fills_the_mirrors_gaps(tmp_path):
    src = cafci_history(tmp_path)
    src.get_quote(Inst("ar-cafci:9010"))
    assert sorted(p.name for p in tmp_path.glob("sheet-*")) == ["sheet-2026-10-23.json", "sheet-2026-10-26.json"]
    # a later sheet no longer carries those days; the kept files still answer for every class
    later = planilla([
        {"B": "Moneda", "F": "Actual", "G": "26/10/26"},
        {"A": "PyMes Peso Argentina"},
        {"A": "Ejemplo PyMEs - Clase A", "B": "ARS", "E": "27/10/26", "F": 2010.0, "G": 2000.0, "U": 9010},
    ])
    nxt = mock(CAFCISource, [(path("pb_get"), httpx.Response(200, content=later))])
    nxt.files_dir = tmp_path
    h = nxt.get_history(Inst("ar-cafci:9010"), "2026-10-21", "2026-10-31")
    assert [(e.date, e.nav) for e in h.entries] == [("2026-10-23", 1.99), ("2026-10-26", 2.0), ("2026-10-27", 2.01)]


def test_cafci_mirror_errors_are_not_mistaken_for_a_missing_class():
    src = mock(CAFCISource, [(path("pb_get"), httpx.Response(200, content=SHEET)),
                             (path("/finanzas/fci/"), httpx.Response(500, text="oops"))])
    with pytest.raises(httpx.HTTPStatusError):
        src.get_history(Inst("ar-cafci:9001"), "2026-10-21", "2026-10-23")


# --- FT symbols -----------------------------------------------------------------------------------

def test_ft_symbol_needs_an_exact_match_and_is_a_nav():
    search = {"data": {"security": [{"symbol": "LU0000000001:EUR", "xid": "1", "name": "Look-alike"},
                                    {"symbol": "0P0000TEST", "xid": "2", "name": "Aviva Pension Example"}]}}
    series = {"Dates": ["2026-10-26T00:00:00", "2026-10-27T00:00:00"],
              "Elements": [{"Currency": "GBP", "ComponentSeries": [{"Type": "Close", "Values": [2.5, 2.5296]}]}]}
    src = mock(FTSource, [(path("searchsecurities"), httpx.Response(200, json=search)),
                          (lambda r: r.url.path.endswith("/series") and b'"2"' in r.content,
                           httpx.Response(200, json=series))])
    q = src.get_quote(Inst("ft:0P0000TEST"))
    assert (q.nav, q.date, q.currency, q.price_type) == (2.5296, "2026-10-27", "GBP", "nav")
    assert src.resolve("ft", "0P0000TEST") == {"id": "ft:0P0000TEST", "name": "Aviva Pension Example", "type": "fund"}
    assert src.resolve("ft", "0P0000NONE") is None
    assert src.get_quote(Inst("ft:0P0000NONE")) is None


def test_ft_supports_its_symbols_and_isins_only():
    src = FTSource(min_interval=0)
    assert src.supports(Inst("ft:0P0000TEST")) and src.supports(Inst("IE00B4L5Y983"))
    assert not src.supports(Inst("ticker:VOO"))


# --- analizy.pl -----------------------------------------------------------------------------------

def analizy_body(label, prices):
    return {"id": "X", "label": label, "currency": "PLN",
            "series": [{"id": "fund_X", "currency": "PLN", "price": [{"date": d, "value": v} for d, v in prices]}]}


def test_analizy_history_and_quote():
    body = analizy_body("Ejemplo Akcje Polskie", [("2026-10-01", 240.0), ("2026-10-02", 241.5), ("2026-10-05", 242.24)])
    src = mock(AnalizySource, [(path("/api/quotation/fio/PZU50"), httpx.Response(200, json=body))])
    q = src.get_quote(Inst("pl-analizy:PZU50"))
    assert (q.nav, q.date, q.currency) == (242.24, "2026-10-05", "PLN")
    h = src.get_history(Inst("pl-analizy:PZU50"), "2026-10-02", "2026-10-31")
    assert [(e.date, e.nav) for e in h.entries] == [("2026-10-02", 241.5), ("2026-10-05", 242.24)]
    assert src.get_quote(Inst("pl-analizy:NOPE1")) is None


@pytest.mark.parametrize("label, kind", [("Erste PPK 2050", PLAN), ("Allianz Polska OFE", PLAN),
                                         ("inPZU Akcje Polskie O", FUND)])
def test_analizy_resolve_tells_pension_products_apart(label, kind):
    src = mock(AnalizySource, [(path("/api/quotation/fio/"), httpx.Response(200, json=analizy_body(label, [])))])
    assert src.resolve("pl-analizy", "ABC12") == {"id": "pl-analizy:ABC12", "name": label, "currency": "PLN",
                                                  "type": kind}


# --- Gemel-net / Pensia-net -----------------------------------------------------------------------

def gemel_handler(records_by_resource):
    def handler(request):
        rid = request.url.params["resource_id"]
        if request.url.params.get("q"):
            recs = [{"FUND_ID": 880, "FUND_NAME": "מיטב השתלמות כללי", "MANAGING_CORPORATION": "מיטב"}]
        else:
            fund = json.loads(request.url.params["filters"])["FUND_ID"]
            recs = [r for r in records_by_resource.get(rid, []) if r["FUND_ID"] == fund]
            if request.url.params.get("sort", "").endswith("desc"):
                recs = sorted(recs, key=lambda r: -r["REPORT_PERIOD"])[:1]
        return httpx.Response(200, json={"result": {"records": recs}})
    return handler


def rec(period, yld, fund=880):
    return {"FUND_ID": fund, "FUND_NAME": "מיטב השתלמות כללי", "MANAGING_CORPORATION": "מיטב",
            "REPORT_PERIOD": period, "MONTHLY_YIELD": yld}


GEMEL_DATA = {
    "91c849ed-ddc4-472b-bd09-0f5486cea35c": [rec(202211, 10.0), rec(202212, None)],
    "2016d770-f094-4a2e-983e-797c26479720": [rec(202301, -10.0)],
    "a30dcbea-a1d2-482c-ae29-8f781f5025fb": [rec(202402, 5.0)],
}


def gemel():
    return mock(GemelNetSource, [(path("datastore_search"), gemel_handler(GEMEL_DATA))])


def test_gemel_chains_monthly_returns_into_a_fixed_base_index():
    h = gemel().get_history(Inst("il-gemel:880"), "1990-01-01", "2030-01-01")
    assert (h.price_type, h.currency) == ("index", "ILS")
    # 100 the month before the first report; the month with no return is skipped, not zeroed.
    assert [(e.date, e.nav) for e in h.entries] == [
        ("2022-10-31", 100.0), ("2022-11-30", 110.0), ("2023-01-31", 99.0), ("2024-02-29", 103.95)]


def test_gemel_index_does_not_depend_on_the_requested_range():
    h = gemel().get_history(Inst("il-gemel:880"), "2023-01-01", "2030-01-01")
    assert [(e.date, e.nav) for e in h.entries] == [("2023-01-31", 99.0), ("2024-02-29", 103.95)]
    q = gemel().get_quote(Inst("il-gemel:880"))
    assert (q.nav, q.date, q.price_type) == (103.95, "2024-02-29", "index")


def test_gemel_unknown_fund_and_resolve():
    src = gemel()
    assert src.get_quote(Inst("il-gemel:999")) is None
    assert src.resolve("il-gemel", "880") == {"id": "il-gemel:880", "name": "מיטב השתלמות כללי", "type": "plan",
                                              "currency": "ILS", "provider": "מיטב"}
    assert src.resolve("il-gemel", "999") is None


def test_gemel_search_skips_latin_queries():
    src = gemel()
    assert src.search("azvalor") == [] and src.calls == []
    hits = src.search("מיטב")
    assert {h["id"] for h in hits} == {"il-gemel:880", "il-pensia:880"} and len(src.calls) == 2
