"""India (AMFI, NPS) and China (Eastmoney) sources against hand-made responses."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from fundly import ids
from fundly.sources.base import ETF, FUND, Inst
from fundly.sources.china import EastmoneySource
from fundly.sources.india import AMFISource, NPSSource
from test_sources import mock, path

NAVALL = "\r\n".join([
    "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date",
    " ",
    "Open Ended Schemes(Equity Scheme - Mid Cap Fund)",
    " ",
    "Example Mutual Fund",
    " ",
    "100001;INF000A01AA1;-;Example Mid Cap Fund;Direct Plan;Growth Option;216.123;27-Oct-2026",
    "100002;INF000A01AB9;INF000A01AC7;Example Mid Cap Fund;Regular Plan;IDCW Option;76.741;27-Oct-2026",
    "100003;-;-;Example Closed Fund Series 1;;;N.A.;27-Oct-2026",
    " ",
    "Close Ended Schemes(Debt Scheme - Fixed Maturity Plan)",
    " ",
    "Otra Gestora Mutual Fund",
    " ",
    "100004;INF000B01AA0;-;Otra Débt Fund;Direct Plan;Growth;10.5;24-Oct-2026",
])
MFAPI = {"meta": {"scheme_code": 100001}, "status": "SUCCESS",
         "data": [{"date": "26-10-2026", "nav": "214.64100"}, {"date": "23-10-2026", "nav": "213.42300"},
                  {"date": "01-09-2026", "nav": "200.00000"}, {"date": "bad", "nav": "1"}]}


def amfi(**extra):
    routes = [(path("NAVAll.txt"), httpx.Response(200, text=NAVALL)),
              (path("/mf/100001"), httpx.Response(200, json=MFAPI))]
    return mock(AMFISource, routes, **extra)


def test_schemes_are_registered():
    assert ids.parse("in-amfi:118989") == ("in-amfi", "118989")
    assert ids.parse("in-nps:sm001001") == ("in-nps", "SM001001")
    assert ids.parse("cn:110022") == ("cn", "110022")
    for bad in ("in-amfi:abc", "in-nps:XX001001", "cn:11002"):
        with pytest.raises(ids.InvalidId):
            ids.parse(bad)


def test_amfi_quote_by_isin_and_by_code_from_the_official_file():
    src = amfi()
    q = src.get_quote(Inst("INF000A01AA1"))
    assert (q.nav, q.date, q.currency, q.price_type) == (216.123, "2026-10-27", "INR", "nav")
    assert src.get_quote(Inst("INF000A01AC7")).nav == 76.741          # second ISIN of a class
    assert src.get_quote(Inst("in-amfi:100004")).date == "2026-10-24"
    assert src.get_quote(Inst("in-amfi:100003")) is None               # NAV "N.A."
    assert src.get_quote(Inst("INF999999999")) is None
    assert sum("NAVAll" in r.url.path for r in src.calls) == 1        # downloaded once, then reused


def test_amfi_only_claims_indian_isins():
    src = amfi()
    assert src.supports(Inst("INF000A01AA1")) and src.supports(Inst("in-amfi:100001"))
    assert not src.supports(Inst("IE00B4L5Y983")) and not src.supports(Inst("INF000A01AA1", ETF))


def test_amfi_history_from_the_mirror_trimmed_and_sorted():
    h = amfi().get_history(Inst("INF000A01AA1"), "2026-10-01", "2026-10-31")
    assert [(e.date, e.nav) for e in h.entries] == [("2026-10-23", 213.423), ("2026-10-26", 214.641)]
    assert h.currency == "INR"


def test_amfi_search_and_resolve_carry_amc_and_category():
    src = amfi()
    hits = src.search("example mid cap direct")
    assert [h["id"] for h in hits] == ["INF000A01AA1"]
    h = hits[0]
    assert (h["name"], h["provider"], h["category"], h["amfi_code"]) == (
        "Example Mid Cap Fund - Direct Plan - Growth Option", "Example Mutual Fund",
        "Equity Scheme - Mid Cap Fund", "100001")
    assert [x["id"] for x in src.search("otra debt")] == ["INF000B01AA0"]     # accent-insensitive
    assert src.search("example closed")[0]["id"] == "in-amfi:100003"        # no ISIN -> scheme id
    assert src.resolve("in-amfi", "100002")["id"] == "INF000A01AB9"
    assert src.resolve("isin", "IE00B4L5Y983") is None


NPS_LATEST = {"data": [
    {"PFM Code": "PFM001", "PFM Name": "EXAMPLE PENSION FUNDS", "Scheme Code": "SM001001",
     "Scheme Name": "EXAMPLE PENSION FUND SCHEME - CENTRAL GOVT", "NAV": "49.3562", "Last Updated": "26-10-2026"},
    {"PFM Code": "PFM002", "PFM Name": "OTHER PENSION", "Scheme Code": "SM002003",
     "Scheme Name": "OTHER SCHEME E - TIER I", "NAV": "61.1", "Last Updated": "26-10-2026"},
]}
NPS_HIST = {"data": [{"date": "26-10-2026", "nav": 49.3562}, {"date": "23-10-2026", "nav": 49.2437},
                     {"date": "01-01-2020", "nav": 30.0}]}


def nps():
    return mock(NPSSource, [(path("/api/latest"), httpx.Response(200, json=NPS_LATEST)),
                            (path("/api/historical/SM001001"), httpx.Response(200, json=NPS_HIST))])


def test_nps_quote_history_search():
    src = nps()
    q = src.get_quote(Inst("in-nps:SM001001", "plan"))
    assert (q.nav, q.date, q.currency) == (49.3562, "2026-10-26", "INR")
    assert src.get_quote(Inst("in-nps:SM009999", "plan")) is None
    h = src.get_history(Inst("in-nps:SM001001", "plan"), "2026-10-01", "2026-10-31")
    assert [e.date for e in h.entries] == ["2026-10-23", "2026-10-26"]
    hit = src.search("central govt")[0]
    assert (hit["id"], hit["type"], hit["provider"]) == ("in-nps:SM001001", "plan", "EXAMPLE PENSION FUNDS")
    assert src.resolve("in-nps", "SM002003")["name"] == "OTHER SCHEME E - TIER I"
    assert src.supports(Inst("in-nps:SM001001")) and not src.supports(Inst("INF000A01AA1"))


def ms_china(y, m, d):
    return int(datetime(y, m, d, tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)


EM_SEARCH = {"ErrCode": 0, "Datas": [
    {"CODE": "600000", "NAME": "A stock", "CATEGORY": 10},
    {"CODE": "000001", "NAME": "示例消费股票", "CATEGORY": 700,
     "FundBaseInfo": {"DWJZ": 2.79, "FSRQ": "2026-10-27", "JJGS": "示例基金", "FTYPE": "股票型"}},
]}
EM_JS = ('﻿var fS_name = "示例消费股票";var fS_code = "000001";'
         f'var Data_netWorthTrend = {json.dumps([{"x": ms_china(2026, 10, d), "y": 2.7 + d / 100} for d in (23, 26, 27)])};'
         'var Data_ACWorthTrend = [[1,1.0]];')


def eastmoney(search=EM_SEARCH):
    return mock(EastmoneySource, [(path("FundSearchAPI"), httpx.Response(200, json=search)),
                                  (path("/pingzhongdata/000001.js"), httpx.Response(200, content=EM_JS.encode()))])


def test_eastmoney_quote_from_search_info_and_resolve():
    src = eastmoney()
    q = src.get_quote(Inst("cn:000001"))
    assert (q.nav, q.date, q.currency) == (2.79, "2026-10-27", "CNY")
    hit = src.resolve("cn", "000001")
    assert (hit["id"], hit["name"], hit["provider"], hit["category"]) == ("cn:000001", "示例消费股票", "示例基金", "股票型")
    assert [h["id"] for h in src.search("示例")] == ["cn:000001"]       # stocks filtered out


def test_eastmoney_history_dates_are_china_calendar_days():
    h = eastmoney().get_history(Inst("cn:000001"), "2026-10-24", "2026-10-31")
    assert [(e.date, e.nav) for e in h.entries] == [("2026-10-26", 2.96), ("2026-10-27", 2.97)]


def test_eastmoney_quote_falls_back_to_history_when_search_has_no_nav():
    no_nav = {"Datas": [{"CODE": "000001", "NAME": "x", "CATEGORY": 700, "FundBaseInfo": {"DWJZ": None}}]}
    q = eastmoney(no_nav).get_quote(Inst("cn:000001", FUND))
    assert (q.nav, q.date) == (2.97, "2026-10-27")


MMF_SEARCH = {"Datas": [{"CODE": "000009", "NAME": "示例货币B", "CATEGORY": 700,
                         "FundBaseInfo": {"DWJZ": 0.2788, "FSRQ": "2026-10-27", "FTYPE": "货币型-普通货币"}}]}
MMF_JS = ('var fS_name = "示例货币B";var ishb=true;'
          f'var Data_millionCopiesIncome = {json.dumps([[ms_china(2026, 10, d), 0.5] for d in (23, 26, 27)])};'
          'var Data_sevenDaysYearIncome = [[1,1.8]];')


def test_eastmoney_money_market_funds_give_an_income_index_never_the_income_as_nav():
    src = mock(EastmoneySource, [(path("FundSearchAPI"), httpx.Response(200, json=MMF_SEARCH)),
                                 (path("/pingzhongdata/000009.js"),
                                  lambda _: httpx.Response(200, content=MMF_JS.encode()))])
    q = src.get_quote(Inst("cn:000009", FUND))
    # 0.5 per 10,000 units a day, compounded three times from 1.0.
    assert (q.nav, q.date, q.price_type) == (round(1.00005 ** 3, 8), "2026-10-27", "index")
    h = src.get_history(Inst("cn:000009", FUND), "2026-10-26", "2026-10-31")
    assert h.price_type == "index" and [e.nav for e in h.entries] == [round(1.00005 ** 2, 8), round(1.00005 ** 3, 8)]
