from datetime import date, datetime, timezone

import httpx
import pytest

import fundly
from fundly import Client, FXUnavailable
from fundly.currency import to_major
from fundly.fx import ECBRates, YahooRates
from fundly.sources.base import HistoryResult, NavEntry, Source, in_range
from fundly.sources.yahoo import day_of
from test_client import ETF_ID, FUND_ID, Fake, q
from test_sources import mock, path

# Units per euro. 2026-10-24/25 is a weekend: no rate published.
RATES = {
    "USD": {"2026-10-22": 1.10, "2026-10-23": 1.12, "2026-10-26": 1.15},
    "INR": {"2026-10-22": 100.0, "2026-10-23": 101.0, "2026-10-26": 103.5},
}


class FakeRates(Source):
    name = "fake-fx"
    types = schemes = frozenset()

    def __init__(self, known=RATES):
        super().__init__(min_interval=0)
        self.known, self.calls = known, 0

    def covers(self, cur):
        return cur in self.known

    def series(self, cur, start, end):
        self.calls += 1
        return sorted((d, v) for d, v in self.known[cur].items() if start <= d <= end)


def fx_client(*sources, rates=None):
    rates = rates or FakeRates()
    c = Client(cache=False, sources=list(sources), fx_providers=[rates])
    c._cache.upsert_instruments([{"id": FUND_ID, "name": "Fund", "type": "fund", "currency": "EUR"},
                                 {"id": ETF_ID, "name": "ETF", "type": "etf", "currency": "USD"}])
    return c, rates


@pytest.mark.parametrize("price, ccy, expected", [
    (4631.5, "GBp", (46.315, "GBP")), (4631.5, "GBX", (46.315, "GBP")), (20167.18, "ZAc", (201.6718, "ZAR")),
    (10827.0, "ILA", (108.27, "ILS")), (12.5, "EUR", (12.5, "EUR")), (1.0, None, (1.0, None)),
])
def test_minor_units_become_major_currency(price, ccy, expected):
    p, c = to_major(price, ccy)
    assert (round(p, 6), c) == expected


def test_pence_from_a_source_are_returned_and_cached_in_pounds():
    ft = Fake("ft", quote=q(4631.5, ccy="GBp", src="ft"),
              history=[NavEntry("2026-10-22", 4600.0), NavEntry("2026-10-23", 4631.5)])
    ft.get_history = lambda inst, s, e: HistoryResult(entries=in_range(ft._history, s, e), currency="GBp", source="ft")
    c, _ = fx_client(ft)
    first, again = c.quote(ETF_ID), c.quote(ETF_ID)
    assert (first.price, first.currency) == (46.315, "GBP")
    assert (again.price, again.currency, again.cached) == (46.315, "GBP", True)
    h = c.history(ETF_ID, "2026-10-01", "2026-10-31")
    assert h.currency == "GBP" and [p.price for p in h.points] == [46.0, 46.315]


def test_convert_quote_keeps_the_original_and_the_rate():
    c, _ = fx_client(Fake("morningstar", quote=q(100.0, d="2026-10-23")))
    r = c.quote(FUND_ID, currency="usd")
    assert (r.price, r.currency, r.original_price, r.original_currency) == (pytest.approx(112.0), "USD", 100.0, "EUR")
    assert (r.fx_rate, r.fx_date, r.fx_source) == (1.12, date(2026, 10, 23), "fake-fx")


def test_cross_rate_goes_through_the_euro():
    c, _ = fx_client(Fake("morningstar", quote=q(1.12, d="2026-10-23", ccy="USD")))
    r = c.quote(ETF_ID, currency="INR")
    assert r.price == pytest.approx(101.0) and r.fx_rate == pytest.approx(101.0 / 1.12)


def test_weekend_price_uses_the_last_published_rate():
    c, _ = fx_client(Fake("morningstar", quote=q(100.0, d="2026-10-25")))
    r = c.quote(FUND_ID, currency="USD")
    assert (r.fx_rate, r.fx_date) == (1.12, date(2026, 10, 23))


def test_same_currency_is_untouched():
    c, rates = fx_client(Fake("morningstar", quote=q(100.0)))
    r = c.quote(FUND_ID, currency="EUR")
    assert (r.price, r.original_price, r.fx_rate) == (100.0, None, None) and rates.calls == 0


def test_bad_or_unknown_currency():
    c, _ = fx_client(Fake("morningstar", quote=q(100.0, d="2026-10-23")))
    with pytest.raises(ValueError):
        c.quote(FUND_ID, currency="euros")
    with pytest.raises(FXUnavailable):
        c.quote(FUND_ID, currency="XYZ")


