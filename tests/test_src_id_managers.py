"""Indonesian managers' own NAV histories (id_managers) against hand-made pages shaped like the real ones."""
import json

import httpx

from fundly.ids import isin_check_digit
from fundly.sources.base import FUND, Inst
from fundly.sources.id_managers import (Avrist, Batavia, Henan, KoreaInvestment, Samuel, Syailendra, Trimegah,
                                       manager_key, match_name)
from fundly.sources.indonesia import PasardanaSource
from test_sources import mock, path


def getter(routes):
    """A get(url, **kw) like Source._get, answering from {url fragment: response}."""
    def handler(request):
        for fragment, resp in routes.items():
            if fragment in str(request.url):
                return resp
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler)).get


def test_names_match_across_spellings():
    assert match_name("Reksa Dana Syailendra Dana Kas (SDK) | Syailendra Capital") == "syailendra dana kas"
    assert match_name("RD Batavia Dana Saham") == match_name("BATAVIA DANA SAHAM") == "batavia dana saham"
    assert manager_key("Syariah Trimegah Dana Tetap Syariah 2", "trimegah") == "trimegah dana tetap syariah 2"
    assert manager_key("Batavia Dana Saham", "trimegah") == "batavia dana saham"


BATAVIA_RETAIL = '''<a class="products-card__item " href="https://bpam.co.id/product/saham/batavia_dana_saham" target="_self">
  <div class="products-card__header"><span class="products-card__label">Batavia Dana Saham </span><svg></svg></div></a>'''
BATAVIA_INST = '''<a class="products-card__item institusi" href="https://bpam.co.id/userfiles/uploads/files/PROSP-BDKG-ID.pdf">
  <div><span class="products-card__label">Batavia Dana Kas Gebyar </span></div></a>
  <a href="https://bpam.co.id/userfiles/uploads/files/69ce41f9f1130.pdf"><span class="products-card__label">X</span></a>'''


def test_batavia_retail_page_gives_the_code_and_institutional_funds_their_prospectus():
    get = getter({
        "/produk/institusi": httpx.Response(200, text=BATAVIA_INST),
        "/produk": httpx.Response(200, text=BATAVIA_RETAIL),
        "/product/saham/batavia_dana_saham": httpx.Response(200, text='<span class="banner-subtitle">BDS</span>'),
        "code=BDS": httpx.Response(200, text=json.dumps({"data": [{"date": "1996-12-16", "value": "1000.000000000000"},
                                                                  {"date": "1996-12-17", "value": "bad"},
                                                                  {"date": "2026-11-02", "value": "8123.5"}]})),
        "code=BDKG": httpx.Response(200, text="[]"),  # unknown code: a bare list
    })
    b = Batavia()
    cat = b.catalogue(get)
    assert cat == {"Batavia Dana Saham": "https://bpam.co.id/product/saham/batavia_dana_saham",
                   "Batavia Dana Kas Gebyar": "BDKG"}
    assert b.history(get, cat["Batavia Dana Saham"]) == {"1996-12-16": 1000.0, "2026-11-02": 8123.5}
    assert b.history(get, "BDKG") == {}


def test_trimegah_reads_the_chart_embedded_in_the_page():
    card = ('<article class="card"><a href="https://www.trimegah-am.com/id/reksadana/detail/TKASA"><div class="card-header">'
            '<img alt="x" /><h3 class="card-title" title="TRIM KAS 2 KELAS A">TRIM KAS 2 KELAS A</h3></div></a></article>')
    chart = {"ALL": {"labels": ["11 Apr 2008", "02 Mei 2008", "nonsense"], "data": [1000, 1001.5, 3]}, "1M": {}}
    page = (f"initReksadanaChart(\n 'container_nav_chart', \n 'container_nav_tab', \n 'h', \n {json.dumps(chart)}\n);"
            "initReksadanaChart('container_aum_chart', 'a', 'b', {\"ALL\": {\"labels\": [\"01 Jan 2020\"], \"data\": [5]}});")
    get = getter({"/detail/TKASA": httpx.Response(200, text=page), "/id/reksadana": httpx.Response(200, text=card)})
    t = Trimegah()
    assert t.catalogue(get) == {"TRIM KAS 2 KELAS A": "TKASA"}
    assert t.history(get, "TKASA") == {"2008-04-11": 1000.0, "2008-05-02": 1001.5}  # NAV chart, not AUM


