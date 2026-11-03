"""Search routing (which sources a query goes to) and ranking."""
import pytest

from fundly.cache import normalize
from fundly.search import match_rank, plan, scripts_in
from fundly.sources.base import Source
from test_client import Fake, client


class Src(Source):
    def __init__(self, name, scope, types=("fund", "etf"), scripts=(), hints=()):
        super().__init__(min_interval=0)
        self.name, self.search_scope, self.types = name, scope, frozenset(types)
        self.search_scripts, self.search_hints = frozenset(scripts), frozenset(hints)


SOURCES = [
    Src("morningstar", "global"), Src("finect", "global", types=("fund", "etf", "plan")),
    Src("eastmoney", "national", scripts=("han",), hints=("china",)),
    Src("gemelnet", "national", types=("plan",), scripts=("hebrew",), hints=("israel",)),
    Src("fmarket", "national", scripts=("vietnamese",), hints=("vietnam", "quy")),
    Src("cl-afp", "national", types=("plan",), hints=("afp", "chile", "habitat")),
    Src("cvm", "national", hints=("brasil", "previdencia", "fundo")),
    Src("ft", "none"),
]


def names(srcs):
    return [s.name for s in srcs]


@pytest.mark.parametrize("q, first, second", [
    ("azvalor internacional", ["morningstar", "finect"], ["eastmoney", "gemelnet", "fmarket", "cl-afp", "cvm"]),
    ("AFP Habitat fondo A", ["morningstar", "finect", "cl-afp"], ["eastmoney", "gemelnet", "fmarket", "cvm"]),
    ("易方达消费", ["eastmoney"], []),
    ("מיטב השתלמות", ["gemelnet"], []),
    ("Quỹ đầu tư trái phiếu", ["fmarket"], []),
    ("quy dau tu", ["morningstar", "finect", "fmarket"], ["eastmoney", "gemelnet", "cl-afp", "cvm"]),
    ("previdência privada", ["morningstar", "finect", "cvm"], ["eastmoney", "gemelnet", "fmarket", "cl-afp"]),
    ("Сбербанк", ["morningstar", "finect"], []),  # a script nobody specialises in: worldwide sources only
])
def test_plan_routes_by_script_and_hints(q, first, second):
    a, b = plan(SOURCES, q)
    assert (names(a), names(b)) == (first, second)


def test_type_filter_leaves_out_sources_without_that_type():
    a, b = plan(SOURCES, "santander autonomos", type="plan")
    assert names(a) == ["finect"] and names(b) == ["gemelnet", "cl-afp"]


@pytest.mark.parametrize("text, expected", [
    ("Azvalor", set()), ("Multigestión ação previdência", set()), ("易方达", {"han"}), ("מיטב", {"hebrew"}),
    ("Quỹ", {"vietnamese"}), ("đầu tư", {"vietnamese"}), ("ひらがな", {"kana"}), ("한국", {"hangul"}),
    ("Сбербанк", {"other"}),
])
def test_scripts_in(text, expected):
    assert scripts_in(text) == expected


@pytest.mark.parametrize("name, rank", [
    ("Azvalor Internacional", 1),             # exact name
    ("Azvalor Internacional FI", 2),          # starts with the query
    ("Lux SICAV Azvalor Internacional R", 3), # the phrase, as whole words
    ("Internacional Azvalor", 4),             # every word, other order
    ("Azvalor Internacionales", 5),           # every word as a word prefix
    ("XAzvalorX Internacional", 6),           # every word somewhere
    ("Azvalor Blue Chips", 9),                # not every word
])
def test_match_rank(name, rank):
    assert match_rank("azvalor internacional", name, normalize(name)) == rank


def test_exact_name_and_prefix_beat_a_bigger_fund_that_only_contains_the_words():
    hits = [
        {"id": "LU0000000001", "name": "Global Fund Santander Autonomos Mixed", "type": "fund", "size_eur": 10**9},
        {"id": "N5508", "name": "Santander Autonomos RVM PPES", "type": "plan"},
    ]
    c = client(Fake("finect", types=("fund", "etf", "plan"), hits=hits))
    assert [i.id for i in c.search("santander autonomos")] == ["N5508", "LU0000000001"]


