import time
from datetime import date

import httpx
import pytest

from fundly import Client, InvalidId, NotFound, UnknownSource
from fundly.sources.base import (
    ETF, FUND, PLAN, USER_AGENT, HistoryResult, NavEntry, QuoteResult, RateLimited, Source, in_range,
)
import itertools

ETF_ID = "IE00B4L5Y983"
FUND_ID = "ES0173311103"
PLAN_ID = "N5508"


class Fake(Source):
    def __init__(self, name, types=(FUND, ETF), quote=None, history=None, hits=None, fail=False):
        super().__init__(min_interval=0)
        self.name, self.types = name, frozenset(types)
        self.schemes = frozenset({"isin", "dgs", "ticker", "ms"})
        self.search_scope = "global"
        self._quote, self._history, self._hits, self._fail = quote, history, hits or [], fail
        self.calls = {"quote": 0, "history": 0, "search": 0}

    def get_quote(self, inst):
        self.calls["quote"] += 1
        if self._fail:
            raise RuntimeError("down")
        return self._quote

    def get_history(self, inst, start, end):
        self.calls["history"] += 1
        if not self._history:
            return None
        return HistoryResult(entries=in_range(self._history, start, end), currency="EUR", source=self.name)

    def search(self, q):
        self.calls["search"] += 1
        return self._hits


def q(price, d="2026-10-24", ccy="EUR", src="morningstar", pt="nav"):
    return QuoteResult(price, d, ccy, src, pt)


SEED = [
    {"id": ETF_ID, "name": "iShares Core MSCI World UCITS ETF", "type": ETF, "currency": "USD", "size_eur": 100},
    {"id": FUND_ID, "name": "Renta 4 Multigestión Num. Patr. Glb FI",
     "alt_name": "Renta 4 Multigestión Numantia Patrimonio Global FI", "type": FUND, "currency": "EUR", "size_eur": 50},
]


def client(*sources, seed=True) -> Client:
    c = Client(cache=False, sources=list(sources))
    if seed:
        c._cache.upsert_instruments(SEED)
    return c


def test_cache_goes_to_the_given_directory(tmp_path):
    with Client(cache=tmp_path, sources=[]) as c:
        assert c.search(live=False) == []
    assert (tmp_path / "fundly.db").exists()


def test_search_matches_words_in_any_order_without_accents():
    r = client().search("patrimonio numantia multigestion", live=False)
    assert [x.id for x in r] == [FUND_ID]


def test_search_exact_id_first_and_type_filter():
    c = client()
    assert c.search(ETF_ID.lower(), live=False)[0].id == ETF_ID
    assert [x.id for x in c.search(type="etf", live=False)] == [ETF_ID]


def test_live_search_remembers_plans_and_is_cached_per_day():
    finect = Fake("finect", types=(FUND, ETF, PLAN), hits=[
        {"id": PLAN_ID, "name": "Santander Autónomos RVM PPES", "type": PLAN, "currency": "EUR",
         "provider": "Santander Pensiones EGFP", "finect_id": "5f44a951"},
    ])
    c = client(finect)
    r = c.search("santander autonomos")[0]
    assert (r.id, r.id_type, r.type, r.provider, r.domicile) == (PLAN_ID, "dgs", "plan", "Santander Pensiones EGFP", "ES")
    c.search("Santander  Autónomos")
    assert finect.calls["search"] == 1
    assert c.instrument(PLAN_ID).name == "Santander Autónomos RVM PPES"


def test_live_search_only_fills_missing_fields():
    c = client(Fake("finect", hits=[{"id": FUND_ID, "name": "Other name", "type": FUND,
                                      "provider": "Renta 4 Gestora", "currency": "USD"}]))
    r = c.search("numantia")[0]
    assert r.name.startswith("Renta 4") and r.currency == "EUR" and r.provider == "Renta 4 Gestora"


def test_invalid_id_and_unknown_source():
    c = client(Fake("morningstar", quote=q(1)))
    for bad in ("", "!!", "ES0112611001-X", "nope:ABC", "isin:ES011", "dgs:5508"):
        with pytest.raises(InvalidId):
            c.quote(bad)
    with pytest.raises(UnknownSource):
        c.quote(FUND_ID, source="bogus")


def test_plan_only_goes_to_plan_sources():
    ms = Fake("morningstar", quote=q(1))
    c = client(ms, Fake("finect", types=(FUND, ETF, PLAN), quote=q(133.5, src="finect")))
    r = c.quote(PLAN_ID)
    assert (r.source, r.price, r.date) == ("finect", 133.5, date(2026, 10, 24))
    assert ms.calls["quote"] == 0


def test_quote_falls_back_and_caches():
    finect = Fake("finect", quote=q(29.36, src="finect"))
    c = client(Fake("morningstar", fail=True), finect)
    assert c.quote(FUND_ID).source == "finect"
    assert c.quote(FUND_ID).cached is True
    assert finect.calls["quote"] == 1


