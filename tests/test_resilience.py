"""Every source against broken websites: the client must answer NotFound (or fall back), never crash or hang."""
import math
import threading

import httpx
import pytest

from fundly import Client, NotFound
from fundly.client import DEFAULT_SOURCES
from fundly.sources.base import QuoteResult, HistoryResult, NavEntry
from test_client import FUND_ID, Fake

# A real-looking id for each source, in a scheme it handles.
IDS = {
    "morningstar": "IE00B4L5Y983", "finect": "N5508", "ft": "ES0112611001", "comparadorfondos": "N5508",
    "quefondos": "ES0112611001", "yahoo": "IE00B4L5Y983", "amfi": "INF179K01XQ0", "npsnav": "in-nps:SM001001",
    "eastmoney": "cn:110022", "cl-afp": "cl-afp:HABITAT-A", "datos-gov-co": "co-pension:10-6000",
    "cvm": "br-cvm:00017024000153", "cafci": "ar-cafci:148", "analizy": "pl-analizy:PZU50",
    "gemelnet": "il-gemel:880", "fmarket": "vn-fmarket:VFF", "pasardana": "IDN000007002",
    "tefas": "tr-tefas:YAC", "uitf": "ph-uitf:69", "smv": "pe-smv:38036-0002",
}


def _raise(exc_type):
    def handler(request):
        raise exc_type("simulated", request=request)
    return handler


BROKEN = {
    "500 html": lambda r: httpx.Response(500, text="<html><body>Internal Server Error</body></html>"),
    "maintenance page": lambda r: httpx.Response(200, text="<html><h1>Down for maintenance</h1></html>"),
    "empty body": lambda r: httpx.Response(200, content=b""),
    "empty json object": lambda r: httpx.Response(200, json={}),
    "empty json list": lambda r: httpx.Response(200, json=[]),
    "null data": lambda r: httpx.Response(200, json={"data": None, "rows": None, "chart": {"result": None}}),
    "truncated json": lambda r: httpx.Response(200, text='{"rows": [{"ISIN": "IE00B4L5Y983", "ClosePr'),
    "binary garbage": lambda r: httpx.Response(200, content=bytes(range(256)) * 40),
    "403": lambda r: httpx.Response(403, text="Forbidden"),
    "404": lambda r: httpx.Response(404, text="Not Found"),
    "429": lambda r: httpx.Response(429, headers={"Retry-After": "1"}),
    "timeout": _raise(httpx.ReadTimeout),
    "connection error": _raise(httpx.ConnectError),
}


def broken_client(source_cls, handler) -> Client:
    src = source_cls(min_interval=0)
    calls = []

    def counted(request):
        calls.append(request.url)
        return handler(request)

    src.http = httpx.Client(transport=httpx.MockTransport(counted), headers=dict(src.http.headers))
    c = Client(cache=False, sources=[src], fx_providers=[])
    c.calls = calls
    return c


def finishes(fn, seconds=10):
    """Run fn in a thread; fail the test if it hangs."""
    out = {}

    def run():
        try:
            out["value"] = fn()
        except BaseException as e:
            out["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), f"hung for more than {seconds}s"
    if "error" in out:
        raise out["error"]
    return out.get("value")


def test_every_default_source_has_a_test_id():
    assert {cls(min_interval=0).name for cls in DEFAULT_SOURCES} == set(IDS)


@pytest.mark.parametrize("kind", list(BROKEN))
@pytest.mark.parametrize("source_cls", DEFAULT_SOURCES, ids=lambda c: c.name)
def test_broken_site_means_not_found_never_a_crash(source_cls, kind):
    c = broken_client(source_cls, BROKEN[kind])
    id = IDS[source_cls.name]
    with pytest.raises(NotFound):
        finishes(lambda: c.quote(id))
    with pytest.raises(NotFound):
        finishes(lambda: c.history(id, "2026-09-01", "2026-10-27"))
    assert isinstance(finishes(lambda: c.search("azvalor internacional")), list)
    cmp = finishes(lambda: c.compare(id))
    assert cmp.quotes == []
    assert c.calls, "the source never reached the (broken) website"


@pytest.mark.parametrize("bad", [
    QuoteResult(float("nan"), "2026-10-24", "EUR", "morningstar"),
    QuoteResult(float("inf"), "2026-10-24", "EUR", "morningstar"),
    QuoteResult(0.0, "2026-10-24", "EUR", "morningstar"),
    QuoteResult(-3.5, "2026-10-24", "EUR", "morningstar"),
    QuoteResult(10.0, "", "EUR", "morningstar"),
    QuoteResult(10.0, "24/10/2026", "EUR", "morningstar"),
    QuoteResult(10.0, "2026-13-45", "EUR", "morningstar"),
    QuoteResult(10.0, "2026-10-24", "", "morningstar"),
])
def test_implausible_quotes_are_rejected_and_the_next_source_answers(bad):
    good = Fake("finect", quote=QuoteResult(29.36, "2026-10-24", "EUR", "finect"))
    c = Client(cache=False, sources=[Fake("morningstar", quote=bad), good], fx_providers=[])
    c._cache.upsert_instruments([{"id": FUND_ID, "name": "Fund", "type": "fund", "currency": "EUR"}])
    assert c.quote(FUND_ID).source == "finect"


def test_implausible_history_points_are_dropped_not_the_whole_series():
    class Mixed(Fake):
        def get_history(self, inst, start, end):
            return HistoryResult(entries=[NavEntry("2026-10-01", 10.0), NavEntry("2026-10-02", math.nan),
                                          NavEntry("bad-date", 11.0), NavEntry("2026-10-03", -1.0),
                                          NavEntry("2026-10-04", 12.0)], currency="EUR", source=self.name)

    c = Client(cache=False, sources=[Mixed("morningstar")], fx_providers=[])
    h = c.history(FUND_ID, "2026-10-01", "2026-10-31")
    assert [(str(p.date), p.price) for p in h.points] == [("2026-10-01", 10.0), ("2026-10-04", 12.0)]


def test_a_history_with_nothing_valid_falls_through_to_the_next_source():
    class AllBad(Fake):
        def get_history(self, inst, start, end):
            return HistoryResult(entries=[NavEntry("2026-10-01", math.nan)], currency="EUR", source=self.name)

    c = Client(cache=False, sources=[AllBad("morningstar"), Fake("finect", history=[NavEntry("2026-10-01", 9.0)])],
               fx_providers=[])
    assert c.history(FUND_ID, "2026-10-01", "2026-10-31").source == "finect"


def test_vietnam_list_stops_when_the_server_ignores_paging():
    from fundly.sources.vietnam import PAGE, FmarketSource

    rows = [{"id": i, "code": f"F{i}", "name": f"Fund {i}"} for i in range(PAGE)]
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"data": {"rows": rows}})

    c = broken_client(FmarketSource, handler)
    finishes(lambda: c.search("fund"))
    assert len(calls) == 2