def test_avrist_api_drops_zero_navs():
    listing = {"data": {"contentDataList": [
        {"title": "Avrist Dana Kas Ruby", "contentData": [{"fieldId": "produk-id", "value": "TP002MMCAVRUBY00"}]},
        {"title": "Sin id", "contentData": []}, "junk"]}}
    navs = {"data": {"history": [{"date": "2025-09-18", "nav": 1000.0}, {"date": "2026-11-02", "nav": 0.0}]}}
    get = getter({"/content/Reksa-Dana": httpx.Response(200, json=listing),
                  "/nav-list/TP002MMCAVRUBY00/1990-01-01/": httpx.Response(200, json=navs)})
    a = Avrist()
    assert a.catalogue(get) == {"Avrist Dana Kas Ruby": "TP002MMCAVRUBY00"}
    assert a.history(get, "TP002MMCAVRUBY00") == {"2025-09-18": 1000.0}


def test_syailendra_menu_and_inline_data():
    url = "https://www.syailendracapital.com/product/reksa-dana-pasar-uang/syailendra-dana-kas-sdk"
    menu = f'<li class=""><a href="{url}">Syailendra Dana Kas (SDK)</a></li>'
    page = 'let dataMaster = [["2015-01-05",1000],["2015-01-06",1000.31],["x"]];'
    get = getter({"/reksa-dana-saham": httpx.Response(200, text=menu), url: httpx.Response(200, text=page)})
    s = Syailendra()
    assert s.catalogue(get) == {"Syailendra Dana Kas (SDK)": url}
    assert s.history(get, url) == {"2015-01-05": 1000.0, "2015-01-06": 1000.31}


def test_samuel_google_charts_rows_count_months_from_zero():
    url = "https://www.sam.co.id/produk/detail/sam-dana-kas"
    listing = f'<td><a href="{url}">\n   SAM DANA KAS   \t</a></td>'
    page = "data.addRows([[new Date(2026, 10 ,03), 1560.4841],[new Date(2017, 1 ,10), 1000],[new Date(2017, 1 ,10), 1000]]);"
    get = getter({"/produk/nab": httpx.Response(200, text=listing), url: httpx.Response(200, text=page)})
    s = Samuel()
    assert s.catalogue(get) == {"SAM DANA KAS": url}
    assert s.history(get, url) == {"2017-02-10": 1000.0, "2026-11-03": 1560.4841}
    assert s.key("RDPT SAM JALAN TOL") == s.key("SAM Penyertaan Terbatas Jalan Tol")
    assert s.key("Indeks SAM ETF Sri Kehati") == s.key("SAM ETF Sri Kehati")


def test_henan_api_list_and_long_history():
    listing = {"data": {"produk": [{"id": 1, "nama_produk": "Henan Ultima Ekuitas Kelas A "}, {"id": None}, "x"]}}
    navs = {"data": {"kinerja": [{"tanggal": "2009-12-07", "nav": 1000}, {"tanggal": "2026-11-02", "nav": 2082.7017}]}}
    get = getter({"/produk/nav3?kinerja=30Y&produk_id=1": httpx.Response(200, json=navs),
                  "/produk": httpx.Response(200, json=listing)})
    h = Henan()
    assert h.catalogue(get) == {"Henan Ultima Ekuitas Kelas A": "1"}
    assert h.history(get, "1") == {"2009-12-07": 1000.0, "2026-11-02": 2082.7017}


def test_korea_investment_reads_the_escaped_attribute_and_aliases_names():
    url = "https://koreainvestment.co.id/produk/kim-equity-fund"
    listing = f'<a href="{url}" class="produk-card"><div><h2 class="produk-card__name">KIM Equity Fund</h2></div></a>'
    page = '<div data-nav="[{&quot;date&quot;:&quot;2019-12-12&quot;,&quot;nav&quot;:1000}]"></div>'
    get = getter({url: httpx.Response(200, text=page), "/produk": httpx.Response(200, text=listing)})
    k = KoreaInvestment()
    assert k.catalogue(get) == {"KIM Equity Fund": url}
    assert k.history(get, url) == {"2019-12-12": 1000.0}
    assert k.key("Indeks Kisi IDX30 ETF") == k.key("KISI IDX30 ETF")
    assert k.key("KIM SYARIAH SUKUK FUND") == k.key("KIM Sharia Sukuk Fund")


