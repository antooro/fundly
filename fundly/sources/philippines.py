import html
import json
import re
import threading
import time
from datetime import date

from .. import ids
from ..cache import normalize
from .base import FUND, HistoryResult, Inst, NavEntry, QuoteResult, Source, in_range

SCHEME = "ph-uitf"
ids.register(SCHEME, re.compile(r"^\d{1,6}$").match)

BASE = "https://www.uitf.com.ph"
BANK_URL = f"{BASE}/daily_navpu.php"
# Trust departments known on 3 Nov 2026, used when the bank menu can't be read.
BANKS = {"29": "AB Capital Asset Management", "42": "AIA Investment Management and Trust Corporation Philippines",
         "4": "Asia United Bank", "31": "ATRAM Trust Corporation", "5": "Bank of Commerce", "6": "BDO Unibank, Inc.",
         "3": "BPI Wealth", "7": "China Banking Corporation", "36": "CTBC Bank (Philippines) Corp.",
         "26": "Development Bank of the Philippines", "8": "EastWest Banking Corporation",
         "9": "LandBank of the Philippines", "39": "Manulife Investment Management and Trust Corporation",
         "40": "Maybank Philippines Inc.", "1": "Metropolitan Bank & Trust Co.", "10": "Philippine Bank of Communications",
         "30": "Philippine Business Bank", "2": "Philippine National Bank", "32": "Philippine Savings Bank",
         "11": "RCBC Trust Corporation", "34": "Robinsons Bank", "12": "Security Bank Corporation",
         "33": "Sterling Bank of Asia", "43": "Sun Life Investment Management and Trust Corporation", "13": "Union Bank",
         "14": "United Coconut Planters Bank"}
# Each table of a bank page sits under a currency icon.
CURRENCY_ICONS = {"philippine-peso": "PHP", "dollar": "USD"}
PAGE_TTL = 3600
CATALOGUE_TTL = 7 * 86400

_DATE = r"([A-Z][a-z]+)\.?\s+(\d{1,2}),\s+(\d{4})"
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def _date(month: str, day: str, year: str) -> str | None:
    try:
        return date(int(year), _MONTHS[month[:3].lower()], int(day)).isoformat()
    except (KeyError, ValueError):
        return None


def _parse_bank_page(page: str, bank: str) -> dict[str, dict]:
    """A bank's daily NAVpu page -> {fund id: {name, bank, currency, category, nav, date}}.

    The page date is in its heading ("UITF NAVpus as of Nov 3, 2026"); a row published on another day says so
    next to its NAVpu ("* as of Nov 2, 2026"). Funds with no date or a zero NAVpu keep nav = None."""
    page = re.sub(r"<!--.*?-->", "", page, flags=re.S)
    heading = re.search(r"\bas of\s+" + _DATE + r"\s*<a", page)
    page_day = _date(*heading.groups()) if heading else None
    funds: dict[str, dict] = {}
    for section in re.split(r'<div class="table-title', page)[1:]:
        icon = re.search(r"images/icons/([a-z-]+)\.png", section)
        currency = CURRENCY_ICONS.get(icon.group(1)) if icon else None
        title = re.search(r"<h3[^>]*>([^<]*)</h3>", section)
        for fund_id, name, cell in re.findall(
                r'<a href="daily_navpu_details\.php\?fund_id=(\d+)&(?:amp;)?bank_id=\d+">([^<]*)</a>\s*</td>\s*<td>(.*?)</td>',
                section, re.S):
            value = re.match(r"\s*([\d,]*\.?\d+)", cell)
            row_day = re.search(r"as of\s+" + _DATE, cell)
            day = _date(*row_day.groups()) if row_day else page_day
            nav = float(value.group(1).replace(",", "")) if value else None
            funds[fund_id] = {
                "name": html.unescape(name).strip(), "bank": bank, "currency": currency,
                "category": html.unescape(title.group(1)).strip() if title else None,
                "nav": nav if nav and day and currency else None, "date": day,
            }
    return funds


