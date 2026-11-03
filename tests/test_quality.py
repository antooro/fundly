"""Stale and suspicious prices, history warnings, to_dict(), and the on-disk file cache of sources."""
import json
import time
from datetime import date, timedelta

import httpx

from fundly.models import PricePoint
from fundly.quality import apart, history_warnings, how_far, stale_warning
from fundly.sources import base
from fundly.sources.base import NavEntry, Source
from test_client import FUND_ID, Fake, client, q

TODAY = date.today()


def days_ago(n):
    return (TODAY - timedelta(days=n)).isoformat()


# --- stale -----------------------------------------------------------------------------------------

def test_stale_thresholds():
    assert stale_warning(TODAY - timedelta(days=10), "nav") is None
    assert stale_warning(TODAY - timedelta(days=11), "nav").startswith("stale: last price is 11 days old")
    assert stale_warning(TODAY - timedelta(days=40), "index") is None  # monthly series
    assert stale_warning(TODAY - timedelta(days=46), "index")


def test_old_quote_is_flagged_but_returned():
    r = client(Fake("morningstar", quote=q(29.36, d=days_ago(400)))).quote(FUND_ID)
    assert r.stale and not r.suspicious and r.age_days == 400 and r.price == 29.36


def test_fresh_quote_has_no_warnings():
    r = client(Fake("morningstar", quote=q(29.36, d=days_ago(1)))).quote(FUND_ID)
    assert (r.warnings, r.stale, r.age_days) == ((), False, 1)


# --- outliers and arbitration ----------------------------------------------------------------------

def test_how_far():
    assert how_far(559.07, 5.5907) == "×100" and how_far(5.59, 559.07) == "×0.01" and how_far(1.6, 1.0) == "+60%"
    assert apart(150.1, 100) and not apart(149, 100)


def prime(c, price, source="finect", day=None):
    """A recent price from another source, as if fetched earlier."""
    with c._cache.tx() as conn:
        conn.execute("INSERT INTO quote_cache (id, source, price, price_date, currency, price_type, fetched_at) "
                     "VALUES (?, ?, ?, ?, 'EUR', 'nav', datetime('now', '-1 day'))",
                     (FUND_ID, source, price, day or days_ago(2)))


def test_a_x100_price_is_replaced_by_the_next_source_that_agrees_with_what_we_knew():
    ms = Fake("morningstar", quote=q(5.5907, d=days_ago(1)))
    fin = Fake("finect", quote=q(559.07, d=days_ago(1), src="finect"))
    c = client(ms, fin)
    prime(c, 558.9, source="ft")
    r = c.quote(FUND_ID)
    assert (r.source, r.price) == ("finect", 559.07)
    assert r.warnings and r.warnings[0].startswith("replaced: morningstar said 5.5907") and not r.suspicious


def test_when_two_sources_agree_the_old_reference_is_the_odd_one():
    c = client(Fake("morningstar", quote=q(50.0, d=days_ago(1))),
               Fake("finect", quote=q(50.5, d=days_ago(1), src="finect")))
    prime(c, 100.0, source="ft", day=days_ago(20))  # e.g. before a real crash
    r = c.quote(FUND_ID)
    assert (r.source, r.price, r.warnings) == ("morningstar", 50.0, ())


def test_a_suspicious_price_nobody_can_check_is_returned_flagged():
    c = client(Fake("morningstar", quote=q(5.59, d=days_ago(1))))
    prime(c, 559.0, source="ft")
    r = c.quote(FUND_ID)
    assert r.suspicious and "×0.01" in r.warnings[0]


def test_forcing_a_source_skips_arbitration():
    c = client(Fake("morningstar", quote=q(5.59, d=days_ago(1))), Fake("finect", quote=q(559.0, src="finect")))
    prime(c, 559.0, source="ft")
    assert c.quote(FUND_ID, source="morningstar").price == 5.59


def test_reference_in_another_currency_is_ignored():
    c = client(Fake("morningstar", quote=q(5.59, d=days_ago(1), ccy="USD")))
    prime(c, 559.0, source="ft")  # EUR
    assert c.quote(FUND_ID).warnings == ()


def test_compare_flags_the_source_far_from_the_others():
    c = client(Fake("morningstar", quote=q(5.5907, d=days_ago(1))), Fake("finect", quote=q(559.07, src="finect",
               d=days_ago(1))), Fake("ft", quote=q(559.07, src="ft", d=days_ago(1))))
    r = c.compare(FUND_ID)
    flagged = {x.source for x in r.quotes if x.suspicious}
    assert flagged == {"morningstar"}


# --- history warnings ------------------------------------------------------------------------------

def pts(*prices):
    return [(TODAY - timedelta(days=len(prices) - i), p) for i, p in enumerate(prices)]


def test_history_spike_and_jump_detection():
    assert history_warnings(pts(10, 10.1, 10.2, 10.1)) == []
    spike = history_warnings(pts(10, 10.1, 1010, 10.2, 10.3))
    assert len(spike) == 1 and spike[0].startswith("outlier:") and "1010" in spike[0]
    jump = history_warnings(pts(10, 10.1, 1010, 1011, 1012))
    assert len(jump) == 1 and jump[0].startswith("jump: ×100")


