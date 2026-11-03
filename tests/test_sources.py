"""Each source's parsing against hand-made responses shaped like the real ones (no real data in the repo)."""
from datetime import datetime, timezone

import httpx
import pytest

from fundly.sources.base import ETF, FUND, PLAN, Inst
from fundly.sources.comparadorfondos import ComparadorFondosSource
from fundly.sources.finect import FinectSource
from fundly.sources.ft import FTSource
from fundly.sources.morningstar import MorningstarSource
from fundly.sources.quefondos import QuefondosSource
from fundly.sources.yahoo import YahooSource


def mock(source_cls, routes, **init):
    """routes: list of (predicate(request) -> bool, response or callable(request) -> response)."""
    calls = []

    def handler(request: httpx.Request):
        calls.append(request)
        for match, resp in routes:
            if match(request):
                return resp(request) if callable(resp) else resp
        return httpx.Response(404)

    src = source_cls(min_interval=0, **init)
    src.http = httpx.Client(transport=httpx.MockTransport(handler), headers=dict(src.http.headers))
    src.calls = calls
    return src


def path(fragment):
    return lambda r: fragment in r.url.path


def param(name, value):
    return lambda r: r.url.params.get(name) == value


def ts(day):
    return int(datetime(2026, 10, day, tzinfo=timezone.utc).timestamp())


# --- Morningstar ----------------------------------------------------------------------------------

MS_ROW = {"SecId": "0P0000TEST", "Name": "Test World Fund", "ISIN": "IE0000000001", "ClosePrice": 145.95,
          "ClosePriceDate": "2026-10-26T00:00:00", "PriceCurrency": "USD", "CategoryName": "RV Global",
          "FundTNAV": 1_000_000.0, "OngoingCharge": 0.2, "Universe": "FOALL$$ALL", "Ticker": "TWF"}
MS_ETF_ROW = {**MS_ROW, "ISIN": "IE0000000009", "Name": "Test ETF", "SecId": "0P0000ETF1", "Universe": "ETALL$$ALL"}
MS_SERIES = {"TimeSeries": {"Security": [{"HistoryDetail": [
    {"EndDate": "2026-09-30", "Value": "139.0"}, {"EndDate": "2026-10-01", "Value": "140.5"},
    {"EndDate": "2026-10-02", "Value": "141.25"}, {"EndDate": "2026-10-03", "Value": None},
]}]}}


def ms_screener(request):
    assert request.url.params["universeIds"] == "FOALL$$ALL|ETALL$$ALL"
    rows = {"ISIN:EQ:IE0000000001": [MS_ROW], "Ticker:EQ:TWF": [MS_ROW], "SecId:EQ:0P0000TEST": [MS_ROW],
            "ISIN:EQ:IE0000000009": [MS_ETF_ROW]}.get(request.url.params.get("filters"), [])
    return httpx.Response(200, json={"rows": rows})


def test_morningstar_quote_fund_is_nav_etf_is_market():
    src = mock(MorningstarSource, [(path("screener"), ms_screener)])
    q = src.get_quote(Inst("IE0000000001"))
    assert (q.nav, q.date, q.currency, q.price_type) == (145.95, "2026-10-26", "USD", "nav")
    # The worldwide ETF universe tells ETFs apart: their price is an exchange close.
    assert src.get_quote(Inst("IE0000000009")).price_type == "market"
    assert src.get_quote(Inst("IE0000000002", FUND)) is None


def test_morningstar_looks_up_tickers_and_its_own_ids_and_resolves_them_to_isin():
    src = mock(MorningstarSource, [(path("screener"), ms_screener)])
    assert src.get_quote(Inst("ticker:TWF")).nav == 145.95
    assert src.get_quote(Inst("ms:0P0000TEST")).nav == 145.95
    hit = src.resolve("ticker", "TWF")
    assert (hit["id"], hit["ticker"], hit["morningstar_id"], hit["type"]) == ("IE0000000001", "TWF", "0P0000TEST", "fund")
    assert src.resolve("ticker", "NOPE") is None


