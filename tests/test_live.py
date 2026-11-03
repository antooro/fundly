"""Real requests to the sources. Run with: uv run pytest -m live"""
from datetime import date, timedelta

import pytest

from fundly import Client

pytestmark = pytest.mark.live

RECENT = date.today() - timedelta(days=14)


@pytest.fixture(scope="module")
def fa():
    with Client(cache=False) as c:
        yield c


@pytest.mark.parametrize("q, expected", [
    ("numantia", "ES0173311103"),
    ("azvalor internacional", "ES0112611001"),
    ("santander autonomos rvm", "N5508"),
    ("santander asg acciones", "N3634"),
    ("mapfre ata autonomos subplan", "S0156"),
])
def test_search_by_name(fa, q, expected):
    assert expected in [i.id for i in fa.search(q, limit=10)]


@pytest.mark.parametrize("id, type_", [
    ("ES0112611001", "fund"), ("ES0173311103", "fund"), ("N5508", "plan"), ("N3634", "plan"),
    ("IE00B4L5Y983", "etf"), ("LU0996182563", "fund"), ("S0156", "plan"),
])
def test_quote_is_recent(fa, id, type_):
    q = fa.quote(id)
    assert q.date >= RECENT and q.price > 0
    assert fa.instrument(id).type == type_


def test_etf_quote_is_official_nav(fa):
    assert fa.quote("IE00B4L5Y983").price_type == "nav"


def test_independent_sources_agree(fa):
    c = fa.compare("ES0112611001")
    assert len(c.quotes) >= 3
    assert c.nav_spread_pct is None or c.nav_spread_pct < 0.5


@pytest.mark.parametrize("source, id", [
    ("morningstar", "ES0112611001"), ("morningstar", "IE00B4L5Y983"), ("morningstar", "LU0996182563"),
    ("finect", "ES0112611001"), ("finect", "IE00B4L5Y983"), ("finect", "N5508"), ("finect", "N3634"),
    ("ft", "ES0112611001"), ("ft", "IE00B4L5Y983"),
    ("comparadorfondos", "N5508"), ("comparadorfondos", "N3634"),
    ("quefondos", "ES0112611001"), ("quefondos", "N3634"),
    ("yahoo", "IE00B4L5Y983"),
])
def test_each_source_covers_what_it_claims(fa, source, id):
    q = fa.quote(id, source=source)
    assert q.source == source and q.price > 0 and q.date >= RECENT - timedelta(days=7)


@pytest.mark.parametrize("id, a, b, start", [
    ("ES0112611001", "morningstar", "ft", "2026-09-01"),
    ("N5508", "finect", "comparadorfondos", "2026-09-01"),
    ("IE00B4L5Y983", "finect", "yahoo", None),
])
def test_independent_series_match_day_by_day(fa, id, a, b, start):
    start = start or (date.today() - timedelta(days=60)).isoformat()
    ha, hb = fa.history(id, start=start, source=a), fa.history(id, start=start, source=b)
    common = {p.date: p.price for p in ha.points}.keys() & {p.date: p.price for p in hb.points}.keys()
    assert len(common) >= 10
    pa, pb = {p.date: p.price for p in ha.points}, {p.date: p.price for p in hb.points}
    if ha.price_type == hb.price_type == "nav":
        assert all(abs(pa[d] / pb[d] - 1) < 1e-4 for d in common)
    else:
        # NAV vs exchange close (or different listings): close, never identical by construction.
        assert all(abs(pa[d] / pb[d] - 1) < 0.05 for d in common)


def test_plan_history_goes_back_years(fa):
    h = fa.history("N5508", start="2023-01-01")
    assert len(h.points) > 500 and h.currency == "EUR"


@pytest.mark.parametrize("id, currency, fx_source", [
    ("ES0112611001", "USD", "ecb"), ("INF179K01XQ0", "EUR", "ecb"), ("cl-afp:HABITAT-A", "EUR", "yahoo"),
])
def test_currency_conversion(fa, id, currency, fx_source):
    q = fa.quote(id, currency=currency)
    assert q.currency == currency and q.fx_source == fx_source
    assert q.price == pytest.approx(q.original_price * q.fx_rate)
    assert 0 <= (q.date - q.fx_date).days <= 5


def test_history_conversion_keeps_every_point(fa):
    native = fa.history("VTSAX", start=date.today() - timedelta(days=30))
    eur = fa.history("VTSAX", start=date.today() - timedelta(days=30), currency="EUR")
    assert len(native.points) == len(eur.points) and eur.original_currency == "USD"
    assert all(0.7 < e.price / n.price < 1.2 for n, e in zip(native.points, eur.points, strict=False))
