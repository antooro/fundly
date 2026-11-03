"""Chile, Colombia and Brazil sources against hand-made responses shaped like the real ones."""
import io
import zipfile
from datetime import date

import httpx
import pytest

from fundly import Client
from fundly.ids import parse
from fundly.sources.base import Inst
from fundly.sources.brazil import BrazilCVMSource
from fundly.sources.chile import ChileAFPSource
from fundly.sources.colombia import ColombiaSource
from test_sources import mock, path

THIS_YEAR = date.today().year


# --- ids ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("cl-afp:habitat-a", ("cl-afp", "HABITAT-A")),
    ("co-pension:10-6000", ("co-pension", "10-6000")),
    ("co-fic:28-58756-808", ("co-fic", "28-58756-808")),
    ("br-cvm:00017024000153", ("br-cvm", "00017024000153")),
    ("br-cvm:00017024000153/abc123", ("br-cvm", "00017024000153/ABC123")),
])
def test_scheme_registration(raw, expected):
    assert parse(raw) == expected


@pytest.mark.parametrize("raw", ["cl-afp:HABITAT", "cl-afp:HABITAT-F", "co-pension:10", "co-fic:1-2",
                                 "br-cvm:00.017.024/0001-53", "br-cvm:123"])
def test_scheme_rejects(raw):
    from fundly import InvalidId
    with pytest.raises(InvalidId):
        parse(raw)


# --- Chile ----------------------------------------------------------------------------------------

def chile_csv(rows: list[str], afps=("CAPITAL", "HABITAT", "UNO")) -> bytes:
    header = "Fecha;" + ";;".join(afps)
    sub = ";" + ";".join(["Valor Cuota;Valor Patrimonio"] * len(afps))
    return "\n".join(["", "Valores Confirmados", "", header, sub, *rows]).encode("latin-1")


CHILE_THIS_YEAR = chile_csv([
    f"{THIS_YEAR}-01-01;10,00;1;20,00;2;30,00;3",           # holiday: repeats the last day of the previous year
    f"{THIS_YEAR}-01-02;10,50;1;20.100,25;2;30,50;3",
    f"{THIS_YEAR}-01-03;10,50;1;20.100,25;2;30,50;3",       # Saturday-style fill, see weekday filter below
    f"{THIS_YEAR}-01-05;10,75;1;20.200,50;2;;",             # UNO missing that day
    f"{THIS_YEAR}-01-06;;;20.300,00;2;;",                   # partial last row
])
CHILE_LAST_YEAR = chile_csv([f"{THIS_YEAR - 1}-12-31;10,00;1;20,00;2;30,00;3"])


def chile_routes(this_year=CHILE_THIS_YEAR):
    return [(lambda r: r.url.params.get("aaaaini") == str(THIS_YEAR), httpx.Response(200, content=this_year)),
            (lambda r: r.url.params.get("aaaaini") == str(THIS_YEAR - 1), httpx.Response(200, content=CHILE_LAST_YEAR))]


def test_chile_parse_skips_weekends_repeats_and_blanks():
    text = chile_csv(["2026-10-23;95.170,02;1;1;1;1;1", "2026-10-24;95.170,02;1;1;1;1;1",
                      "2026-10-25;95.170,02;1;1;1;1;1", "2026-10-26;95.300,10;2;;;;"]).decode("latin-1")
    series = ChileAFPSource._parse(text)
    assert [(e.date, e.nav) for e in series["CAPITAL"]] == [("2026-10-23", 95170.02), ("2026-10-26", 95300.10)]
    assert [e.date for e in series["HABITAT"]] == ["2026-10-23"]


