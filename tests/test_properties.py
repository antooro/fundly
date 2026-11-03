"""Property-based tests: rules that must hold for any input, checked on thousands of generated cases."""
import math
import string
from datetime import date, timedelta

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from fundly import Client, InvalidId
from fundly.cache import normalize
from fundly.client import _valid_point
from fundly.currency import MINOR_UNITS, to_major
from fundly.ids import canonical, is_valid_isin, isin_check_digit, parse, split
from fundly.sources.base import NavEntry, in_range
from test_client import Fake, q
from test_fx import FakeRates

ANY_TEXT = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=40)
ID_CHARS = string.ascii_letters + string.digits + ":-./_ "


@given(st.one_of(ANY_TEXT, st.text(alphabet=ID_CHARS, max_size=30)))
def test_parse_either_accepts_or_raises_invalid_id(raw):
    try:
        scheme, code = parse(raw)
    except InvalidId:
        return
    # Whatever is accepted round-trips through its canonical form.
    assert split(canonical(scheme, code)) == (scheme, code)
    assert parse(canonical(scheme, code)) == (scheme, code)


@given(st.from_regex(r"[A-Z]{2}[A-Z0-9]{9}", fullmatch=True))
def test_isin_check_digit_is_unique(body):
    good = body + isin_check_digit(body)
    assert is_valid_isin(good) and parse(good) == ("isin", good)
    assert sum(is_valid_isin(body + d) for d in string.digits) == 1


@given(ANY_TEXT)
def test_normalize_is_stable_and_plain(text):
    n = normalize(text)
    assert normalize(n) == n
    assert n == n.strip() and "  " not in n
    assert not any(c in string.ascii_uppercase for c in n)
    assert all(c.isalnum() or c == " " for c in n)


@given(st.sampled_from(sorted(MINOR_UNITS)), st.floats(min_value=0.0001, max_value=1e9))
def test_minor_units_divide_by_their_factor(code, price):
    major, factor = MINOR_UNITS[code]
    assert to_major(price, code) == (price / factor, major)


@given(st.text(alphabet=string.ascii_uppercase, min_size=3, max_size=3), st.floats(allow_nan=True, allow_infinity=True))
def test_major_currencies_are_untouched(code, price):
    assume(code not in MINOR_UNITS)
    p, c = to_major(price, code)
    assert c == code and (p == price or (math.isnan(p) and math.isnan(price)))


@given(st.one_of(ANY_TEXT, st.dates().map(str), st.none()),
       st.one_of(st.floats(allow_nan=True, allow_infinity=True), st.integers(-10, 10)))
def test_valid_point_never_raises(day, price):
    ok = _valid_point(day, price)
    if ok:
        assert date.fromisoformat(str(day)[:10]) and math.isfinite(price) and price > 0


@given(st.lists(st.tuples(st.dates(min_value=date(2000, 1, 1), max_value=date(2030, 1, 1)),
                          st.floats(min_value=0.01, max_value=1e6)), max_size=50),
       st.dates(min_value=date(2000, 1, 1), max_value=date(2030, 1, 1)), st.integers(0, 4000))
def test_in_range_is_sorted_bounded_subset(points, start, days):
    end = start + timedelta(days=days)
    entries = [NavEntry(d.isoformat(), v) for d, v in points]
    out = in_range(entries, start.isoformat(), end.isoformat())
    assert [e.date for e in out] == sorted(e.date for e in out)
    assert all(start.isoformat() <= e.date <= end.isoformat() for e in out)
    assert len(out) == sum(start.isoformat() <= e.date <= end.isoformat() for e in entries)


@pytest.fixture(scope="module")
def catalogue():
    c = Client(cache=False, sources=[], fx_providers=[])
    c._cache.upsert_instruments([
        {"id": "ES0112611001", "name": "Azvalor Internacional FI", "type": "fund", "ticker": "AZVI"},
        {"id": "cn:110022", "name": "易方达消费行业股票", "type": "fund"},
        {"id": "vn-fmarket:VFF", "name": "QUỸ ĐẦU TƯ TRÁI PHIẾU", "type": "fund"},
        {"id": "N5508", "name": "Santander Autónomos RVM PPES", "type": "plan"},
    ])
    return c


@given(ANY_TEXT)
def test_offline_search_accepts_any_text(catalogue, text):
    results = catalogue.search(text, live=False, limit=5)
    assert isinstance(results, list) and len(results) <= 5


@given(st.sampled_from(["Azvalor Internacional FI", "易方达消费行业股票", "QUỸ ĐẦU TƯ TRÁI PHIẾU",
                        "Santander Autónomos RVM PPES"]), st.data())
def test_any_subset_of_a_names_words_finds_it(catalogue, name, data):
    words = name.split()
    picked = data.draw(st.lists(st.sampled_from(words), min_size=1, max_size=len(words), unique=True))
    query = " ".join(data.draw(st.permutations(picked)))
    query = data.draw(st.sampled_from([query, query.upper(), query.lower()]))
    assert name in [i.name for i in catalogue.search(query, live=False, limit=50)]


@given(st.floats(min_value=0.01, max_value=1e6), st.floats(min_value=0.001, max_value=1e5),
       st.floats(min_value=0.001, max_value=1e5))
def test_converting_there_and_back_returns_the_original(price, usd_per_eur, inr_per_eur):
    rates = FakeRates({"USD": {"2026-10-23": usd_per_eur}, "INR": {"2026-10-23": inr_per_eur}})
    c = Client(cache=False, sources=[Fake("morningstar", quote=q(price, d="2026-10-23", ccy="USD"))],
               fx_providers=[rates], max_wait=None)
    c._cache.upsert_instruments([{"id": "IE00B4L5Y983", "name": "ETF", "type": "etf", "currency": "USD"}])
    there = c.quote("IE00B4L5Y983", currency="INR")
    convert, _ = c._fx.converter("INR", "USD", "2026-10-23", "2026-10-23")
    back = there.price * convert("2026-10-23")[0]
    assert back == pytest.approx(price, rel=1e-9)
    c.close()
