"""TEFAS (Turkey) against hand-made responses shaped like the real ones."""
import json
from datetime import date

import httpx
import pytest

from fundly.sources.base import ETF, FUND, PLAN, Inst
from fundly.sources.turkey import TEFASSource, months_back
from test_sources import mock, path

NAME = "YAPI KREDİ PORTFÖY İKİNCİ FON SEPETİ FONU"


def price_row(day, price, code="YAC"):
    return {"fonKodu": code, "fonUnvan": NAME, "kategoriDerece": 62, "kategoriFonSay": 101, "tarih": day, "fiyat": price}


LISTS = {
    "YAT": [{"fonKodu": "YAC", "fonUnvan": NAME, "fonTurAciklama": "Fon Sepeti Fonu", "tefasDurum": True},
            {"fonKodu": "TTE", "fonUnvan": "İŞ PORTFÖY BIST TEKNOLOJİ AĞIRLIK SINIRLAMALI ENDEKS HİSSE SENEDİ FONU"},
            {"fonKodu": None, "fonUnvan": "ignored"}, "junk"],
    "EMK": [{"fonKodu": "EAE", "fonUnvan": "AGESA HAYAT VE EMEKLİLİK A.Ş. ALTIN EMEKLİLİK YATIRIM FONU",
             "fonTurAciklama": "Altın Fonu"}],
    "BYF": [{"fonKodu": "LTK", "fonUnvan": "AK PORTFÖY ALTIN KATILIM BORSA YATIRIM FONU"}],
}
PRICES = {
    1: [price_row("2026-10-30", 14.2), price_row("2026-11-02", 14.087382), price_row("2026-11-03", 0),
        price_row("2026-10-31", None), {"fonKodu": "OTHER", "tarih": "2026-11-03", "fiyat": 1.0}, "junk"],
    12: [price_row("2026-01-02", 11.5), price_row("2026-11-02", 14.087382)],
    60: [price_row("2021-11-03", 2.1), price_row("2026-11-02", 14.087382)],
}


def tefas():
    def prices(request):
        body = json.loads(request.content)
        if body["periyod"] not in PRICES:
            return httpx.Response(200, json={"errorCode": None, "errorMessage": "Sistem Hatası!!", "resultList": None})
        rows = PRICES[body["periyod"]] if body["fonKodu"] == "YAC" else []
        return httpx.Response(200, json={"errorCode": None, "errorMessage": None, "resultList": rows})

    def lists(request):
        body = json.loads(request.content)
        assert body["islem"] == 1 and body["calismaTipi"] == 2  # the filter body the site's own search sends
        return httpx.Response(200, json={"errorCode": None, "errorMessage": None, "resultList": LISTS[body["fonTipi"]]})

    return mock(TEFASSource, [(path("fonFiyatBilgiGetir"), prices), (path("fonGetiriBazliBilgiGetir"), lists)])


def test_quote_is_the_latest_valid_price():
    q = tefas().get_quote(Inst("tr-tefas:YAC", FUND))
    assert (q.nav, q.date, q.currency, q.source, q.price_type) == (14.087382, "2026-11-02", "TRY", "tefas", "nav")


def test_unknown_code_is_none():
    assert tefas().get_quote(Inst("tr-tefas:ZZZ", FUND)) is None
    assert tefas().get_history(Inst("tr-tefas:ZZZ", FUND), "2026-01-01", "2026-11-03") is None


def test_history_asks_for_the_smallest_period_that_reaches_start():
    src = tefas()
    h = src.get_history(Inst("tr-tefas:YAC", FUND), "2021-01-01", "2026-12-31")
    assert [(e.date, e.nav) for e in h.entries] == [("2021-11-03", 2.1), ("2026-11-02", 14.087382)]
    assert json.loads(src.calls[-1].content)["periyod"] == 60  # older than 5 years: the most there is


def test_months_back_snaps_to_what_the_api_accepts():
    today = date(2026, 11, 3)
    assert months_back("2026-10-20", today) == 1
    assert months_back("2026-09-01", today) == 3
    assert months_back("2025-12-01", today) == 12
    assert months_back("2024-01-01", today) == 36
    assert months_back("2026-10-07", today) == 1 and months_back("2026-10-04", today) == 3  # 27 days fit in one
    assert months_back("1990-01-01", today) == 60


def test_an_api_error_message_is_an_error_not_a_miss():
    src = tefas()
    with pytest.raises(ValueError, match="Sistem"):
        src._prices("YAC", 2)


def test_search_and_resolve_over_all_three_lists():
    src = tefas()
    assert [h["id"] for h in src.search("yapi kredi portfoy")] == ["tr-tefas:YAC"]
    assert [h["id"] for h in src.search("is portföy teknoloji")] == ["tr-tefas:TTE"]
    assert src.resolve("tr-tefas", "eae") == {
        "id": "tr-tefas:EAE", "name": "AGESA HAYAT VE EMEKLİLİK A.Ş. ALTIN EMEKLİLİK YATIRIM FONU", "type": PLAN,
        "currency": "TRY", "category": "Altın Fonu"}
    assert src.resolve("tr-tefas", "LTK")["type"] == ETF
    assert src.resolve("tr-tefas", "NOPE") is None and src.resolve("isin", "YAC") is None
    assert src.search("") == []
    assert sum("fonGetiriBazliBilgiGetir" in str(r.url) for r in src.calls) == 3  # one list per type, cached


def test_supports_its_own_scheme_and_types():
    src = TEFASSource(min_interval=0)
    assert src.supports(Inst("tr-tefas:YAC", FUND)) and src.supports(Inst("tr-tefas:EAE", PLAN))
    assert not src.supports(Inst("IE00B4L5Y983", FUND))