class Counting(Fake):
    def __init__(self, *a, scope="global", hints=(), **kw):
        super().__init__(*a, **kw)
        self.search_scope, self.search_hints = scope, frozenset(hints)
        self.queries = []

    def search(self, query):
        self.queries.append(query)
        return super().search(query)

    def resolve(self, scheme, code):
        self.calls["resolve"] = self.calls.get("resolve", 0) + 1
        if (scheme, code) == ("isin", "ES0112611001"):
            return {"id": "ES0112611001", "name": "Azvalor Internacional FI", "type": "fund", "currency": "EUR"}
        if (scheme, code) == ("ticker", "VOO"):
            return {"id": "US9229083632", "name": "Vanguard S&P 500 ETF", "type": "etf", "currency": "USD"}
        return None


def test_an_isin_is_looked_up_directly_without_text_search():
    ms = Counting("morningstar")
    national = Counting("cvm", scope="national")
    results = client(ms, national).search("ES0112611001")
    assert [i.id for i in results] == ["ES0112611001"]
    assert ms.queries == [] and national.queries == [] and ms.calls["resolve"] == 1


def test_an_unknown_id_never_reaches_national_catalogues():
    finect = Counting("finect", types=("fund", "etf", "plan"))
    brazil = Counting("cvm", scope="national")
    c = client(finect, brazil)
    assert c.search("N9999") == [] and c.search("cl-afp:HABITAT-A") == []
    assert brazil.queries == []
    assert len(finect.queries) <= 2  # at most the code itself, as a last-resort lookup


def test_an_upper_case_word_is_also_tried_as_a_ticker():
    ms = Counting("morningstar")
    results = client(ms).search("VOO")
    assert results[0].id == "US9229083632" and ms.queries == ["VOO"]


def test_national_sources_are_a_second_wave_only_when_the_first_has_no_full_match():
    world = Counting("morningstar", hits=[{"id": "LU0000000001", "name": "Nordic Equity", "type": "fund"}])
    brazil = Counting("cvm", scope="national",
                      hits=[{"id": "br-cvm:00000000000001", "name": "Fundo Exemplo Acoes", "type": "fund"}])
    c = client(world, brazil)
    c.search("nordic equity")
    assert brazil.queries == []  # the first wave already matched every word
    c.search("exemplo acoes")
    assert brazil.queries == ["exemplo acoes"]  # nothing matched in the first wave: second wave


def test_hint_words_send_national_sources_in_the_first_wave():
    world = Counting("morningstar", hits=[{"id": "LU0000000001", "name": "Habitat Global", "type": "fund"}])
    chile = Counting("cl-afp", scope="national", types=("plan",), hints=("afp", "habitat"))
    client(world, chile).search("afp habitat")
    assert chile.queries == ["afp habitat"]


def test_the_search_log_is_per_type():
    finect = Counting("finect", types=("fund", "etf", "plan"))
    c = client(finect)
    c.search("santander autonomos")
    c.search("santander autonomos", type="plan")
    c.search("santander autonomos", type="plan")
    assert len(finect.queries) == 2




def test_a_morningstar_only_entry_with_the_same_name_is_folded_into_the_public_id():
    ms = Fake("morningstar", hits=[
        {"id": "ms:F00001I2ZO", "name": "Santander Autónomos RFM PPES", "type": "fund", "size_eur": 16_788_785,
         "morningstar_id": "F00001I2ZO"},
        {"id": "ms:F000000001", "name": "Some Other Fund", "type": "fund"},
    ])
    finect = Fake("finect", types=("fund", "etf", "plan"), hits=[
        {"id": "N5507", "name": "Santander Autónomos RFM PPES", "type": "plan", "provider": "Santander Pensiones EGFP"},
    ])
    c = client(ms, finect)
    found = c.search("santander autonomos")
    assert found[0].id == "N5507" and "ms:F00001I2ZO" not in [i.id for i in found]
    assert (found[0].type, found[0].size_eur, found[0].provider) == ("plan", 16_788_785, "Santander Pensiones EGFP")
    assert c.instrument("ms:F00001I2ZO").id == "N5507"
    assert c.search("other fund")[0].id == "ms:F000000001"  # no twin: kept as it is