def test_chile_quote_and_history_across_years():
    src = mock(ChileAFPSource, chile_routes())
    inst = Inst("cl-afp:HABITAT-A", "plan")
    q = src.get_quote(inst)
    assert (q.nav, q.currency) == (20300.0, "CLP")
    h = src.get_history(inst, f"{THIS_YEAR - 1}-12-01", f"{THIS_YEAR}-12-31")
    navs = [e.nav for e in h.entries]
    assert navs[0] == 20.0 and 20.0 not in navs[1:]  # the 1 January holiday repeat is dropped
    assert all(src.calls[i].url.params["tf"] == "A" for i in range(len(src.calls)))
    # Past years are final: asking again doesn't download them twice.
    src.get_history(inst, f"{THIS_YEAR - 1}-12-01", f"{THIS_YEAR - 1}-12-31")
    assert sum(r.url.params["aaaaini"] == str(THIS_YEAR - 1) for r in src.calls) == 1


def test_chile_quote_falls_back_to_last_year_in_early_january():
    empty = chile_csv([])
    src = mock(ChileAFPSource, chile_routes(this_year=empty))
    assert src.get_quote(Inst("cl-afp:UNO-C", "plan")).nav == 30.0


def test_chile_search_and_resolve():
    src = mock(ChileAFPSource, chile_routes())
    assert [h["id"] for h in src.search("habitat")] == [f"cl-afp:HABITAT-{f}" for f in "ABCDE"]
    assert [h["id"] for h in src.search("chile uno fondo e")] == ["cl-afp:UNO-E"]
    assert src.resolve("cl-afp", "CAPITAL-B") == {
        "id": "cl-afp:CAPITAL-B", "name": "AFP Capital Fondo B", "type": "plan", "currency": "CLP",
        "provider": "AFP Capital", "category": "Fondo B"}
    assert src.resolve("cl-afp", "NOPE-A") is None


def test_chile_through_the_client():
    src = mock(ChileAFPSource, chile_routes())
    c = Client(cache=False, sources=[src])
    q = c.quote(" cl-afp:habitat-a ")
    assert (q.id, q.name, q.price) == ("cl-afp:HABITAT-A", "AFP Habitat Fondo A", 20300.0)
    assert c.instrument(q.id).domicile == "CL"


# --- Colombia -------------------------------------------------------------------------------------

PENSION_ROWS = [
    {"fecha": "2026-10-24T00:00:00.000", "codigo_entidad": "10", "codigo_patrimonio": "6000", "valor_unidad": "97104.94"},
    {"fecha": "2026-10-25T00:00:00.000", "codigo_entidad": "10", "codigo_patrimonio": "6000", "valor_unidad": "97116.41"},
]
FIC_ROWS = [
    {"fecha_corte": "2026-10-22T00:00:00.000", "valor_unidad_operaciones": "10350.135706"},
    {"fecha_corte": "2026-10-22T00:00:00.000", "valor_unidad_operaciones": "10350.135706"},
]
PENSION_LIST = [{"codigo_entidad": "10", "nombre_entidad": '"Colfondos S.A." Y "Colfondos"',
                 "codigo_patrimonio": "6000", "nombre_fondo": "Fondo de Pensiones Mayor Riesgo"}]
FIC_LIST = [{"codigo_entidad": "28", "nombre_entidad": "Credicorp Capital", "codigo_negocio": "58756",
             "nombre_patrimonio": "CREDICORP CAPITAL ALTA LIQUIDEZ", "tipo_participacion": "808",
             "nombre_subtipo_patrimonio": "FIC DE TIPO GENERAL"}]


def colombia_routes():
    def pension(r):
        if "$group" in r.url.params:
            return httpx.Response(200, json=PENSION_LIST)
        assert (r.url.params["codigo_entidad"], r.url.params["codigo_patrimonio"]) == ("10", "6000")
        rows = PENSION_ROWS[-1:] if r.url.params.get("$limit") == "1" else PENSION_ROWS
        return httpx.Response(200, json=rows)

    def fic(r):
        if "$group" in r.url.params:
            return httpx.Response(200, json=FIC_LIST)
        assert r.url.params["tipo_participacion"] == "808"
        return httpx.Response(200, json=FIC_ROWS)

    return [(path("uawh-cjvi"), pension), (path("qhpu-8ixx"), fic)]


