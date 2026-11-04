"""Full NAV histories that Indonesian fund managers publish on their own websites.

Pasardana only shows the last 12 months; these managers chart every day since launch on each fund's public page.
None of their pages carries a usable ISIN, so funds are matched to Pasardana's list by name.
"""
import html
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
    # (pattern, replacement) applied to both sides' names where the site and Pasardana spell them differently
    aliases: tuple[tuple[str, str], ...] = ()

    def key(self, name: str) -> str:
        text = manager_key(name, self.provider)
        for pattern, repl in self.aliases:
            text = re.sub(pattern, repl, text)
        return " ".join(text.split())

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
        # Cards link to /product/... or, since November 2026, /en/product/...; newer cards also carry the fund code
        # in their class ("products-card__item BDKM ..."), which saves fetching the fund page.
        for code, url, label in re.findall(
                r'class="products-card__item ?([A-Z][A-Z0-9-]*)?[^"]*"\s*href="(https://bpam\.co\.id/(?:[a-z]{2}/)?product/[^"]+)"'
                r'.*?products-card__label">([^<]+)<', retail, re.S):
            out[label.strip()] = code or url
        # Institutional funds have no page; their code is in the prospectus file name (-ID or -EN by site language).
        institutional = _ok(get(f"{self.BASE}/produk/institusi")).text
        prospectus = r'PROSP-([A-Z0-9-]+?)-(?:ID|EN)\.pdf".*?products-card__label">([^<]+)<'
        for code, label in re.findall(prospectus, institutional, re.S):
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


class Samuel(Manager):
    name, provider = "samuel", "samuel"
    aliases = ((r"^rdpt sam ", "sam penyertaan terbatas "), (r"^indeks ", ""))
    BASE = "https://www.sam.co.id/produk"

    def catalogue(self, get: Get) -> dict[str, str]:
        page = _ok(get(f"{self.BASE}/nab")).text
        return {_text(label): url for url, label in
                re.findall(r'href="(https://www\.sam\.co\.id/produk/detail/[^"]+)"\s*>([^<]+)</a>', page)}

    def history(self, get: Get, key: str) -> dict[str, float]:
        # Google Charts rows: [new Date(2017, 1 ,10), 1000], months counted from 0 as in JavaScript; unsorted.
        rows = re.findall(r"\[new Date\((\d+),\s*(\d+)\s*,\s*(\d+)\),\s*([\d.]+)\]", _ok(get(key)).text)
        return _points((f"{y}-{int(m) + 1:02d}-{int(d):02d}", v) for y, m, d, v in rows)


class Henan(Manager):
    name, provider = "henan", "henan putihrai"
    API = "https://api2.myhero.id/produk"

    def catalogue(self, get: Get) -> dict[str, str]:
        data = _ok(get(self.API)).json()
        rows = (data.get("data") or {}).get("produk") if isinstance(data, dict) else None
        return {x["nama_produk"].strip(): str(x["id"]) for x in rows or []
                if isinstance(x, dict) and isinstance(x.get("nama_produk"), str) and x.get("id") is not None}

    def history(self, get: Get, key: str) -> dict[str, float]:
        # The site offers up to 5Y, but the API takes longer periods; "ALL" returns a single point.
        data = _ok(get(f"{self.API}/nav3", params={"kinerja": "30Y", "produk_id": key})).json()
        rows = (data.get("data") or {}).get("kinerja") if isinstance(data, dict) else None
        return _points((x.get("tanggal"), x.get("nav")) for x in rows or [] if isinstance(x, dict))


class KoreaInvestment(Manager):
    name, provider = "kisi", "korea investment"
    # Pasardana writes "Indeks Kisi IDX30 ETF" and "KIM SYARIAH SUKUK FUND" for the site's "KISI IDX30 ETF" and
    # "KIM Sharia Sukuk Fund"; KIM and KISI are the same house.
    aliases = ((r"^indeks ", ""), (r"^kisi ", "kim "), (r"\bsyariah\b", "sharia"))
    BASE = "https://koreainvestment.co.id/produk"

    def catalogue(self, get: Get) -> dict[str, str]:
        page = _ok(get(self.BASE)).text
        return {_text(label): url for url, label in re.findall(
            r'href="(https://koreainvestment\.co\.id/produk/[a-z0-9-]+)".*?class="produk-card__name">([^<]+)<', page, re.S)}

    def history(self, get: Get, key: str) -> dict[str, float]:
        m = re.search(r'data-nav="([^"]*)"', _ok(get(key)).text)
        try:
            rows = json.loads(html.unescape(m.group(1))) if m else []
        except ValueError:
            return {}
        return _points((x.get("date"), x.get("nav")) for x in rows if isinstance(x, dict))


# Principal (principal.co.id) also publishes full histories, but its host refuses non-browser clients.
MANAGERS: tuple[Manager, ...] = (Batavia(), Trimegah(), Avrist(), Syailendra(), Samuel(), Henan(), KoreaInvestment())
