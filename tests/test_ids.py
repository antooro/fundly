import pytest

from fundly import Client, InvalidId, UnknownSource
from fundly.ids import canonical, is_valid_isin, isin_check_digit, parse, split
from test_client import FUND_ID, Fake, client, q


@pytest.mark.parametrize("raw, expected", [
    ("IE00B4L5Y983", ("isin", "IE00B4L5Y983")),
    (" ie00b4l5y983 ", ("isin", "IE00B4L5Y983")),
    ("isin:IE00B4L5Y983", ("isin", "IE00B4L5Y983")),
    ("N5508", ("dgs", "N5508")),
    ("es-dgs:n5508", ("dgs", "N5508")),
    ("S0156", ("dgs", "S0156")),
    ("s0156", ("dgs", "S0156")),
    ("VTSAX", ("ticker", "VTSAX")),
    ("vtsax", ("ticker", "VTSAX")),
    ("ticker:brk.b", ("ticker", "BRK.B")),
    ("ms:F00001H9R0", ("ms", "F00001H9R0")),
])
def test_parse(raw, expected):
    assert parse(raw) == expected


@pytest.mark.parametrize("raw", ["", "!!", "far too long to be a ticker", "nope:ABC", "isin:ES011", "dgs:5508",
                                 "ms:short", "ticker:"])
def test_parse_rejects(raw):
    with pytest.raises(InvalidId):
        parse(raw)


def test_canonical_and_split_round_trip():
    for scheme, code in [("isin", "IE00B4L5Y983"), ("dgs", "N5508"), ("ticker", "VTSAX"), ("ms", "F00001H9R0")]:
        assert split(canonical(scheme, code)) == (scheme, code)
    assert canonical("isin", "IE00B4L5Y983") == "IE00B4L5Y983" and canonical("ticker", "VOO") == "ticker:VOO"


@pytest.mark.parametrize("isin", ["IE00B4L5Y983", "US9229087286", "ES0112611001", "AU60VAN00022", "INF179K01XQ0"])
def test_isin_check_digit(isin):
    assert isin_check_digit(isin[:11]) == isin[11] and is_valid_isin(isin)
    assert not is_valid_isin(isin[:11] + str((int(isin[11]) + 1) % 10))


def test_ticker_is_translated_to_isin_once_and_remembered():
    class Resolver(Fake):
        def resolve(self, scheme, code):
            self.calls["resolve"] = self.calls.get("resolve", 0) + 1
            if (scheme, code) == ("ticker", "VTSAX"):
                return {"id": "US9229087286", "name": "Vanguard Total Stock Mkt Idx Adm", "type": "fund",
                        "currency": "USD", "ticker": "VTSAX"}
            return None

    ms = Resolver("morningstar", quote=q(182.82, ccy="USD"))
    c = client(ms)
    assert c.quote("VTSAX").id == "US9229087286"
    assert c.quote("ticker:vtsax").cached is True
    assert c.instrument("US9229087286").identifiers["ticker"] == "VTSAX"
    assert c.search("VTSAX", live=False)[0].id == "US9229087286"
    assert ms.calls["resolve"] == 1


def test_unresolvable_alias_still_reaches_sources_that_speak_it():
    yahoo = Fake("yahoo", quote=q(100.0, ccy="USD", src="yahoo", pt="market"))
    yahoo.schemes = frozenset({"isin", "ticker"})
    r = client(yahoo).quote("ZZZZ")
    assert (r.id, r.source) == ("ticker:ZZZZ", "yahoo")


def test_sources_only_get_ids_in_schemes_they_understand():
    isin_only = Fake("ft", quote=q(1.0, src="ft"))
    isin_only.schemes = frozenset({"isin"})
    ms = Fake("morningstar", quote=q(2.0))
    r = client(isin_only, ms).quote("ms:F00001H9R0")
    assert r.source == "morningstar" and isin_only.calls["quote"] == 0


def test_exclude_sources():
    c = Client(cache=False, sources=[Fake("morningstar", quote=q(1.0)), Fake("ft", quote=q(2.0, src="ft"))],
               exclude=["morningstar"])
    assert list(c.sources) == ["ft"]
    with pytest.raises(UnknownSource):
        Client(cache=False, sources=[Fake("ft")], exclude=["nope"])


def test_cache_from_an_older_schema_is_rebuilt(tmp_path):
    import sqlite3
    conn = sqlite3.connect(tmp_path / "fundly.db")
    conn.execute("CREATE TABLE instruments (id TEXT PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO instruments VALUES ('X', 'old')")
    conn.commit()
    conn.close()
    with Client(cache=tmp_path, sources=[Fake("morningstar", quote=q(1.0))]) as c:
        c._cache.upsert_instruments([{"id": FUND_ID, "name": "New", "type": "fund", "ticker": "NEW"}])
        assert [i.id for i in c.search(live=False)] == [FUND_ID]