def test_colombia_pension_quote_and_history():
    src = mock(ColombiaSource, colombia_routes())
    inst = Inst("co-pension:10-6000", "plan")
    q = src.get_quote(inst)
    assert (q.nav, q.date, q.currency) == (97116.41, "2026-10-25", "COP")
    h = src.get_history(inst, "2026-10-20", "2026-10-27")
    assert [(e.date, e.nav) for e in h.entries] == [("2026-10-24", 97104.94), ("2026-10-25", 97116.41)]
    where = next(r for r in src.calls if "$where" in r.url.params).url.params["$where"]
    assert "2026-10-20T00:00:00" in where and "2026-10-27T23:59:59" in where


def test_colombia_fic_dedupes_repeated_dates():
    src = mock(ColombiaSource, colombia_routes())
    h = src.get_history(Inst("co-fic:28-58756-808", "fund"), "2026-10-01", "2026-10-31")
    assert [(e.date, e.nav) for e in h.entries] == [("2026-10-22", 10350.135706)]


def test_colombia_search_resolve_and_clean_names():
    src = mock(ColombiaSource, colombia_routes())
    hits = src.search("colfondos mayor riesgo")
    assert [h["id"] for h in hits] == ["co-pension:10-6000"]
    assert hits[0]["provider"] == "Colfondos S.A. Y Colfondos"
    assert src.resolve("co-fic", "28-58756-808")["name"].startswith("CREDICORP CAPITAL ALTA LIQUIDEZ")
    assert src.search("credicorp")[0]["type"] == "fund"
    src.search("colombia")
    assert sum("$group" in r.url.params for r in src.calls) == 2  # catalogue fetched once


# --- Brazil ---------------------------------------------------------------------------------------

def zip_of(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text.encode("latin-1"))
    return buf.getvalue()


def ym(d: date) -> str:
    return d.strftime("%Y%m")


TODAY = date.today()
PREV = date(TODAY.year - (TODAY.month == 1), (TODAY.month - 2) % 12 + 1, 1)
DAILY_HEADER = ("TP_FUNDO_CLASSE;CNPJ_FUNDO_CLASSE;ID_SUBCLASSE;DT_COMPTC;VL_TOTAL;VL_QUOTA;VL_PATRIM_LIQ;"
                "CAPTC_DIA;RESG_DIA;NR_COTST\r\n")


def daily(month: date, rows: list[tuple[str, str, int, str]]) -> bytes:
    lines = [f"CLASSES - FIF;{c};{s};{month:%Y-%m}-{d:02d};1;{v};1;0;0;1\r\n" for c, s, d, v in rows]
    return zip_of({f"inf_diario_fi_{ym(month)}.csv": DAILY_HEADER + "".join(lines)})


A, B = "00.017.024/0001-53", "11.222.333/0001-81"
CURRENT = daily(TODAY, [(A, "", 1, "45.10"), (A, "", 2, "45.20"), (A, "SUB1", 2, "9.99"), (B, "", 2, "1.50")])
PREVIOUS = daily(PREV, [(A, "", 28, "44.90"), (B, "", 28, "1.40")])
YEAR_2020 = zip_of({"inf_diario_fi_202012.csv": "TP_FUNDO;CNPJ_FUNDO;DT_COMPTC;VL_TOTAL;VL_QUOTA;VL_PATRIM_LIQ\r\n"
                                                  f"FI;{A};2020-12-30;1;30.00;1\r\n"})
