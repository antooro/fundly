"""Client edge cases: cache expiry and persistence, parallelism, logging, module-level API."""
import logging
import threading
import time
from datetime import date, timedelta

import pytest

import fundly
from fundly import Client, NotFound
from fundly.cache import normalize
from fundly.sources.base import NavEntry
from test_client import ETF_ID, FUND_ID, HIST, PLAN_ID, Fake, client, q


def age(c: Client, table: str, hours: float):
    with c._cache.tx() as conn:
        conn.execute(f"UPDATE {table} SET fetched_at = datetime('now', ?)", (f"-{hours} hours",))


@pytest.mark.parametrize("raw, expected", [
    ("Multigestión", "multigestion"),
    ("  Renta 4 — Num. Patr.  ", "renta 4 num patr"),
    ("AZVALOR", "azvalor"),
    ("", ""),
    (None, ""),
])
def test_normalize(raw, expected):
    assert normalize(raw) == expected


def test_ids_are_normalised_before_use():
    ms = Fake("morningstar", quote=q(29.36))
    c = client(ms)
    assert c.quote("  es0173311103 ").id == FUND_ID
    finect = Fake("finect", types=("fund", "etf", "plan"), quote=q(133.5, src="finect"))
    assert client(finect).quote(" n5508").id == PLAN_ID


def test_forcing_a_source_that_does_not_cover_the_type_is_not_found():
    with pytest.raises(NotFound):
        client(Fake("morningstar", quote=q(1))).quote(PLAN_ID, source="morningstar")


def test_quote_cache_expires_after_six_hours():
    ms = Fake("morningstar", quote=q(29.36))
    c = client(ms)
    c.quote(FUND_ID)
    age(c, "quote_cache", 5)
    assert c.quote(FUND_ID).cached is True
    age(c, "quote_cache", 7)
    assert c.quote(FUND_ID).cached is False
    assert ms.calls["quote"] == 2


def test_history_cache_expires_after_a_day():
    ms = Fake("morningstar", history=HIST)
    c = client(ms)
    c.history(FUND_ID, "2026-10-01", "2026-10-10")
    age(c, "history_fetches", 23)
    assert c.history(FUND_ID, "2026-10-01", "2026-10-10").cached is True
    age(c, "history_fetches", 25)
    assert c.history(FUND_ID, "2026-10-01", "2026-10-10").cached is False


def test_live_search_is_repeated_after_a_day():
    finect = Fake("finect", hits=[{"id": "ES0112611001", "name": "Azvalor Internacional FI", "type": "fund"}])
    c = client(finect)
    c.search("azvalor")
    age(c, "search_log", 23)
    c.search("azvalor")
    assert finect.calls["search"] == 1
    age(c, "search_log", 25)
    c.search("azvalor")
    assert finect.calls["search"] == 2


def test_search_ranks_full_name_matches_then_size():
    c = client(Fake("finect", hits=[
        {"id": "LU0000000001", "name": "Azvalor Lux Small", "type": "fund", "size_eur": 10},
        {"id": "ES0000000002", "name": "Something fuzzy", "type": "fund", "size_eur": 10_000},
        {"id": "ES0112611001", "name": "Azvalor Internacional FI", "type": "fund", "size_eur": 3_000},
    ]))
    assert [i.id for i in c.search("azvalor")] == ["ES0112611001", "LU0000000001", "ES0000000002"]


def test_search_limit_category_and_live_off():
    c = client()
    c._cache.upsert_instruments([{"id": f"LU00000000{n:02d}", "name": f"Global Fund {n}", "type": "fund",
                                  "category": "RV Global" if n % 2 else "RF Euro", "size_eur": n} for n in range(10)])
    assert len(c.search("global", live=False, limit=3)) == 3
    assert {i.category for i in c.search("global", category="rf euro", live=False, limit=50)} == {"RF Euro"}


