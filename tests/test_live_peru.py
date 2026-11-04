"""Real requests to the SMV (Peru). Run with: uv run pytest -m live"""
from datetime import date, timedelta

import pytest

from fundly.sources.base import FUND, Inst
from fundly.sources.peru import SMVSource

pytestmark = pytest.mark.live

RECENT = (date.today() - timedelta(days=10)).isoformat()


@pytest.fixture(scope="module")
def smv():
    s = SMVSource()
    yield s
    s.close()


def test_single_series_fund_has_years_of_daily_history(smv):
    h = smv.get_history(Inst("pe-smv:38036-0002", FUND), "2015-01-01", date.today().isoformat())
    assert h.currency == "PEN" and h.entries[0].date < "2015-02-01" and h.entries[-1].date >= RECENT
    assert len(h.entries) > 2500


def test_series_of_a_multi_series_fund(smv):
    a, b = (smv.get_quote(Inst(f"pe-smv:151229-0004-{s}", FUND)) for s in "AB")
    assert a.currency == b.currency == "USD" and a.date >= RECENT and a.nav != b.nav


def test_search_finds_funds_by_manager(smv):
    assert any(h["id"] == "pe-smv:38036-0002" for h in smv.search("bbva renta mediano plazo soles"))
