"""Real requests to uitf.com.ph (Philippine UITFs). Run with: uv run pytest -m live"""
from datetime import date, timedelta

import pytest

from fundly.sources.base import FUND, Inst
from fundly.sources.philippines import UITFSource

pytestmark = pytest.mark.live

RECENT = (date.today() - timedelta(days=14)).isoformat()


@pytest.fixture(scope="module")
def ph(tmp_path_factory):
    s = UITFSource()
    s.files_dir = tmp_path_factory.mktemp("uitf")
    yield s
    s.close()


def test_bpi_equity_index_fund_quote_and_history(ph):
    q = ph.get_quote(Inst("ph-uitf:69", FUND))  # BPI Philippine Equity Index Fund
    assert q.currency == "PHP" and q.date >= RECENT and 10 < q.nav < 1000
    h = ph.get_history(Inst("ph-uitf:69", FUND), "2020-01-01", date.today().isoformat())
    assert h.entries[-1].nav == q.nav  # what this machine has seen: today's price, kept on disk
    assert any(p.name.startswith("navpu-") for p in ph.files_dir.iterdir())


def test_catalogue_covers_every_bank_and_search_by_bank(ph):
    catalogue = ph._catalogue()
    assert len(catalogue) > 300 and len({f["bank"] for f in catalogue.values()}) > 20
    hits = ph.search("bpi equity index")
    assert any(h["id"] == "ph-uitf:69" for h in hits)
    assert {f["currency"] for f in catalogue.values()} >= {"PHP", "USD"}