def test_same_fund_from_two_sources_is_merged_and_findable_by_either_name():
    c = client(
        Fake("morningstar", hits=[{"id": "ES0000000005", "name": "Ejemplo Abr. Glb FI", "type": "fund",
                                   "category": "Mixtos Flexibles", "size_eur": 100}]),
        Fake("finect", hits=[{"id": "ES0000000005", "name": "Ejemplo Abreviado Global FI", "type": "fund",
                              "provider": "Gestora Ejemplo", "category": "ignored, first one wins"}]),
    )
    hit = c.search("ejemplo")[0]
    assert (hit.name, hit.provider, hit.category) == ("Ejemplo Abr. Glb FI", "Gestora Ejemplo", "Mixtos Flexibles")
    assert [i.id for i in c.search("abreviado global", live=False)] == ["ES0000000005"]


def test_a_failing_search_source_does_not_break_search():
    class Broken(Fake):
        def search(self, q):
            raise RuntimeError("down")

    c = client(Broken("morningstar"), Fake("finect", hits=[{"id": "ES0112611001", "name": "Azvalor Internacional FI",
                                                             "type": "fund"}]))
    assert [i.id for i in c.search("azvalor")] == ["ES0112611001"]


def test_history_falls_back_when_a_source_raises():
    class Broken(Fake):
        def get_history(self, inst, start, end):
            raise RuntimeError("down")

    h = client(Broken("morningstar"), Fake("finect", history=HIST)).history(FUND_ID, "2026-10-01", "2026-10-03")
    assert h.source == "finect" and len(h.points) == 3


def test_short_queries_do_not_hit_live_sources():
    finect = Fake("finect", hits=[])
    client(finect).search("az")
    assert finect.calls["search"] == 0


def test_cache_persists_between_clients(tmp_path):
    finect = Fake("finect", types=("fund", "etf", "plan"),
                  hits=[{"id": PLAN_ID, "name": "Santander Autónomos RVM PPES", "type": "plan", "currency": "EUR"}],
                  quote=q(133.5, src="finect"))
    with Client(cache=tmp_path, sources=[finect]) as c:
        c.search("santander autonomos")
        c.quote(PLAN_ID)
    offline = Fake("finect", types=("fund", "etf", "plan"), fail=True)
    with Client(cache=tmp_path, sources=[offline]) as c:
        assert c.search("autonomos", live=False)[0].id == PLAN_ID
        assert c.quote(PLAN_ID).cached is True
    assert offline.calls["quote"] == 0


def test_compare_queries_sources_in_parallel():
    class Slow(Fake):
        def get_quote(self, inst):
            time.sleep(0.3)
            return super().get_quote(inst)

    c = client(*(Slow(n, quote=q(29.36, src=n)) for n in ("morningstar", "finect", "ft", "quefondos")))
    t = time.monotonic()
    assert len(c.compare(FUND_ID).quotes) == 4
    assert time.monotonic() - t < 0.9


def test_compare_spread_ignores_market_prices_and_single_navs():
    c = client(Fake("finect", quote=q(145.74, ccy="USD", src="finect")),
               Fake("morningstar", quote=q(145.95, ccy="USD", pt="market")),
               Fake("yahoo", quote=q(146.63, ccy="USD", src="yahoo", pt="market")))
    assert c.compare(ETF_ID).nav_spread_pct is None


def test_compare_spread_uses_the_most_common_date_and_currency():
    c = client(Fake("morningstar", quote=q(100.0)), Fake("finect", quote=q(100.5, src="finect")),
               Fake("ft", quote=q(90.0, d="2026-10-20", src="ft")))
    assert c.compare(FUND_ID).nav_spread_pct == 0.5


def test_history_defaults_to_the_last_year():
    seen = {}

    class Spy(Fake):
        def get_history(self, inst, start, end):
            seen.update(start=start, end=end)
            return super().get_history(inst, start, end)

    client(Spy("morningstar", history=[NavEntry(date.today().isoformat(), 1.0)])).history(FUND_ID)
    assert seen == {"start": (date.today() - timedelta(days=365)).isoformat(), "end": date.today().isoformat()}


def test_history_not_found_and_instrument_not_found():
    c = client(Fake("morningstar"), Fake("finect"))
    with pytest.raises(NotFound):
        c.history(FUND_ID)
    with pytest.raises(NotFound):
        c.instrument("LU9999999999")