def test_history_carries_warnings_but_keeps_the_points():
    hist = [NavEntry(days_ago(4), 10.0), NavEntry(days_ago(3), 1000.0), NavEntry(days_ago(2), 10.1)]
    h = client(Fake("morningstar", history=hist)).history(FUND_ID, days_ago(10), days_ago(0))
    assert [p.price for p in h.points] == [10.0, 1000.0, 10.1] and h.warnings[0].startswith("outlier")


# --- to_dict ---------------------------------------------------------------------------------------

def test_everything_serialises_to_json():
    c = client(Fake("morningstar", quote=q(29.36, d=days_ago(1)), history=[NavEntry(days_ago(2), 29.0)]),
               Fake("finect", quote=q(29.35, d=days_ago(1), src="finect")))
    for obj in (c.quote(FUND_ID), c.history(FUND_ID, days_ago(5), days_ago(0)), c.compare(FUND_ID),
                c.instrument(FUND_ID), *c.search("numantia", live=False)):
        d = obj.to_dict()
        assert json.loads(json.dumps(d)) == d
    qd = c.quote(FUND_ID).to_dict()
    assert qd["date"] == days_ago(1) and qd["warnings"] == []
    assert c.history(FUND_ID, days_ago(5), days_ago(0)).to_dict()["points"] == [{"date": days_ago(2), "price": 29.0}]
    assert PricePoint(TODAY, 1.0).to_dict() == {"date": TODAY.isoformat(), "price": 1.0}


# --- the on-disk file cache ------------------------------------------------------------------------

class FileSource(Source):
    name = "files"

    def __init__(self, tmp_path, handler):
        super().__init__(min_interval=0)
        self.http = httpx.Client(transport=httpx.MockTransport(handler))
        self.files_dir = tmp_path / "files"


def test_files_are_kept_on_disk_and_revalidated(tmp_path, monkeypatch):
    seen = []

    def handler(request):
        seen.append(dict(request.headers))
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=b"DATA-v1", headers={"ETag": '"v1"', "Last-Modified": "Mon, 26 Oct 2026"})

    src = FileSource(tmp_path, handler)
    assert src._fetch_file("https://example.org/a.zip", "a.zip") == b"DATA-v1"
    assert src._fetch_file("https://example.org/a.zip", "a.zip") == b"DATA-v1"
    assert len(seen) == 1  # fresh copy used without asking
    monkeypatch.setattr(base, "FILE_FRESH_SECONDS", 0)
    assert src._fetch_file("https://example.org/a.zip", "a.zip") == b"DATA-v1"
    assert len(seen) == 2 and seen[1]["if-none-match"] == '"v1"'  # revalidated, 304, nothing downloaded
    other = FileSource(tmp_path, handler)  # a new session reuses the file
    assert other._fetch_file("https://example.org/a.zip", "a.zip") == b"DATA-v1"


def test_a_changed_file_replaces_the_old_one(tmp_path, monkeypatch):
    version = {"n": 1}

    def handler(request):
        return httpx.Response(200, content=f"DATA-v{version['n']}".encode(), headers={"ETag": f'"v{version["n"]}"'})

    monkeypatch.setattr(base, "FILE_FRESH_SECONDS", 0)
    src = FileSource(tmp_path, handler)
    assert src._fetch_file("https://example.org/a.zip", "a.zip") == b"DATA-v1"
    version["n"] = 2
    assert src._fetch_file("https://example.org/a.zip", "a.zip") == b"DATA-v2"
    assert (tmp_path / "files" / "a.zip").read_bytes() == b"DATA-v2"


def test_missing_file_and_size_cap(tmp_path, monkeypatch):
    def handler(request):
        if request.url.path.endswith("missing.zip"):
            return httpx.Response(404)
        return httpx.Response(200, content=b"x" * 100)

    monkeypatch.setattr(base, "FILES_MAX_BYTES", 250)
    src = FileSource(tmp_path, handler)
    assert src._fetch_file("https://example.org/missing.zip", "missing.zip") is None
    for name in ("1.zip", "2.zip", "3.zip"):
        src._fetch_file(f"https://example.org/{name}", name)
        time.sleep(0.01)
    kept = sorted(p.name for p in (tmp_path / "files").iterdir() if not p.name.endswith(".meta.json"))
    assert kept == ["2.zip", "3.zip"]  # the least recently used went first


def test_no_cache_directory_means_no_files_written(tmp_path):
    src = FileSource(tmp_path, lambda r: httpx.Response(200, content=b"x"))
    src.files_dir = None
    assert src._fetch_file("https://example.org/a.zip", "a.zip") == b"x"
    assert not (tmp_path / "files").exists()


def test_client_gives_each_source_its_own_folder(tmp_path):
    from fundly import Client
    with Client(cache=tmp_path, sources=[Fake("morningstar"), Fake("finect")], fx_providers=[]) as c:
        assert {s.name: s.files_dir for s in c._sources.values()} == {
            "morningstar": tmp_path / "files" / "morningstar", "finect": tmp_path / "files" / "finect"}
    with Client(cache=False, sources=[Fake("morningstar")], fx_providers=[]) as c:
        assert c._sources["morningstar"].files_dir is None