def test_no_rate_before_the_series_starts():
    c, _ = fx_client(Fake("morningstar", quote=q(100.0, d="2026-10-01")))
    with pytest.raises(FXUnavailable):
        c.quote(FUND_ID, currency="USD")


def test_history_converts_each_day_with_its_own_rate():
    hist = [NavEntry(d, 100.0) for d in ("2026-10-22", "2026-10-23", "2026-10-26")]
    c, _ = fx_client(Fake("morningstar", history=hist))
    h = c.history(FUND_ID, "2026-10-01", "2026-10-31", currency="USD")
    assert [round(p.price, 6) for p in h.points] == [110.0, 112.0, 115.0]
    assert (h.currency, h.original_currency, h.fx_source) == ("USD", "EUR", "fake-fx")


def test_rates_are_cached():
    c, rates = fx_client(Fake("morningstar", quote=q(100.0, d="2026-10-23")))
    c.quote(FUND_ID, currency="USD")
    c.quote(FUND_ID, currency="USD")
    assert rates.calls == 1


def test_fallback_provider_for_currencies_the_first_does_not_have():
    clp = FakeRates({"CLP": {"2026-10-23": 1074.0}})
    c = Client(cache=False, sources=[Fake("morningstar", quote=q(100.0, d="2026-10-23"))],
               fx_providers=[FakeRates(), clp])
    c._cache.upsert_instruments([{"id": FUND_ID, "name": "Fund", "type": "fund", "currency": "EUR"}])
    r = c.quote(FUND_ID, currency="CLP")
    assert r.price == pytest.approx(107400.0) and clp.calls == 1


def test_excluding_yahoo_also_drops_it_for_exchange_rates():
    names = lambda c: [type(p).__name__ for p in c._fx_providers]  # noqa: E731
    assert names(Client(cache=False, sources=[Fake("yahoo")])) == ["ECBRates", "YahooRates"]
    assert names(Client(cache=False, sources=[Fake("yahoo")], exclude=["yahoo"])) == ["ECBRates"]


def test_module_functions_pass_currency(monkeypatch):
    c, _ = fx_client(Fake("morningstar", quote=q(100.0, d="2026-10-23"),
                          history=[NavEntry("2026-10-23", 100.0)]))
    monkeypatch.setattr(fundly, "_default", c)
    assert fundly.quote(FUND_ID, currency="USD").currency == "USD"
    assert fundly.history(FUND_ID, "2026-10-01", "2026-10-31", currency="USD").points[0].price == pytest.approx(112.0)


ECB_CSV = ("KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\n"
           "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-10-22,1.1205\n"
           "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-10-23,\n"
           "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-10-26,1.1188\n")


def test_ecb_parser_and_request():
    src = mock(ECBRates, [(path("/EXR/D.USD.EUR.SP00.A"), httpx.Response(200, text=ECB_CSV))])
    assert src.series("USD", "2026-10-20", "2026-10-27") == [("2026-10-22", 1.1205), ("2026-10-26", 1.1188)]
    call = src.calls[0]
    assert (call.url.params["format"], call.url.params["startPeriod"]) == ("csvdata", "2026-10-20")
    assert src.covers("USD") and not src.covers("CLP")


def ts_utc(y, m, d, h):
    return int(datetime(y, m, d, h, tzinfo=timezone.utc).timestamp())


def test_yahoo_rates_use_the_exchange_calendar_day_across_dst():
    # London midnight is 23:00 UTC the day before while summer time is on (until 2026-10-25).
    chart = {"chart": {"result": [{"meta": {"currency": "CLP", "exchangeTimezoneName": "Europe/London", "gmtoffset": 0},
                                   "timestamp": [ts_utc(2026, 10, 22, 23), ts_utc(2026, 10, 26, 0)],
                                   "indicators": {"quote": [{"close": [1096.1, 1100.2]}]}}]}}
    src = mock(YahooRates, [(path("/EURCLP=X"), httpx.Response(200, json=chart))])
    assert src.series("CLP", "2026-10-20", "2026-10-27") == [("2026-10-23", 1096.1), ("2026-10-26", 1100.2)]
    wrong_ccy = {"chart": {"result": [{**chart["chart"]["result"][0], "meta": {"currency": "USD"}}]}}
    wrong = mock(YahooRates, [(path("/EURCLP=X"), httpx.Response(200, json=wrong_ccy))])
    assert wrong.series("CLP", "2026-10-20", "2026-10-27") == []


def test_day_of_falls_back_to_the_offset_without_a_time_zone_name():
    assert day_of(ts_utc(2026, 10, 22, 23), {"gmtoffset": 3600}) == "2026-10-23"
    assert day_of(ts_utc(2026, 10, 22, 23), {}) == "2026-10-22"
