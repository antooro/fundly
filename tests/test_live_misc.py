"""Real requests: CAFCI (AR), FT symbols, analizy.pl (PL), Gemel-net/Pensia-net (IL). Run with -m live."""
from datetime import date, timedelta

import pytest

from fundly.sources.argentina import CAFCISource
from fundly.sources.base import Inst
from fundly.sources.ft import FTSource
from fundly.sources.israel import GemelNetSource
from fundly.sources.poland import AnalizySource

pytestmark = pytest.mark.live

RECENT = (date.today() - timedelta(days=21)).isoformat()
MONTHS_AGO = (date.today() - timedelta(days=120)).isoformat()


def test_cafci_superfondo_acciones():
    src = CAFCISource()
    q = src.get_quote(Inst("ar-cafci:148"))
    assert q.currency == "ARS" and q.date >= RECENT and 10 < q.nav < 1_000_000
    assert any(h["id"] == "ar-cafci:148" for h in src.search("superfondo acciones"))



def test_cafci_history_is_daily_from_2020_including_2026():
    h = CAFCISource().get_history(Inst("ar-cafci:148"), "2020-01-01", date.today().isoformat())
    days = {e.date for e in h.entries}
    assert h.entries[0].date < "2020-02-01" and h.currency == "ARS"
    assert sum(d.startswith("2026-07") for d in days) >= 18  # a month the per-day endpoints lack
    assert max(days) >= (date.today() - timedelta(days=6)).isoformat()


def test_ft_uk_pension_fund_without_isin():
    src = FTSource()
    h = src.get_history(Inst("ft:0P00009VB8"), "2024-01-01", date.today().isoformat())
    assert h.currency == "GBP" and h.price_type == "nav" and len(h.entries) > 200
    assert src.resolve("ft", "0P00009VB8")["name"]


@pytest.mark.parametrize("code, kind", [("PZU50", "fund"), ("ARK52", "plan"), ("EALL1", "plan")])
def test_analizy(code, kind):
    src = AnalizySource()
    q = src.get_quote(Inst(f"pl-analizy:{code}"))
    assert q.currency == "PLN" and q.date >= RECENT
    assert src.resolve("pl-analizy", code)["type"] == kind
    assert len(src.get_history(Inst(f"pl-analizy:{code}"), "2024-01-01", date.today().isoformat()).entries) > 300


@pytest.mark.parametrize("id", ["il-gemel:880", "il-pensia:2009"])
def test_israel_monthly_index(id):
    src = GemelNetSource()
    q = src.get_quote(Inst(id))
    assert q.price_type == "index" and q.date >= MONTHS_AGO and q.nav > 0
    h = src.get_history(Inst(id), "2020-01-01", date.today().isoformat())
    assert len(h.entries) > 30 and all(e.date.endswith(("28", "29", "30", "31")) for e in h.entries)
    assert src.resolve(*id.split(":"))["name"]
