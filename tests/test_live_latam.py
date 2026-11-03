"""Real requests to the Chile, Colombia and Brazil sources. Run with: uv run pytest -m live"""
from datetime import date, timedelta

import pytest

from fundly import Client
from fundly.sources.brazil import BrazilCVMSource
from fundly.sources.chile import ChileAFPSource
from fundly.sources.colombia import ColombiaSource

pytestmark = pytest.mark.live

RECENT = date.today() - timedelta(days=14)


@pytest.fixture(scope="module")
def fl():
    with Client(cache=False, sources=[ChileAFPSource(), ColombiaSource(), BrazilCVMSource()]) as c:
        yield c


@pytest.mark.parametrize("id, currency, type_", [
    ("cl-afp:HABITAT-A", "CLP", "plan"),
    ("cl-afp:uno-e", "CLP", "plan"),
    ("co-pension:10-6000", "COP", "plan"),
    ("co-fic:28-58756-808", "COP", "fund"),
    ("br-cvm:00017024000153", "BRL", "fund"),
])
def test_quote_is_recent_and_named(fl, id, currency, type_):
    q = fl.quote(id)
    assert q.price > 0 and q.currency == currency and q.date >= RECENT and q.name
    assert fl.instrument(id).type == type_


def test_chile_history_is_weekdays_only(fl):
    h = fl.history("cl-afp:HABITAT-C", start=(date.today() - timedelta(days=60)).isoformat())
    assert len(h.points) > 30 and all(p.date.weekday() < 5 for p in h.points)


def test_colombia_pension_history_goes_back_to_2016(fl):
    h = fl.history("co-pension:10-6000", start="2016-01-01", end="2016-03-31")
    assert len(h.points) > 60 and h.points[0].date.year == 2016


def test_brazil_history_two_months(fl):
    h = fl.history("br-cvm:00017024000153", start=(date.today() - timedelta(days=45)).isoformat())
    assert len(h.points) > 20 and h.currency == "BRL"


@pytest.mark.parametrize("q, expected", [
    ("habitat", "cl-afp:HABITAT-A"),
    ("colfondos mayor riesgo", "co-pension:10-6000"),
    ("credicorp alta liquidez", "co-fic:28-58756-808"),
])
def test_search(fl, q, expected):
    assert expected in [i.id for i in fl.search(q, limit=50)]