def test_not_found_when_no_source_answers():
    with pytest.raises(NotFound):
        client(Fake("morningstar"), Fake("yahoo")).quote(FUND_ID)


def test_etf_prefers_official_nav_over_exchange_close():
    c = client(Fake("morningstar", quote=q(145.95, ccy="USD", pt="market")),
               Fake("finect", quote=q(145.74, ccy="USD", src="finect")))
    r = c.quote(ETF_ID)
    assert (r.source, r.price_type) == ("finect", "nav")


def test_unknown_isin_is_resolved_so_etf_gets_etf_source_order():
    unknown = "IE00BK5BQT80"
    c = client(
        Fake("morningstar", quote=q(150.1, ccy="USD", pt="market"),
             hits=[{"id": unknown, "name": "Vanguard FTSE All-World", "type": ETF, "currency": "USD"}]),
        Fake("finect", types=(FUND, ETF, PLAN), quote=q(149.9, ccy="USD", src="finect")),
    )
    r = c.quote(unknown)
    assert (r.source, r.name) == ("finect", "Vanguard FTSE All-World")


def test_forced_source_does_not_replace_default_answer():
    ms = Fake("morningstar", quote=q(29.36))
    c = client(ms, Fake("yahoo", quote=q(29.1, src="yahoo")))
    c.quote(FUND_ID)
    assert c.quote(FUND_ID, source="yahoo").source == "yahoo"
    r = c.quote(FUND_ID)
    assert (r.source, r.cached, ms.calls["quote"]) == ("morningstar", True, 1)


def test_compare_reports_spread_and_missing():
    c = client(Fake("morningstar", quote=q(29.3605)), Fake("finect", quote=q(29.36, src="finect")),
               Fake("ft", quote=q(29.10, d="2026-10-21", src="ft")), Fake("yahoo"), Fake("quefondos", fail=True))
    r = c.compare(FUND_ID)
    assert {x.source for x in r.quotes} == {"morningstar", "finect", "ft"}
    assert r.missing == {"yahoo": "not found", "quefondos": "error: RuntimeError"}
    assert r.nav_spread_pct == pytest.approx(0.0017, abs=1e-4)


HIST = [NavEntry(f"2026-10-{d:02d}", 100 + d) for d in range(1, 27)]


def test_history_cache_only_covers_fetched_range():
    ms = Fake("morningstar", history=HIST)
    c = client(ms)
    h = c.history(FUND_ID, "2026-10-10", "2026-10-20")
    assert len(h.points) == 11 and h.cached is False and h.points[0].date == date(2026, 10, 10)
    h = c.history(FUND_ID, date(2026, 10, 12), date(2026, 10, 15))
    assert len(h.points) == 4 and h.cached is True and ms.calls["history"] == 1
    h = c.history(FUND_ID, "2026-10-01", "2026-10-26")
    assert len(h.points) == 26 and h.cached is False and ms.calls["history"] == 2


def test_history_rejects_inverted_range():
    with pytest.raises(ValueError):
        client(Fake("morningstar", history=HIST)).history(FUND_ID, "2026-10-20", "2026-10-01")


def test_history_to_pandas():
    pd = pytest.importorskip("pandas")
    s = client(Fake("morningstar", history=HIST)).history(FUND_ID, "2026-10-01", "2026-10-03").to_pandas()
    assert list(s) == [101, 102, 103] and isinstance(s.index, pd.DatetimeIndex)


def test_requests_are_rate_limited_and_identify_the_library():
    seen = []

    def handler(request):
        seen.append((time.perf_counter(), request.headers["user-agent"]))
        return httpx.Response(200, json={})

    src = Source(min_interval=0.2)
    src.http = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": USER_AGENT})
    for _ in range(3):
        src._get("https://example.org/")
    gaps = [b[0] - a[0] for a, b in itertools.pairwise(seen)]
    assert all(g >= 0.19 for g in gaps)
    assert all(ua.startswith("fundly/") for _, ua in seen)


def test_429_pauses_the_source_without_more_requests():
    hits = []

    def handler(request):
        hits.append(request.url)
        return httpx.Response(429, headers={"Retry-After": "30"})

    class Example(Source):
        name = "example"

    src = Example(min_interval=0)
    src.http = httpx.Client(transport=httpx.MockTransport(handler))
    for _ in range(3):
        with pytest.raises(RateLimited):
            src._get("https://example.org/")
    assert len(hits) == 1


def test_rate_limited_source_falls_back_to_the_next_one():
    class Limited(Fake):
        def get_quote(self, inst):
            self.calls["quote"] += 1
            raise RateLimited("429")

    limited = Limited("finect", types=(FUND, ETF, PLAN))
    c = client(limited, Fake("morningstar", quote=q(145.95, ccy="USD", pt="market")))
    assert c.quote(ETF_ID).source == "morningstar"
