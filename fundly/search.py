"""Name-search routing and ranking.

Routing decides which sources a text query goes to, so a query does not cost one request (or one large
catalogue download) per source:

- Text in a non-Latin script goes to the sources that publish names in that script (Chinese → Eastmoney,
  Hebrew → Israel, Vietnamese → fmarket).
- Latin text goes to the worldwide sources, plus national sources whose hint words appear in the query
  ("afp" → Chile, "reksa dana" → Indonesia, "hdfc" → India...).
- If that first wave finds nothing that contains every word of the query, the remaining national sources
  are asked as a second wave.
- A type filter leaves out sources that don't carry that type.
"""
import unicodedata

from .cache import normalize

GLOBAL, NATIONAL, NONE = "global", "national", "none"

# Words people search with that no fund name contains, mapped to one that does. Mexican pension funds are
# SIEFOREs run by AFOREs, and their names are codes like "PROF-70 A-I" whose category says "Siefore".
SYNONYMS = {"afore": "siefore", "afores": "siefore"}


def rewrite(q: str) -> str:
    """The query with SYNONYMS replaced, word by word (other words are kept as typed)."""
    words = q.split()
    return " ".join(SYNONYMS.get(normalize(w), w) for w in words) if words else q


def scripts_in(text: str) -> set[str]:
    """Distinctive non-Latin scripts in the text (accented Latin letters don't count, except Vietnamese)."""
    found = set()
    for ch in text:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
            found.add("han")
        elif 0x0590 <= o <= 0x05FF:
            found.add("hebrew")
        elif 0x1EA0 <= o <= 0x1EF9 or ch in "ơưđƠƯĐ":  # letters used only in Vietnamese
            found.add("vietnamese")
        elif 0x3040 <= o <= 0x30FF:
            found.add("kana")
        elif 0xAC00 <= o <= 0xD7AF:
            found.add("hangul")
        elif unicodedata.category(ch).startswith("L") and o > 0x024F and "LATIN" not in unicodedata.name(ch, ""):
            found.add("other")
    return found


def plan(sources, q: str, type: str | None = None) -> tuple[list, list]:
    """-> (first wave, second wave) of sources to ask for a text query."""
    candidates = [s for s in sources if s.search_scope != NONE and (not type or type in s.types)]
    scripts = scripts_in(q)
    if scripts:
        specialised = [s for s in candidates if s.search_scripts & scripts]
        return (specialised or [s for s in candidates if s.search_scope == GLOBAL]), []
    words = set(normalize(q).split())
    first = [s for s in candidates if s.search_scope == GLOBAL or s.search_hints & words]
    return first, [s for s in candidates if s not in first]


def match_rank(qn: str, name: str | None, search_text: str) -> int:
    """How well a catalogue entry matches a normalised query; lower is better."""
    tokens = qn.split()
    if not tokens:
        return 9
    n = normalize(name)
    if n == qn:
        return 1
    if n.startswith(qn + " "):
        return 2
    text = f" {search_text} "
    if f" {qn} " in text:
        return 3
    words = search_text.split()
    if all(t in words for t in tokens):
        return 4
    if all(any(w.startswith(t) for w in words) for t in tokens):
        return 5
    if all(t in search_text for t in tokens):
        return 6
    return 9  # a live hit that a source considered relevant but doesn't contain every word