# --- merged into Pasardana's history ----------------------------------------------------------------

ISIN = "IDN00000500" + isin_check_digit("IDN00000500")
ROWS = [{"Id": 7, "Name": "RD Batavia Dana Saham", "IsinCode": ISIN, "NetAssetValue": 8123.5,
         "LastUpdate": "2026-11-02T00:00:00", "InvestmentManagerName": "Batavia Prosperindo Aset Manajemen, PT",
         "Currency": 0}]
SNAPSHOT = {"NetAssetValues": [{"Date": "2026-10-30T00:00:00", "Value": 8100.0},
                               {"Date": "2026-11-02T00:00:00", "Value": 8123.5}]}


def pasardana_with_batavia(batavia_history):
    return mock(PasardanaSource, [
        (path("/FundSearchResult/GetAll"), httpx.Response(200, json=ROWS)),
        (path("/FundService/GetSnapshot"), httpx.Response(200, json=SNAPSHOT)),
        (lambda r: r.url.path == "/produk", httpx.Response(200, text=BATAVIA_RETAIL)),
        (lambda r: r.url.path == "/produk/institusi", httpx.Response(200, text="")),
        (path("/product/saham/batavia_dana_saham"), httpx.Response(200, text='<span class="banner-subtitle">BDS</span>')),
        (path("/nav/get_navs_json"), batavia_history),
    ])


def test_pasardana_history_reaches_back_with_the_managers_own_series():
    src = pasardana_with_batavia(httpx.Response(200, text=json.dumps({"data": [
        {"date": "1996-12-16", "value": "1000"}, {"date": "2026-10-30", "value": "8099.99"}]})))
    h = src.get_history(Inst(ISIN, FUND), "1990-01-01", "2026-12-31")
    assert [(e.date, e.nav) for e in h.entries] == [("1996-12-16", 1000.0), ("2026-10-30", 8100.0), ("2026-11-02", 8123.5)]
    assert h.source == "pasardana" and h.currency == "IDR"  # Pasardana's own values win on shared days


def test_a_failing_manager_site_leaves_pasardanas_history_intact():
    src = pasardana_with_batavia(httpx.Response(503, text="down"))
    h = src.get_history(Inst(ISIN, FUND), "1990-01-01", "2026-12-31")
    assert [e.date for e in h.entries] == ["2026-10-30", "2026-11-02"]


def test_funds_of_other_managers_cost_no_manager_requests():
    rows = [{**ROWS[0], "InvestmentManagerName": "Contoh AM"}]
    src = mock(PasardanaSource, [(path("/FundSearchResult/GetAll"), httpx.Response(200, json=rows)),
                                 (path("/FundService/GetSnapshot"), httpx.Response(200, json=SNAPSHOT))])
    src.get_history(Inst(ISIN, FUND), "2026-01-01", "2026-12-31")
    assert all("pasardana" in str(r.url) for r in src.calls)


def test_batavia_cards_since_november_2026_carry_the_code_and_english_files():
    retail = ('<a\r\n class="products-card__item BDKM Dummy"\r\n href="https://bpam.co.id/en/product/money_market/'
              'batavia_dana_kas_maxima"\r\n target="_self">\r\n'
              '<span class="products-card__label">Batavia Dana Kas Maxima </span>'
              '<a class="products-card__item " href="https://bpam.co.id/en/product/saham/batavia_dana_saham">'
              '<span class="products-card__label">Batavia Dana Saham</span>')
    institutional = ('<a class="products-card__item institusi" href="https://bpam.co.id/userfiles/uploads/files/PROSP-BDKS-C-EN.pdf">'
                     '<span class="products-card__label">Batavia Dana Kas Sentosa C</span>')
    get = getter({"/produk/institusi": httpx.Response(200, text=institutional), "/produk": httpx.Response(200, text=retail)})
    assert Batavia().catalogue(get) == {"Batavia Dana Kas Maxima": "BDKM",  # no fund page needed
                                        "Batavia Dana Saham": "https://bpam.co.id/en/product/saham/batavia_dana_saham",
                                        "Batavia Dana Kas Sentosa C": "BDKS-C"}
