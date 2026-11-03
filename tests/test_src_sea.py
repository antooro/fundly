"""Vietnam (fmarket) and Indonesia (Pasardana) against hand-made responses shaped like the real ones."""
import json
from datetime import date, timedelta

import httpx
import pytest

from fundly.ids import isin_check_digit, parse
from fundly.sources.base import ETF, FUND, Inst
from fundly.sources.indonesia import PasardanaSource
from fundly.sources.vietnam import FmarketSource
from test_sources import mock, path

# --- fmarket ----------------------------------------------------------------------------------------

FM_ROWS = [
    {"id": 7, "code": "AAA", "shortName": "AAA", "name": "QUỸ ĐẦU TƯ TRÁI PHIẾU MẪU",
     "owner": {"name": "CÔNG TY QUẢN LÝ QUỸ MẪU"}, "dataFundAssetType": {"name": "Quỹ trái phiếu"}},
    {"id": 8, "code": "bbb", "name": "QUỸ CỔ PHIẾU THỬ", "owner": {"name": "OTRA GESTORA"}},
    {"id": None, "code": "NOID", "name": "ignored"},
]


def fm_nav(request):
    body = json.loads(request.content)
    if body["productId"] != 7:
        return httpx.Response(200, json={"data": []})
    today = date.today()
    days = [today - timedelta(days=d) for d in (400, 30, 3, 2)]
    return httpx.Response(200, json={"data": [
        {"navDate": d.isoformat(), "nav": 10000.0 + i, "productId": 7} for i, d in enumerate(days)
    ] + [{"navDate": None, "nav": 1.0}]})


def fmarket():
    return mock(FmarketSource, [(path("/products/filter"), httpx.Response(200, json={"data": {"rows": FM_ROWS}})),
                                (path("/get-nav-history"), fm_nav)])


def test_fmarket_scheme_is_registered_upper_case():
    assert parse("vn-fmarket:vff") == ("vn-fmarket", "VFF")


def test_fmarket_quote_asks_a_recent_window_and_takes_the_last_nav():
    src = fmarket()
    q = src.get_quote(Inst("vn-fmarket:AAA", FUND))
    assert (q.nav, q.date, q.currency, q.price_type) == (10003.0, (date.today() - timedelta(days=2)).isoformat(), "VND", "nav")
    body = json.loads(next(r for r in src.calls if "get-nav-history" in r.url.path).content)
    assert body["isAllData"] == 0 and body["productId"] == 7
    assert body["fromDate"] == (date.today() - timedelta(days=40)).strftime("%Y%m%d")


def test_fmarket_history_is_trimmed_and_list_is_fetched_once():
    src = fmarket()
    start = (date.today() - timedelta(days=60)).isoformat()
    h = src.get_history(Inst("vn-fmarket:AAA", FUND), start, date.today().isoformat())
    assert [e.nav for e in h.entries] == [10001.0, 10002.0, 10003.0]
    src.get_quote(Inst("vn-fmarket:AAA", FUND))
    assert sum("/products/filter" in r.url.path for r in src.calls) == 1


def test_fmarket_unknown_code():
    assert fmarket().get_quote(Inst("vn-fmarket:ZZZ", FUND)) is None


def test_fmarket_search_handles_vietnamese_d_and_accents():
    hits = fmarket().search("dau tu trai phieu")
    assert [h["id"] for h in hits] == ["vn-fmarket:AAA"]
    h = hits[0]
    assert (h["type"], h["currency"], h["provider"], h["category"]) == (
        "fund", "VND", "CÔNG TY QUẢN LÝ QUỸ MẪU", "Quỹ trái phiếu")
    assert [x["id"] for x in fmarket().search("gestora")] == ["vn-fmarket:BBB"]
    assert fmarket().search("") == []


def test_fmarket_resolve():
    src = fmarket()
    assert src.resolve("vn-fmarket", "bbb")["name"] == "QUỸ CỔ PHIẾU THỬ"
    assert src.resolve("vn-fmarket", "NOID") is None and src.resolve("isin", "VN0000000000") is None