def test_morningstar_history_asks_in_the_class_currency_and_trims_range():
    src = mock(MorningstarSource, [(path("screener"), ms_screener),
                                   (path("timeseries_price"), httpx.Response(200, json=MS_SERIES))])
    h = src.get_history(Inst("IE0000000001", FUND), "2026-10-01", "2026-10-31")
    series_call = next(r for r in src.calls if "timeseries_price" in r.url.path)
    # Without an explicit currencyId the real endpoint answers in an arbitrary currency.
    assert series_call.url.params["currencyId"] == "USD"
    # The plain SecId returns nothing outside Europe; the universe-qualified form works everywhere.
    assert series_call.url.params["id"] == "0P0000TEST]2]0]FOALL$$ALL"
    assert [(e.date, e.nav) for e in h.entries] == [("2026-10-01", 140.5), ("2026-10-02", 141.25)]
    assert h.currency == "USD"


def test_morningstar_history_none_when_series_missing():
    src = mock(MorningstarSource, [(path("screener"), ms_screener), (path("timeseries_price"), httpx.Response(404))])
    assert src.get_history(Inst("IE0000000001", FUND), "2026-10-01", "2026-10-31") is None


def test_morningstar_search_is_worldwide_types_by_universe_and_dedupes():
    no_isin = {**MS_ROW, "ISIN": None, "SecId": "F00001H9R0", "Name": "Korean fund"}
    screener = httpx.Response(200, json={"rows": [MS_ROW, MS_ETF_ROW, MS_ROW, no_isin]})
    hits = mock(MorningstarSource, [(path("screener"), screener)]).search("test")
    assert [(h["id"], h["type"]) for h in hits] == [
        ("IE0000000001", "fund"), ("IE0000000009", "etf"), ("ms:F00001H9R0", "fund")]
    assert hits[0]["size_eur"] == 1_000_000 and hits[0]["morningstar_id"] == "0P0000TEST"


# --- Finect ---------------------------------------------------------------------------------------

FINECT_FUND = {"data": {"id": "abc123", "currency": {"code": "EUR"},
                        "lastQuote": {"datetime": "2026-10-22T00:00:00.000Z", "price": 365.68544}}}
FINECT_SERIES = {"data": [{"datetime": f"2026-10-{d:02d}T00:00:00.000Z", "price": 100 + d} for d in (1, 2, 5, 6)]
                 + [{"datetime": "2026-10-07T00:00:00.000Z", "price": None}]}
FINECT_SEARCH = {"data": [
    {"type": "article", "title": "Noticia", "entity": {}},
    {"type": "plan", "title": "Otro plan", "entity": {"id": "zzz", "isin": "N5507", "currency": "EUR"}},
    {"type": "plan", "title": "Santander Autónomos RVM PPES",
     "entity": {"id": "5f44a951", "isin": "N5508", "fund_class_name": "Santander Autónomos RVM PPES",
                "management_organization": "Santander Pensiones EGFP", "currency": "EUR"}},
    {"type": "fund", "title": "Short name", "entity": {"id": "f1", "isin": "ES0000000001",
                                                        "fund_class_name": "Long Fund Name FI"}},
]}


def test_finect_fund_quote_from_product_page():
    src = mock(FinectSource, [(path("/funds/ES0000000001"), httpx.Response(200, json=FINECT_FUND))])
    q = src.get_quote(Inst("ES0000000001", FUND))
    assert (q.nav, q.date, q.currency, q.price_type) == (365.68544, "2026-10-22", "EUR", "nav")
    assert src.get_quote(Inst("ES0000000002", FUND)) is None


def test_finect_fund_history_resolves_id_once_and_trims():
    src = mock(FinectSource, [(path("/funds/ES0000000001"), httpx.Response(200, json=FINECT_FUND)),
                              (path("/funds/abc123/timeseries"), httpx.Response(200, json=FINECT_SERIES))])
    inst = Inst("ES0000000001", FUND)
    h = src.get_history(inst, "2026-10-02", "2026-10-06")
    assert [(e.date, e.nav) for e in h.entries] == [("2026-10-02", 102), ("2026-10-05", 105), ("2026-10-06", 106)]
    src.get_history(inst, "2026-10-01", "2026-10-06")
    assert sum("/funds/ES0000000001" in r.url.path for r in src.calls) == 1