# --- the per-call time budget ----------------------------------------------------------------------

import time  # noqa: E402

from fundly import FundlyTimeout  # noqa: E402
from fundly.sources.base import QuoteResult as QR  # noqa: E402


class Slow(Fake):
    def __init__(self, name, seconds, **kw):
        super().__init__(name, **kw)
        self.seconds = seconds

    def get_quote(self, inst):
        time.sleep(self.seconds)
        return super().get_quote(inst)

    def get_history(self, inst, start, end):
        time.sleep(self.seconds)
        return super().get_history(inst, start, end)

    def search(self, q):
        time.sleep(self.seconds)
        return super().search(q)


def seeded(*sources, max_wait):
    c = Client(cache=False, sources=list(sources), fx_providers=[], max_wait=max_wait)
    c._cache.upsert_instruments([{"id": FUND_ID, "name": "Fund", "type": "fund", "currency": "EUR"}])
    return c


def test_a_hanging_source_cannot_hold_a_call_past_max_wait():
    c = seeded(Slow("morningstar", 5, quote=QR(1.0, "2026-10-24", "EUR", "morningstar")), max_wait=0.5)
    t = time.monotonic()
    with pytest.raises(FundlyTimeout, match="morningstar"):
        c.quote(FUND_ID)
    assert time.monotonic() - t < 1.5


def test_the_budget_is_shared_by_every_source_tried():
    c = seeded(Slow("morningstar", 0.4), Slow("finect", 0.4), Slow("ft", 0.4,
               quote=QR(1.0, "2026-10-24", "EUR", "ft")), max_wait=1.0)
    with pytest.raises(FundlyTimeout):
        c.quote(FUND_ID)


def test_fast_enough_sources_still_answer_within_the_budget():
    c = seeded(Slow("morningstar", 0.1), Fake("finect", quote=QR(2.0, "2026-10-24", "EUR", "finect")), max_wait=2)
    assert c.quote(FUND_ID).source == "finect"


def test_history_honours_the_budget_too():
    c = seeded(Slow("morningstar", 5, history=[NavEntry("2026-10-01", 1.0)]), max_wait=0.5)
    with pytest.raises(FundlyTimeout):
        c.history(FUND_ID, "2026-10-01", "2026-10-31")


def test_compare_reports_late_sources_as_timeout():
    c = seeded(Fake("finect", quote=QR(2.0, "2026-10-24", "EUR", "finect")), Slow("morningstar", 5), max_wait=0.5)
    r = c.compare(FUND_ID)
    assert [q.source for q in r.quotes] == ["finect"] and r.missing == {"morningstar": "timeout"}


def test_a_search_cut_short_is_not_remembered():
    slow = Slow("morningstar", 1, hits=[{"id": "LU0000000001", "name": "Zeta Slow", "type": "fund"}])
    c = seeded(slow, Fake("finect", hits=[{"id": "ES0000000001", "name": "Zeta Quick", "type": "fund"}]), max_wait=0.4)
    assert [i.id for i in c.search("zeta")] == ["ES0000000001"]
    time.sleep(1)
    c.max_wait = 5
    assert {i.id for i in c.search("zeta")} == {"ES0000000001", "LU0000000001"}


def test_no_budget_means_no_limit():
    c = seeded(Slow("morningstar", 0.3, quote=QR(1.0, "2026-10-24", "EUR", "morningstar")), max_wait=None)
    assert c.quote(FUND_ID).price == 1.0