class UITFSource(Source):
    """Unit investment trust funds (UITFs) of Philippine banks and trust companies, from the daily NAVpu pages of
    the Trust Officers Association of the Philippines (uitf.com.ph). Not the mutual funds: ICAP's site blocks
    automated clients.

    Latest NAVpu only: the site keeps past values behind a reCAPTCHA, which fundly doesn't get around. With a
    cache folder, every bank page downloaded is kept on disk, so each machine builds its own history from the day
    it first asked. Finding which bank runs a fund means reading every bank's page once (~26 requests, about a
    minute); that list is kept for a week."""

    name = "uitf"
    search_scope = "national"
    search_hints = frozenset({"uitf", "philippines", "philippine", "php", "bpi", "bdo", "metrobank", "landbank",
                              "rcbc", "chinabank", "pnb", "unionbank", "eastwest", "atram", "psbank", "ucpb", "dbp",
                              "aub", "ctbc", "sterling", "robinsons"})
    types = frozenset({FUND})
    schemes = frozenset({SCHEME})
    # robots.txt asks for 60 s between requests; fundly uses 2 s (leave the source out with exclude=["uitf"]).
    min_interval = 2.0
    timeout = 60

    def __init__(self, min_interval: float | None = None):
        super().__init__(min_interval)
        self._pages: dict[str, tuple[float, dict[str, dict]]] = {}
        self._catalogue_value: dict[str, dict] | None = None
        self._catalogue_at = 0.0
        self._catalogue_lock = threading.Lock()

    # --- pages and catalogue -------------------------------------------------------------------------

    def _bank_page(self, bank: str) -> dict[str, dict]:
        cached = self._pages.get(bank)
        if cached and time.monotonic() - cached[0] < PAGE_TTL:
            return cached[1]
        r = self._get(BANK_URL, params={"bank_id": bank})
        r.raise_for_status()
        funds = _parse_bank_page(r.text, bank)
        self._pages[bank] = (time.monotonic(), funds)
        self._keep(funds)
        return funds

    def _banks(self) -> dict[str, str]:
        r = self._get(BANK_URL)
        r.raise_for_status()
        menu = re.search(r'<select name="bank_id".*?</select>', r.text, re.S)
        found = {b: html.unescape(n).replace("\xa0", " ").strip()
                 for b, n in re.findall(r"<option value='(\d+)'>([^<]+)", menu.group(0) if menu else "")}
        return found or BANKS

    def _catalogue(self, build: bool = True) -> dict[str, dict]:
        """{fund id: {name, bank, bank_name, currency, category}}, from disk or memory if younger than a week."""
        with self._catalogue_lock:
            if self._catalogue_value is not None and time.monotonic() - self._catalogue_at < CATALOGUE_TTL:
                return self._catalogue_value
            path = self.files_dir / "catalogue.json" if self.files_dir else None
            if path is not None and path.exists() and time.time() - path.stat().st_mtime < CATALOGUE_TTL:
                try:
                    kept = json.loads(path.read_text())
                except ValueError:
                    kept = None
                if isinstance(kept, dict) and kept:
                    self._catalogue_value, self._catalogue_at = kept, time.monotonic()
                    return kept
            if not build:
                return {}
            catalogue = {}
            for bank, bank_name in self._banks().items():
                for fund_id, f in self._bank_page(bank).items():
                    catalogue[fund_id] = {"name": f["name"], "bank": bank, "bank_name": bank_name,
                                          "currency": f["currency"], "category": f["category"]}
            if catalogue and path is not None:
                self._write_atomic(path, json.dumps(catalogue).encode())
            self._catalogue_value, self._catalogue_at = catalogue, time.monotonic()
            return catalogue

    def _keep(self, funds: dict[str, dict]):
        """Each NAVpu downloaded goes into a file per day ({fund id: NAVpu}), the history this source can give."""
        if self.files_dir is None:
            return
        by_day: dict[str, dict[str, float]] = {}
        for fund_id, f in funds.items():
            if f["nav"]:
                by_day.setdefault(f["date"], {})[fund_id] = f["nav"]
        for day, values in by_day.items():
            kept = self._read_day(day)
            if not values.items() <= kept.items():
                self._write_atomic(self.files_dir / f"navpu-{day}.json", json.dumps({**kept, **values}).encode())

    def _read_day(self, day: str) -> dict[str, float]:
        path = self.files_dir / f"navpu-{day}.json" if self.files_dir else None
        try:
            data = json.loads(path.read_text()) if path is not None and path.exists() else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    # --- Source API ----------------------------------------------------------------------------------

    def _hit(self, fund_id: str, f: dict) -> dict:
        return {"id": ids.canonical(SCHEME, fund_id), "name": f["name"], "type": FUND, "currency": f["currency"],
                "provider": f["bank_name"], "category": f["category"]}

    def get_quote(self, inst: Inst) -> QuoteResult | None:
        f = self._catalogue().get(inst.code)
        if not f:
            return None
        row = self._bank_page(f["bank"]).get(inst.code)
        if not row or not row["nav"]:
            return None
        return QuoteResult(nav=row["nav"], date=row["date"], currency=row["currency"], source=self.name)

    def get_history(self, inst: Inst, start: str, end: str) -> HistoryResult | None:
        f = self._catalogue().get(inst.code)
        if not f or not f["currency"]:
            return None
        found: dict[str, float] = {}
        if self.files_dir is not None and self.files_dir.exists():
            for path in self.files_dir.glob("navpu-*.json"):
                day = path.stem[len("navpu-"):]
                if start <= day <= end and isinstance(value := self._read_day(day).get(inst.code), int | float):
                    found[day] = value
        row = self._bank_page(f["bank"]).get(inst.code)
        if row and row["nav"]:
            found[row["date"]] = row["nav"]
        entries = in_range([NavEntry(d, v) for d, v in found.items() if v > 0], start, end)
        return HistoryResult(entries=entries, currency=f["currency"], source=self.name) if entries else None

    def search(self, q: str) -> list[dict]:
        words = normalize(q).split()
        if not words:
            return []
        # Building the list costs a minute, so only a query meant for this source (a hint word) pays for it;
        # fallback searches use the list only if it is already there.
        catalogue = self._catalogue(build=bool(self.search_hints & set(words)))
        hits = []
        for fund_id, f in catalogue.items():
            text = normalize(f"{f['name']} {f['bank_name']} {f['category'] or ''} {f['currency'] or ''}")
            if all(w in text for w in words if w not in {"uitf", "philippines", "philippine"}):
                hits.append(self._hit(fund_id, f))
        return hits[:50]

    def resolve(self, scheme: str, code: str) -> dict | None:
        if scheme != SCHEME:
            return None
        f = self._catalogue().get(code)
        return self._hit(code, f) if f else None
