import re
from datetime import date

from ..cache import normalize
from ..ids import canonical, register
from .base import Cached, ETF, FUND, PLAN, HistoryResult, Inst, NavEntry, QuoteResult, Source, in_range

SCHEME = "tr-tefas"
register(SCHEME, re.compile(r"^[A-Z0-9]{2,5}$").match, upper=True)

API = "https://www.tefas.gov.tr/api/funds"
PRICES_URL = f"{API}/fonFiyatBilgiGetir"
LIST_URL = f"{API}/fonGetiriBazliBilgiGetir"
# The price call only takes these look-backs, in months; anything else answers "Sistem Hatası!!".
PERIODS = (1, 3, 6, 12, 36, 60)
DAYS_PER_MONTH = 30.4
LIST_TTL = 6 * 3600
# fonTipi: investment funds, BES private-pension funds, exchange-traded funds.
KINDS = {"YAT": FUND, "EMK": PLAN, "BYF": ETF}
# The list call wants the same filter body the site's own fund search sends.
LIST_BODY = {
    "dil": "TR", "kurucuKodu": None, "sfonTurKod": None, "fonTurAciklama": None, "islem": 1, "fonTurKod": None,
    "fonGrubu": None, "donemGetiri1a": "1", "donemGetiri3a": "1", "donemGetiri6a": "1", "donemGetiri1y": "1",
    "donemGetiriyb": "1", "donemGetiri3y": "1", "donemGetiri5y": "1", "basTarih": None, "bitTarih": None,
    "calismaTipi": 2, "getiriOrani": "1",
}


def months_back(start: str, today: date | None = None) -> int:
    """The smallest look-back the API accepts that reaches `start` (5 years at most). A few days are kept
    in hand because a period counts calendar months back from today."""
    days = max(0, ((today or date.today()) - date.fromisoformat(start)).days)
    return next((p for p in PERIODS if p * DAYS_PER_MONTH - 3 >= days), PERIODS[-1])


class TEFASSource(Source):
    """Turkish investment funds, BES private-pension funds and exchange-traded funds, from TEFAS, the official
    fund platform run by Takasbank (prices in TRY, same day). History goes back 5 years at most.

    Only TEFAS's JSON API answers fundly: www.tefas.gov.tr's web pages, robots.txt included, reject non-browser
    clients, so its terms of use can't be read from here.
    """

    name = "tefas"
    search_scope = "national"
    search_hints = frozenset({"turkey", "turkish", "turkiye", "tefas", "portfoy", "fon", "fonu", "emeklilik", "bes",
                              "katilim", "yapi", "kredi", "garanti", "akbank", "is", "ziraat", "qnb", "deniz",
                              "anadolu", "agesa", "allianz", "tl", "try"})
    types = frozenset({FUND, PLAN, ETF})
    schemes = frozenset({SCHEME})
    timeout = 60

    def __init__(self, min_interval: float | None = None):
        super().__init__(min_interval)
        self._list = Cached(self._load_list, LIST_TTL)

    def _post(self, url: str, **kw):
        r = super()._post(url, **kw)
        r.raise_for_status()
        data = r.json()
        rows = data.get("resultList") if isinstance(data, dict) else None
        if rows is None and isinstance(data, dict) and data.get("errorMessage"):
            raise ValueError(f"tefas: {data['errorMessage']}")
        return rows if isinstance(rows, list) else []

    def _load_list(self) -> dict[str, dict]:
        """{fund code: {name, type, category}} for every fund TEFAS lists."""
        funds: dict[str, dict] = {}
        for kind, type_ in KINDS.items():
            for x in self._post(LIST_URL, json={**LIST_BODY, "fonTipi": kind}):
                if not isinstance(x, dict):
                    continue
                code, name = str(x.get("fonKodu") or "").strip().upper(), str(x.get("fonUnvan") or "").strip()
                if code and name:
                    funds.setdefault(code, {"name": name, "type": type_, "category": x.get("fonTurAciklama")})
        return funds

    def _prices(self, code: str, months: int) -> list[NavEntry]:
        rows = self._post(PRICES_URL, json={"fonKodu": code, "dil": "TR", "periyod": months})
        entries = []
        for x in rows:
            if not isinstance(x, dict) or str(x.get("fonKodu") or "").upper() != code:
                continue
            day, price = str(x.get("tarih") or "")[:10], x.get("fiyat")
            if len(day) == 10 and isinstance(price, int | float) and price > 0:
                entries.append(NavEntry(day, float(price)))
        return sorted(entries, key=lambda e: e.date)

    def get_quote(self, inst: Inst) -> QuoteResult | None:
        # A fund that hasn't priced for a month is looked up over a year before giving up.
        entries = self._prices(inst.code, 1) or self._prices(inst.code, 12)
        if not entries:
            return None
        return QuoteResult(nav=entries[-1].nav, date=entries[-1].date, currency="TRY", source=self.name)

    def get_history(self, inst: Inst, start: str, end: str) -> HistoryResult | None:
        entries = in_range(self._prices(inst.code, months_back(start)), start, end)
        return HistoryResult(entries=entries, currency="TRY", source=self.name) if entries else None

    def _hit(self, code: str, f: dict) -> dict:
        return {"id": canonical(SCHEME, code), "name": f["name"], "type": f["type"], "currency": "TRY",
                "category": f["category"]}

    def search(self, q: str) -> list[dict]:
        words = normalize(q).split()
        if not words:
            return []
        return [self._hit(code, f) for code, f in self._list.get().items()
                if all(w in normalize(f"{f['name']} {code}") for w in words)][:30]

    def resolve(self, scheme: str, code: str) -> dict | None:
        if scheme != SCHEME:
            return None
        f = self._list.get().get(code.upper())
        return self._hit(code.upper(), f) if f else None
