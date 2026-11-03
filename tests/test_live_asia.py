"""Real requests to the India and China sources. Run with: uv run pytest -m live tests/test_live_asia.py"""
from contextlib import closing
from datetime import date, timedelta

import pytest

from fundly.sources.base import Inst
from fundly.sources.china import EastmoneySource
from fundly.sources.india import AMFISource, NPSSource

pytestmark = pytest.mark.live

RECENT = (date.today() - timedelta(days=14)).isoformat()
YEAR_AGO = (date.today() - timedelta(days=365)).isoformat()
TODAY = date.today().isoformat()


@pytest.fixture(scope="module")
def amfi():
    src = AMFISource()
    yield src
    src.close()


@pytest.mark.parametrize("id", ["INF179K01XQ0", "in-amfi:118989"])
def test_amfi_quote(amfi, id):
    q = amfi.get_quote(Inst(id))
    assert q.currency == "INR" and q.nav > 0 and q.date >= RECENT


def test_amfi_history_and_search(amfi):
    h = amfi.get_history(Inst("INF179K01XQ0"), YEAR_AGO, TODAY)
    assert len(h.entries) > 200 and h.entries[-1].date >= RECENT
    hits = amfi.search("hdfc mid cap direct growth")
    assert "INF179K01XQ0" in [x["id"] for x in hits]
    assert amfi.resolve("in-amfi", "118989")["provider"] == "HDFC Mutual Fund"


def test_nps():
    with closing(NPSSource()) as src:
        inst = Inst("in-nps:SM001001", "plan")
        q = src.get_quote(inst)
        assert q.nav > 0 and q.date >= RECENT
        assert len(src.get_history(inst, YEAR_AGO, TODAY).entries) > 200
        assert src.search("sbi central govt")[0]["id"] == "in-nps:SM001001"


def test_eastmoney():
    with closing(EastmoneySource()) as src:
        inst = Inst("cn:110022")
        q = src.get_quote(inst)
        assert q.currency == "CNY" and q.nav > 0 and q.date >= RECENT
        h = src.get_history(inst, YEAR_AGO, TODAY)
        assert len(h.entries) > 200 and abs(h.entries[-1].nav / q.nav - 1) < 0.05
        assert src.resolve("cn", "110022")["name"]