def test_finect_plan_resolved_through_search_by_exact_dgs_code():
    src = mock(FinectSource, [(path("/search"), httpx.Response(200, json=FINECT_SEARCH)),
                              (path("/plans/5f44a951/timeseries"), httpx.Response(200, json=FINECT_SERIES))])
    q = src.get_quote(Inst("N5508", PLAN))
    assert (q.nav, q.date, q.currency) == (106, "2026-10-06", "EUR")
    assert not any("/plans/zzz" in r.url.path for r in src.calls)


def test_finect_search_maps_funds_and_plans_and_skips_the_rest():
    hits = mock(FinectSource, [(path("/search"), httpx.Response(200, json=FINECT_SEARCH))]).search("x")
    assert [(h["id"], h["type"]) for h in hits] == [("N5507", "plan"), ("N5508", "plan"), ("ES0000000001", "fund")]
    plan = hits[1]
    assert plan["provider"] == "Santander Pensiones EGFP" and plan["finect_id"] == "5f44a951"
    assert hits[2]["name"] == "Long Fund Name FI" and hits[2]["alt_name"] == "Short name"


def test_finect_key_can_be_overridden(monkeypatch):
    monkeypatch.setenv("FUNDLY_FINECT_KEY", "my-key")
    src = mock(FinectSource, [(path("/search"), httpx.Response(200, json={"data": []}))])
    src.search("x")
    assert src.calls[0].headers["key"] == "my-key"


# --- FT -------------------------------------------------------------------------------------------

def ft_series(*values):
    return {"Dates": [f"2026-10-{d:02d}T00:00:00" for d in range(20, 20 + len(values))],
            "Elements": [{"Currency": "EUR", "ExchangeId": "LIP",
                          "ComponentSeries": [{"Type": "Open", "Values": list(values)},
                                              {"Type": "Close", "Values": list(values)}]}]}


def test_ft_fund_only_trusts_symbols_with_the_exact_isin():
    search = {"data": {"security": [{"symbol": "ES0999999999:EUR", "xid": "1"},
                                    {"symbol": "ES0000000001:EUR", "xid": "2"}]}}
    src = mock(FTSource, [(path("searchsecurities"), httpx.Response(200, json=search)),
                          (lambda r: r.url.path.endswith("/series") and b'"Symbol":"2"' in r.content.replace(b" ", b""),
                           httpx.Response(200, json=ft_series(29.1, 29.36)))])
    q = src.get_quote(Inst("ES0000000001", FUND, currency="EUR"))
    assert (q.nav, q.date, q.price_type) == (29.36, "2026-10-21", "nav")


def test_ft_fund_fuzzy_match_is_rejected():
    search = {"data": {"security": [{"symbol": "ES0999999999:EUR", "xid": "1"}]}}
    src = mock(FTSource, [(path("searchsecurities"), httpx.Response(200, json=search))])
    assert src.get_quote(Inst("ES0000000001", FUND)) is None


def test_ft_etf_uses_listing_in_class_currency_as_market_price():
    search = {"data": {"security": [{"symbol": "SWDA:LSE:GBX", "xid": "1", "isPrimary": True},
                                    {"symbol": "IWDA:AEX:USD", "xid": "2"}]}}
    src = mock(FTSource, [(path("searchsecurities"), httpx.Response(200, json=search)),
                          (lambda r: b'"2"' in r.content, httpx.Response(200, json=ft_series(146.63)))])
    q = src.get_quote(Inst("IE0000000001", ETF, currency="USD"))
    assert (q.nav, q.price_type) == (146.63, "market")


# --- comparadorfondos -----------------------------------------------------------------------------

def rsc(prices):
    return "[" + ",".join(f'{{\\"date\\":\\"{d}T00:00:00Z\\",\\"performance\\":{p}}}' for d, p in prices) + "]"


