"""Real requests to TEFAS (Turkey). Run with: uv run pytest -m live"""
from datetime import date, timedelta

import pytest

from fundly.sources.base import FUND, PLAN, Inst
from fundly.sources.turkey import TEFASSource

pytestmark = pytest.mark.live

RECENT = (date.today() - timedelta(days=10)).isoformat()


@pytest.fixture(scope="module")
def tf():
    s = TEFASSource()
    yield s
    s.close()


@pytest.mark.parametrize("code, kind", [("YAC", FUND), ("EAE", PLAN)])
def test_tefas_quote_is_recent(tf, code, kind):
    q = tf.get_quote(Inst(f"tr-tefas:{code}", kind))
    assert q.currency == "TRY" and q.date >= RECENT and q.nav > 0


def test_tefas_history_reaches_back_five_years(tf):
    h = tf.get_history(Inst("tr-tefas:YAC", FUND), "2015-01-01", date.today().isoformat())
    assert len(h.entries) > 1000 and h.entries[0].date < (date.today() - timedelta(days=4 * 365)).isoformat()


def test_tefas_search_finds_investment_and_pension_funds(tf):
    assert any(h["id"] == "tr-tefas:YAC" for h in tf.search("yapi kredi fon sepeti"))
    pensions = tf.search("emeklilik altin")
    assert pensions and all(h["type"] == "plan" for h in pensions if "EMEKLİLİK" in h["name"])