def test_source_failures_are_logged_on_the_fundly_logger(caplog):
    with caplog.at_level(logging.WARNING, logger="fundly"):
        client(Fake("morningstar", fail=True), Fake("finect", quote=q(29.36, src="finect"))).quote(FUND_ID)
    assert any("morningstar" in r.getMessage() and r.name == "fundly" for r in caplog.records)


def test_library_is_silent_without_logging_config():
    assert any(isinstance(h, logging.NullHandler) for h in logging.getLogger("fundly").handlers)


def test_module_level_functions_use_a_shared_default_client(monkeypatch):
    c = client(Fake("morningstar", quote=q(29.36), history=HIST))
    monkeypatch.setattr(fundly, "_default", c)
    assert fundly.quote(FUND_ID).price == 29.36
    assert fundly.instrument(FUND_ID).id == FUND_ID
    assert fundly.search("numantia", live=False)[0].id == FUND_ID
    assert len(fundly.history(FUND_ID, "2026-10-01", "2026-10-05").points) == 5
    assert fundly.compare(FUND_ID).quotes[0].price == 29.36


def test_client_is_safe_to_use_from_several_threads():
    c = client(Fake("morningstar", quote=q(29.36), history=HIST))
    errors = []

    def work():
        try:
            for _ in range(20):
                c.quote(FUND_ID)
                c.history(FUND_ID, "2026-10-01", "2026-10-20")
                c.search("numantia", live=False)
        except Exception as e:  # pragma: no cover - only on failure
            errors.append(e)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


@pytest.mark.parametrize("platform, env, expected", [
    ("win32", {"LOCALAPPDATA": r"C:\Users\ana\AppData\Local"}, ("Local", "fundly", "Cache")),
    ("darwin", {}, ("Library", "Caches", "fundly")),
    ("linux", {"XDG_CACHE_HOME": "/srv/cache"}, ("srv", "cache", "fundly")),
    ("linux", {}, (".cache", "fundly")),
])
def test_default_cache_dir_follows_the_platform(monkeypatch, platform, env, expected):
    from fundly import cache
    for var in ("FUNDLY_CACHE_DIR", "LOCALAPPDATA", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(cache.sys, "platform", platform)
    # Compared as text: a Windows path built on macOS/Linux keeps its backslashes inside one part.
    assert str(cache.default_cache_dir()).replace("\\", "/").endswith("/".join(expected))


def test_fundly_cache_dir_overrides_everything(monkeypatch, tmp_path):
    from fundly import cache
    monkeypatch.setenv("FUNDLY_CACHE_DIR", str(tmp_path / "x"))
    monkeypatch.setattr(cache.sys, "platform", "win32")
    assert cache.default_cache_dir() == tmp_path / "x"


def test_a_source_that_has_nothing_is_not_asked_again_for_a_day():
    empty = Fake("morningstar")
    c = client(empty, Fake("finect", quote=q(29.36, src="finect"), history=HIST))
    for _ in range(3):
        c.quote(FUND_ID)
        c.history(FUND_ID, "2026-10-01", "2026-10-10")
    assert (empty.calls["quote"], empty.calls["history"]) == (1, 1)
    age(c, "misses", 25)
    with c._cache.tx() as conn:
        conn.execute("DELETE FROM quote_cache")
    c.quote(FUND_ID)
    assert empty.calls["quote"] == 2


def test_errors_are_not_remembered_as_misses():
    broken = Fake("morningstar", fail=True)
    c = client(broken, Fake("finect", quote=q(29.36, src="finect")))
    c.quote(FUND_ID)
    with c._cache.tx() as conn:
        conn.execute("DELETE FROM quote_cache")
    c.quote(FUND_ID)
    assert broken.calls["quote"] == 2


def test_a_history_miss_only_covers_the_same_range():
    empty = Fake("morningstar")
    c = client(empty, Fake("finect", history=HIST))
    c.history(FUND_ID, "2026-10-01", "2026-10-10")
    c.history(FUND_ID, "2026-10-01", "2026-10-20")
    assert empty.calls["history"] == 2