def test_fmarket_pages_through_long_lists():
    rows = [{"id": i, "code": f"F{i}", "name": f"Fund {i}"} for i in range(150)]

    def filt(request):
        page = json.loads(request.content)["page"]
        return httpx.Response(200, json={"data": {"rows": rows[(page - 1) * 100:page * 100]}})

    src = mock(FmarketSource, [(path("/products/filter"), filt)])
    assert src.resolve("vn-fmarket", "F149")["id"] == "vn-fmarket:F149"


# --- Pasardana --------------------------------------------------------------------------------------

def isin(body):
    return body + isin_check_digit(body)


IDR_ISIN, USD_ISIN, ETF_ISIN = isin("IDN00000100"), isin("IDN00000200"), isin("IDN00000300")
PD_ROWS = [
    {"Id": 1, "Name": "Reksa Dana Contoh Campuran", "IsinCode": IDR_ISIN, "NetAssetValue": 944.2687,
     "LastUpdate": "2026-10-27T00:00:00", "InvestmentManagerName": "Contoh Asset Management, PT", "Currency": 0,
     "ExchangeTradedFund": False},
    {"Id": 2, "Name": "Contoh Dana USD", "IsinCode": USD_ISIN, "NetAssetValue": 1.1491,
     "LastUpdate": "2026-10-24T00:00:00", "InvestmentManagerName": "Lain AM", "Currency": 1},
    {"Id": 3, "Name": "Contoh ETF IDX30", "IsinCode": ETF_ISIN, "NetAssetValue": 512.0,
     "LastUpdate": "2026-10-27T00:00:00", "InvestmentManagerName": "Lain AM", "Currency": 0, "ExchangeTradedFund": True},
    {"Id": 4, "Name": "Dana Tanpa ISIN", "IsinCode": "RAM02MXCMPOWER00", "NetAssetValue": 1000.5,
     "LastUpdate": "2026-10-27T00:00:00", "Currency": 0},
    {"Id": 5, "Name": "Mata Uang Aneh", "IsinCode": None, "NetAssetValue": 10.0, "LastUpdate": "2026-10-27", "Currency": 9},
    {"Id": None, "Name": "ignored"},
]


PD_SNAPSHOT = {"Id": 1, "Name": "Reksa Dana Contoh Campuran", "NetAssetValues": [
    {"Date": "2026-10-27T00:00:00", "Value": 944.2687, "DailyReturn": 0.001},
    {"Date": "2026-10-23T00:00:00", "Value": 941.1, "DailyReturn": None},
    {"Date": "2026-10-24T00:00:00", "Value": None},  # gaps are skipped
    {"Date": "", "Value": 1.0}, {"Date": "2025-11-03T00:00:00", "Value": 1001.5}]}


def pasardana():
    def snapshot(request):
        if request.url.params.get("fundId") == "1" and request.url.params.get("username") == "anonymous":
            return httpx.Response(200, json=PD_SNAPSHOT)
        return httpx.Response(200, content=b"null")  # what the site answers for an unknown fund

    return mock(PasardanaSource, [(path("/FundSearchResult/GetAll"), httpx.Response(200, json=PD_ROWS)),
                                  (path("/FundService/GetSnapshot"), snapshot)])


def test_pasardana_quote_by_isin_and_by_id_with_currency_enum():
    src = pasardana()
    q = src.get_quote(Inst(IDR_ISIN, FUND))
    assert (q.nav, q.date, q.currency, q.price_type) == (944.2687, "2026-10-27", "IDR", "nav")
    assert src.get_quote(Inst("id-pasardana:1", FUND)).nav == 944.2687
    assert src.get_quote(Inst(USD_ISIN, FUND)).currency == "USD"
    assert src.get_quote(Inst("id-pasardana:5", FUND)) is None  # unknown currency: refuse rather than guess
    assert sum("GetAll" in r.url.path for r in src.calls) == 1