JAN_2021 = daily(date(2021, 1, 1), [(A, "", 4, "30.50")])
REGISTER = zip_of({
    "registro_fundo.csv": "ID_Registro_Fundo;CNPJ_Fundo;Tipo_Fundo;Denominacao_Social;Situacao;Administrador;Gestor\r\n"
                          "1;00017024000153;FIF;FUNDO EXPONENCIAL;Em Funcionamento Normal;ADM;BRADESCO\r\n"
                          "2;99999999000199;FIF;FUNDO VELHO EXPONENCIAL;Cancelado;ADM;X\r\n",
    "registro_classe.csv": "ID_Registro_Fundo;ID_Registro_Classe;CNPJ_Classe;Tipo_Classe;Denominacao_Social;Situacao;"
                           "Classificacao;Patrimonio_Liquido\r\n"
                           "1;10;00017024000153;FIF;CLASSE RENDA FIXA EXPONENCIAL;Em Funcionamento Normal;Renda Fixa;1000\r\n"
                           "1;11;11222333000181;FIF;CLASSE PREVIDÊNCIA EXPONENCIAL;Em Funcionamento Normal;Previdência;5000\r\n",
    "registro_subclasse.csv": "ID_Registro_Classe;ID_Subclasse;Denominacao_Social;Situacao\r\n"
                              "10;SUB1;SUBCLASSE A;Em Funcionamento Normal\r\n",
})


def brazil_routes():
    return [
        (path(f"inf_diario_fi_{ym(TODAY)}.zip"), httpx.Response(200, content=CURRENT)),
        (path(f"inf_diario_fi_{ym(PREV)}.zip"), httpx.Response(200, content=PREVIOUS)),
        (path("HIST/inf_diario_fi_2020.zip"), httpx.Response(200, content=YEAR_2020)),
        (path("inf_diario_fi_202101.zip"), httpx.Response(200, content=JAN_2021)),
        (path("registro_fundo_classe.zip"), httpx.Response(200, content=REGISTER)),
    ]


def test_brazil_quote_reads_only_the_asked_class_and_reuses_the_month_file():
    src = mock(BrazilCVMSource, brazil_routes())
    q = src.get_quote(Inst("br-cvm:00017024000153", "fund"))
    assert (q.nav, q.date, q.currency) == (45.20, f"{TODAY:%Y-%m}-02", "BRL")
    assert src.get_quote(Inst("br-cvm:00017024000153/SUB1", "fund")).nav == 9.99
    assert src.get_quote(Inst("br-cvm:11222333000181", "fund")).nav == 1.50
    assert sum(ym(TODAY) in r.url.path for r in src.calls) == 1


def test_brazil_quote_falls_back_to_previous_month():
    routes = [(path(f"inf_diario_fi_{ym(TODAY)}.zip"), httpx.Response(404)), *brazil_routes()]
    q = mock(BrazilCVMSource, routes).get_quote(Inst("br-cvm:00017024000153", "fund"))
    assert (q.nav, q.date) == (44.90, f"{PREV:%Y-%m}-28")


def test_brazil_history_spans_yearly_and_monthly_files():
    src = mock(BrazilCVMSource, brazil_routes())
    h = src.get_history(Inst("br-cvm:00017024000153", "fund"), "2020-12-15", "2021-01-31")
    assert [(e.date, e.nav) for e in h.entries] == [("2020-12-30", 30.0), ("2021-01-04", 30.5)]


def test_brazil_search_uses_register_skips_cancelled_and_subclasses():
    src = mock(BrazilCVMSource, brazil_routes())
    hits = src.search("exponencial")
    assert [h["id"] for h in hits] == ["br-cvm:11222333000181", "br-cvm:00017024000153"]  # bigger first
    assert hits[1]["provider"] == "BRADESCO" and hits[1]["category"] == "Renda Fixa"
    assert all(not k.startswith("_") for h in hits for k in h)
    assert src.search("previdencia")[0]["id"] == "br-cvm:11222333000181"
    sub = src.resolve("br-cvm", "00017024000153/sub1")
    assert sub["name"] == "CLASSE RENDA FIXA EXPONENCIAL - SUBCLASSE A"
    assert src.resolve("br-cvm", "99999999000199") is None
    assert sum("registro" in r.url.path for r in src.calls) == 1