def test_comparadorfondos_takes_the_first_prices_array_not_similar_plans():
    target = [("2026-10-22", 133.1), ("2026-10-23", 133.50458)]
    similar = [(f"2026-09-{d:02d}", 10 + d) for d in range(1, 29)]
    html = (f'<script>self.__next_f.push([1,"{{\\"currency\\":\\"EUR\\",\\"prices\\":{rsc(target)},'
            f'\\"similar\\":[{{\\"prices\\":{rsc(similar)}}}]}}"])</script>')
    src = mock(ComparadorFondosSource, [(path("/plans/N5508"), httpx.Response(200, text=html))])
    q = src.get_quote(Inst("N5508", PLAN))
    assert (q.nav, q.date, q.currency) == (133.50458, "2026-10-23", "EUR")
    h = src.get_history(Inst("N5508", PLAN), "2026-10-23", "2026-10-31")
    assert [e.nav for e in h.entries] == [133.50458]


def test_comparadorfondos_missing_plan():
    assert mock(ComparadorFondosSource, []).get_quote(Inst("N0000", PLAN)) is None


# --- Quefondos ------------------------------------------------------------------------------------

def quefondos_page(nav, ccy, day="23/10/2026"):
    return (f'<span class="floatleft">Valor liquidativo: </span><span class="floatright">{nav} {ccy}</span>'
            f'<span class="floatleft">Fecha: </span><span class="floatright">{day}</span>')


def test_quefondos_parses_spanish_number_format():
    src = mock(QuefondosSource, [(path("/fondos/"), httpx.Response(200, text=quefondos_page("1.234,567891", "EUR")))])
    q = src.get_quote(Inst("ES0000000001", FUND, currency="EUR"))
    assert (q.nav, q.date, q.currency) == (1234.567891, "2026-10-23", "EUR")


def test_quefondos_rejects_eur_conversion_of_a_usd_class_but_accepts_plans():
    page = httpx.Response(200, text=quefondos_page("13,414847", "EUR", "22/10/2026"))
    src = mock(QuefondosSource, [(lambda r: True, page)])
    assert src.get_quote(Inst("LU0000000001", FUND, currency="USD")) is None
    q = src.get_quote(Inst("N3634", PLAN))
    assert (q.nav, q.date) == (13.414847, "2026-10-22")
    assert "/planes/" in src.calls[-1].url.path


def test_quefondos_unknown_id_returns_template():
    src = mock(QuefondosSource, [(lambda r: True, httpx.Response(200, text="<h1>{NFONDO}</h1>"))])
    assert src.get_quote(Inst("ES0000000009", FUND, currency="EUR")) is None


# --- Yahoo ----------------------------------------------------------------------------------------

def yahoo_routes(quote_type):
    search = {"quotes": [{"symbol": "TEST.L", "quoteType": quote_type}]}
    chart = {"chart": {"result": [{"meta": {"currency": "USD", "regularMarketPrice": 146.63,
                                            "regularMarketTime": ts(27)},
                                   "timestamp": [ts(1), ts(2), ts(3)],
                                   "indicators": {"quote": [{"close": [140.0, None, 141.1234567]}]}}]}}
    return [(path("/search"), httpx.Response(200, json=search)),
            (path("/chart/TEST.L"), httpx.Response(200, json=chart))]


@pytest.mark.parametrize("quote_type, price_type", [("ETF", "market"), ("MUTUALFUND", "nav")])
def test_yahoo_price_type_follows_the_listing(quote_type, price_type):
    src = mock(YahooSource, yahoo_routes(quote_type))
    q = src.get_quote(Inst("IE0000000001", None))
    assert (q.nav, q.date, q.currency, q.price_type) == (146.63, "2026-10-27", "USD", price_type)


