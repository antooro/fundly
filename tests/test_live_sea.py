"""Real requests to fmarket (Vietnam) and Pasardana (Indonesia). Run with: uv run pytest -m live"""
from datetime import date, timedelta

import pytest

from fundly.sources.base import FUND, Inst
from fundly.sources.id_managers import MANAGERS
from fundly.sources.indonesia import PasardanaSource
from fundly.sources.vietnam import FmarketSource

pytestmark = pytest.mark.live

RECENT = (date.today() - timedelta(days=14)).isoformat()


@pytest.fixture(scope="module")
def fm():
    s = FmarketSource()
    yield s
    s.close()


@pytest.fixture(scope="module")
def pd():
    s = PasardanaSource()
    yield s
    s.close()


@pytest.mark.parametrize("code", ["VFF", "VCBFBCF", "SSISCA"])
def test_fmarket_quote_is_recent(fm, code):
    if not fm.resolve("vn-fmarket", code):
        pytest.skip(f"{code} not listed on fmarket today")
    q = fm.get_quote(Inst(f"vn-fmarket:{code}", FUND))
    assert q.currency == "VND" and q.nav > 0 and q.date >= RECENT


def test_fmarket_history_goes_back_to_launch(fm):
    h = fm.get_history(Inst("vn-fmarket:VFF", FUND), "2013-01-01", date.today().isoformat())
    assert len(h.entries) > 900 and h.entries[0].date < "2014-01-01"


def test_fmarket_search(fm):
    assert "vn-fmarket:VFF" in [h["id"] for h in fm.search("vinacapital trai phieu")]


@pytest.mark.parametrize("id", ["IDN000007002", "id-pasardana:13"])
def test_pasardana_quote_is_recent(pd, id):
    q = pd.get_quote(Inst(id, FUND))
    assert q.currency == "IDR" and q.nav > 0 and q.date >= RECENT


def test_pasardana_usd_funds_are_in_usd(pd):
    hits = [h for h in pd.search("ashmore usd") if h["currency"] == "USD"]
    assert hits
    q = pd.get_quote(Inst(hits[0]["id"], FUND))
    assert q.currency == "USD" and 0 < q.nav < 100


def test_pasardana_resolve_returns_isin(pd):
    assert pd.resolve("id-pasardana", "13")["id"] == "IDN000007002"


def test_pasardana_history_covers_the_last_year(pd):
    start = (date.today() - timedelta(days=330)).isoformat()
    h = pd.get_history(Inst("id-pasardana:13", FUND), start, date.today().isoformat())
    assert len(h.entries) > 150 and h.entries[0].date < (date.today() - timedelta(days=300)).isoformat()
    assert h.currency == "IDR" and all(e.nav > 0 for e in h.entries)


@pytest.mark.parametrize("manager", MANAGERS, ids=lambda m: m.name)
def test_indonesian_managers_publish_years_of_history_matching_pasardana(pd, manager):
    funds = {manager.key(f["name"]): f for f in pd._lists.get()[1].values()
             if manager.provider in (f["provider"] or "").lower()}
    site = {manager.key(name): key for name, key in manager.catalogue(pd._get).items()}
    matched = [k for k in site if k in funds]
    assert len(matched) >= 0.6 * len(site) and len(matched) >= 8
    # some funds are new; at least one goes well beyond Pasardana's ~240 points
    assert any(len(manager.history(pd._get, site[k])) > 400 for k in matched)  # stops at the first


def test_pasardana_history_of_a_batavia_fund_starts_in_the_nineties(pd):
    h = pd.get_history(Inst("id-pasardana:" + next(f["id"] for f in pd._lists.get()[1].values()
                                                   if f["name"].lower() == "batavia dana saham"), FUND),
                       "1990-01-01", date.today().isoformat())
    assert h.entries[0].date < "2000-01-01" and h.entries[-1].date >= RECENT