def test_pasardana_history_from_the_fund_snapshot():
    src = pasardana()
    h = src.get_history(Inst(IDR_ISIN, FUND), "2026-01-01", "2026-10-27")
    assert [(e.date, e.nav) for e in h.entries] == [("2026-10-23", 941.1), ("2026-10-27", 944.2687)]
    assert (h.currency, h.source) == ("IDR", "pasardana")
    assert len(src.get_history(Inst("id-pasardana:1", FUND), "2025-01-01", "2026-10-27").entries) == 3


def test_pasardana_history_outside_the_last_year_or_for_unknown_funds_is_none():
    src = pasardana()
    assert src.get_history(Inst(IDR_ISIN, FUND), "2020-01-01", "2020-12-31") is None
    assert src.get_history(Inst(USD_ISIN, FUND), "2026-01-01", "2026-10-27") is None  # snapshot is null
    assert src.get_history(Inst("id-pasardana:999", FUND), "2026-01-01", "2026-10-27") is None


def test_pasardana_history_grows_past_twelve_months_on_disk(tmp_path):
    src = pasardana()
    src.files_dir = tmp_path
    src.get_quote(Inst(IDR_ISIN, FUND))  # the daily list is kept too
    assert sorted(p.name for p in tmp_path.glob("list-*")) == ["list-2026-10-24.json", "list-2026-10-27.json"]
    src.get_history(Inst(IDR_ISIN, FUND), "2025-01-01", "2026-12-31")
    # a year later the site's window has moved on and no longer shows 2025
    snapshot_later = {"NetAssetValues": [{"Date": "2027-10-27T00:00:00", "Value": 990.0}]}
    later = mock(PasardanaSource, [(path("/FundSearchResult/GetAll"), httpx.Response(200, json=PD_ROWS)),
                                   (path("/FundService/GetSnapshot"), httpx.Response(200, json=snapshot_later))])
    later.files_dir = tmp_path
    h = later.get_history(Inst(IDR_ISIN, FUND), "2025-01-01", "2027-12-31")
    assert [(e.date, e.nav) for e in h.entries] == [
        ("2025-11-03", 1001.5), ("2026-10-23", 941.1), ("2026-10-27", 944.2687), ("2027-10-27", 990.0)]


def test_pasardana_list_navs_fill_days_the_history_never_had(tmp_path):
    src = pasardana()
    src.files_dir = tmp_path
    src.get_quote(Inst(USD_ISIN, FUND))
    # fund 2 has no snapshot (null), but the kept list gives it its 24/10 NAV
    h = src.get_history(Inst(USD_ISIN, FUND), "2026-10-01", "2026-10-31")
    assert [(e.date, e.nav) for e in h.entries] == [("2026-10-24", 1.1491)]


def test_pasardana_only_claims_indonesian_isins():
    src = pasardana()
    assert src.supports(Inst(IDR_ISIN))
    assert src.supports(Inst("id-pasardana:4"))
    assert not src.supports(Inst("IE00B4L5Y983"))


def test_pasardana_internal_codes_are_not_treated_as_isins():
    src = pasardana()
    hit = src.resolve("id-pasardana", "4")
    assert hit["id"] == "id-pasardana:4" and hit["name"] == "Dana Tanpa ISIN"
    assert src.resolve("id-pasardana", "1")["id"] == IDR_ISIN  # canonical id is the ISIN when there is one
    assert src.resolve("isin", ETF_ISIN)["type"] == ETF
    assert src.resolve("id-pasardana", "999") is None


def test_pasardana_search_by_name_and_manager():
    src = pasardana()
    assert [h["id"] for h in src.search("contoh campuran")] == [IDR_ISIN]
    assert {h["id"] for h in src.search("lain am")} == {USD_ISIN, ETF_ISIN}
    assert src.search("") == []


def test_pasardana_scheme_validation():
    assert parse("id-pasardana:13") == ("id-pasardana", "13")
    with pytest.raises(ValueError):
        parse("id-pasardana:abc")
