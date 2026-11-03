"""Full NAV histories that Indonesian fund managers publish on their own websites.

Pasardana only shows the last 12 months; these managers chart every day since launch on each fund's public page.
None of their pages carries a usable ISIN, so funds are matched to Pasardana's list by name.
"""
import json
import re
from collections.abc import Callable
from datetime import datetime

import httpx

from ..cache import normalize

Get = Callable[..., httpx.Response]

_MONTHS_ID = {"mei": "may", "agu": "aug", "agt": "aug", "okt": "oct", "des": "dec", "nop": "nov"}


def match_name(name: str) -> str:
    """'Reksa Dana Syailendra Dana Kas (SDK)' and 'SYAILENDRA DANA KAS' -> 'syailendra dana kas'."""
    text = normalize(re.sub(r"\([^)]*\)", " ", name.split("|")[0]))
    text = re.sub(r"\breksa ?dana\b|\brd\b", " ", text)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def manager_key(name: str, provider: str) -> str:
    """match_name() without whatever precedes the manager's name ('syariah trimegah ...' -> 'trimegah ...')."""
    text = match_name(name)
    at = text.find(provider)
    return text[at:] if at > 0 else text


def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).replace("&amp;", "&").split())


def _ok(r: httpx.Response) -> httpx.Response:
    r.raise_for_status()
    return r


def _points(pairs, day_format: str = "%Y-%m-%d") -> dict[str, float]:
    out = {}
    for day, value in pairs:
        try:
            d = datetime.strptime(str(day).strip(), day_format).strftime("%Y-%m-%d")
            v = float(value)
        except (TypeError, ValueError):
            continue
        if v > 0:
            out[d] = v
    return out


class Manager:
    name: str
    provider: str  # lowercase text found in Pasardana's InvestmentManagerName

    def catalogue(self, get: Get) -> dict[str, str]:
        """{fund name: key}."""
        raise NotImplementedError

    def history(self, get: Get, key: str) -> dict[str, float]:
        """{date: NAV per unit} since launch."""
        raise NotImplementedError


class Batavia(Manager):
    name, provider = "batavia", "batavia"
    BASE = "https://bpam.co.id"

    def catalogue(self, get: Get) -> dict[str, str]:
        out = {}
        retail = _ok(get(f"{self.BASE}/produk")).text
        for url, label in re.findall(r'href="(https://bpam\.co\.id/product/[^"]+)".*?products-card__label">([^<]+)<',
                                     retail, re.S):
            out[label.strip()] = url
        # Institutional funds have no page; their code is in the prospectus file name.
        institutional = _ok(get(f"{self.BASE}/produk/institusi")).text
        for code, label in re.findall(r'PROSP-([A-Z0-9-]+)-ID\.pdf".*?products-card__label">([^<]+)<', institutional, re.S):
            out.setdefault(label.strip(), code)
        return out

    def history(self, get: Get, key: str) -> dict[str, float]:
        code = key
        if key.startswith("https://"):
            m = re.search(r'banner-subtitle">\s*([A-Z0-9-]+)\s*<', _ok(get(key)).text)
            if not m:
                return {}
            code = m.group(1)
        data = json.loads(_ok(get(f"{self.BASE}/nav/get_navs_json", params={"code": code})).text or "null")
        rows = data.get("data") if isinstance(data, dict) else None
        return _points((x.get("date"), x.get("value")) for x in rows or [] if isinstance(x, dict))


class Trimegah(Manager):
    name, provider = "trimegah", "trimegah"
    BASE = "https://www.trimegah-am.com/id/reksadana"

    def catalogue(self, get: Get) -> dict[str, str]:
        page = _ok(get(self.BASE)).text
        return {_text(label): code for code, label in
                re.findall(r'href="[^"]*/reksadana/detail/([A-Za-z0-9_-]+)".*?class="card-title"[^>]*>([^<]+)<', page, re.S)}

    def history(self, get: Get, key: str) -> dict[str, float]:
        page = _ok(get(f"{self.BASE}/detail/{key}")).text
        m = re.search(r"initReksadanaChart\(\s*'container_nav_chart'[^{]*(\{.*?\})\s*\)", page, re.S)
        if not m:
            return {}
        try:
            series = json.JSONDecoder().raw_decode(m.group(1))[0].get("ALL") or {}
        except (ValueError, AttributeError):
            return {}
        labels = [_english_month(x) for x in series.get("labels") or []]
        return _points(zip(labels, series.get("data") or [], strict=False), "%d %b %Y")


def _english_month(label) -> str:
    parts = str(label).split()
    if len(parts) == 3:
        parts[1] = _MONTHS_ID.get(parts[1].lower(), parts[1])
    return " ".join(parts)


class Avrist(Manager):
    name, provider = "avrist", "avrist"
    API = "https://avrist-am.com/api-front/api"

    def catalogue(self, get: Get) -> dict[str, str]:
        data = _ok(get(f"{self.API}/content/Reksa-Dana", params={"includeAttributes": "true"})).json()
        items = (data.get("data") or {}).get("contentDataList") if isinstance(data, dict) else None
        out: dict[str, str] = {}
        for item in items or []:
            if not isinstance(item, dict):
                continue
            fields = {f.get("fieldId"): f.get("value") for f in item.get("contentData") or [] if isinstance(f, dict)}
            code, title = fields.get("produk-id"), item.get("title")
            if isinstance(code, str) and isinstance(title, str):
                out[title.strip()] = code
        return out

    def history(self, get: Get, key: str) -> dict[str, float]:
        today = datetime.now().strftime("%Y-%m-%d")
        data = _ok(get(f"{self.API}/reksa-dana/nav-list/{key}/1990-01-01/{today}")).json()
        rows = (data.get("data") or {}).get("history") if isinstance(data, dict) else None
        # The last point sometimes comes as 0.0; _points drops it.
        return _points((x.get("date"), x.get("nav")) for x in rows or [] if isinstance(x, dict))


class Syailendra(Manager):
    name, provider = "syailendra", "syailendra"
    BASE = "https://www.syailendracapital.com/product"

    def catalogue(self, get: Get) -> dict[str, str]:
        page = _ok(get(f"{self.BASE}/reksa-dana-saham")).text  # every category page lists all funds in its menu
        return {_text(label): url for url, label in
                re.findall(r'<a href="(https://www\.syailendracapital\.com/product/reksa-dana-[a-z-]+/[a-z0-9-]+)">([^<]+)</a>',
                           page)}

    def history(self, get: Get, key: str) -> dict[str, float]:
        m = re.search(r"dataMaster\s*=\s*(\[\[.*?\]\])\s*;", _ok(get(key)).text, re.S)
        try:
            rows = json.loads(m.group(1)) if m else []
        except ValueError:
            return {}
        return _points(tuple(x[:2]) for x in rows if isinstance(x, list) and len(x) >= 2)


# Principal (principal.co.id) also publishes full histories, but its host refuses non-browser clients.
MANAGERS: tuple[Manager, ...] = (Batavia(), Trimegah(), Avrist(), Syailendra())