def test_yahoo_history_skips_gaps_and_caches_ticker():
    src = mock(YahooSource, yahoo_routes("ETF"))
    inst = Inst("IE0000000001", ETF)
    h = src.get_history(inst, "2026-10-01", "2026-10-31")
    assert [(e.date, e.nav) for e in h.entries] == [("2026-10-01", 140.0), ("2026-10-03", 141.123457)]
    src.get_quote(inst)
    assert sum("/search" in r.url.path for r in src.calls) == 1


def test_yahoo_no_ticker():
    src = mock(YahooSource, [(path("/search"), httpx.Response(200, json={"quotes": []}))])
    assert src.get_quote(Inst("ES0000000001", FUND)) is None


# --- shared behaviour -----------------------------------------------------------------------------

@pytest.mark.parametrize("cls", [MorningstarSource, FinectSource, FTSource,
                                 ComparadorFondosSource, QuefondosSource, YahooSource])
def test_every_source_identifies_itself(cls):
    src = cls(min_interval=0)
    assert src.http.headers["user-agent"].startswith("fundly/")
    assert "Mozilla" not in src.http.headers["user-agent"]


@pytest.mark.parametrize("cls, inst, expected", [
    (MorningstarSource, Inst("N5508", PLAN), False),
    (ComparadorFondosSource, Inst("ES0000000001", FUND), False),
    (ComparadorFondosSource, Inst("N5508", PLAN), True),
    (FinectSource, Inst("N5508", PLAN), True),
    (FTSource, Inst("IE0000000001", None), True),
    (QuefondosSource, Inst("IE0000000001", ETF), False),
])
def test_supports(cls, inst, expected):
    assert cls(min_interval=0).supports(inst) is expected


# --- Finect key rotation -------------------------------------------------------------------------

def finect_rotating(new_key="NEWKEY1234567890abcdef", bundle_key=None):
    state = {"api_calls": 0, "discovery": 0}

    def handler(request):
        url = str(request.url)
        if url.startswith("https://api.finect.com/"):
            state["api_calls"] += 1
            if request.headers.get("key") != new_key:
                return httpx.Response(401, json={"code": 401, "message": "Authentication required or invalid"})
            return httpx.Response(200, json=FINECT_FUND)
        if "fundly-key-discovery" in url:
            state["discovery"] += 1
            return httpx.Response(404, text='<html><script src="https://www.finect.com/front-products/resources/'
                                             'vendors-abc123.js"></script></html>')
        if url.endswith("vendors-abc123.js"):
            return httpx.Response(200, text=f'var e={{FINECT_API_KEY:"{bundle_key or new_key}",X:1}};')
        return httpx.Response(404)

    src = FinectSource(min_interval=0)
    src.http = httpx.Client(transport=httpx.MockTransport(handler), headers=dict(src.http.headers))
    return src, state


def test_finect_rediscovers_a_rotated_key_and_retries_once():
    src, state = finect_rotating()
    q = src.get_quote(Inst("ES0000000001", FUND))
    assert q.nav == 365.68544 and state["discovery"] == 1 and src.http.headers["key"] == "NEWKEY1234567890abcdef"
    src.get_quote(Inst("ES0000000001", FUND))
    assert state["discovery"] == 1  # the new key keeps working


def test_finect_reads_its_key_from_the_frontend_on_first_use(monkeypatch):
    monkeypatch.delenv("FUNDLY_FINECT_KEY", raising=False)
    src, state = finect_rotating()
    assert "key" not in src.http.headers  # no key ships with fundly
    assert src.get_quote(Inst("ES0000000001", FUND)).nav == 365.68544
    assert (state["discovery"], state["api_calls"]) == (1, 1)  # found before the first API call, no 401


def test_finect_does_not_loop_when_the_frontend_has_no_new_key():
    src, state = finect_rotating(bundle_key="OLDKEY1234567890abcdef")
    src.http.headers["key"] = "OLDKEY1234567890abcdef"
    # A rejected key is an error (so the client falls back and doesn't remember "not found"), not a miss.
    for _ in range(2):
        with pytest.raises(httpx.HTTPStatusError):
            src.get_quote(Inst("ES0000000001", FUND))
    assert state["discovery"] == 1  # at most once an hour
