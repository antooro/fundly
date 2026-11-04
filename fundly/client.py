import json
import logging
import math
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import cast

from .cache import Cache, default_cache_dir, normalize
from .currency import check_currency, to_major
from .errors import FundlyTimeout, InvalidId, NotFound, UnknownSource
from .fx import FX, ECBRates, RateSource, YahooRates
from .models import Comparison, History, Instrument, PricePoint, PriceType, Quote
from .ids import DGS, ISIN, MS, TICKER, TICKER_RE, canonical, is_valid_isin, parse, split
from .quality import AGREE_FACTOR, REFERENCE_WINDOW_DAYS, apart, history_warnings, how_far, stale_warning
from .search import match_rank, plan, rewrite
from .sources.base import ETF, FUND, PLAN, HistoryResult, Inst, NavEntry, QuoteResult, Source
from .sources.argentina import CAFCISource
from .sources.brazil import BrazilCVMSource
from .sources.chile import ChileAFPSource
from .sources.china import EastmoneySource
from .sources.colombia import ColombiaSource
from .sources.comparadorfondos import ComparadorFondosSource
from .sources.finect import FinectSource
from .sources.ft import FTSource
from .sources.india import AMFISource, NPSSource
from .sources.indonesia import PasardanaSource
from .sources.israel import GemelNetSource
from .sources.morningstar import MorningstarSource
from .sources.peru import SMVSource
from .sources.philippines import UITFSource
from .sources.poland import AnalizySource
from .sources.quefondos import QuefondosSource
from .sources.turkey import TEFASSource
from .sources.vietnam import FmarketSource
from .sources.yahoo import YahooSource
import contextlib

# Every source in the default client; each one only answers for the id schemes and types it declares.
DEFAULT_SOURCES = (
    MorningstarSource, FinectSource, FTSource, ComparadorFondosSource, QuefondosSource, YahooSource,
    AMFISource, NPSSource, EastmoneySource, ChileAFPSource, ColombiaSource, BrazilCVMSource, CAFCISource,
    AnalizySource, GemelNetSource, FmarketSource, PasardanaSource, TEFASSource,
    UITFSource, SMVSource,
)

log = logging.getLogger("fundly")

# First source that answers wins; sources not listed are tried afterwards. ETFs start with the official
# NAV (Finect) before exchange closes; funds with the official Indian feed for Indian ISINs, then
# Morningstar, then the independent VDOS feed (Finect). National sources only claim their own ids.
ORDER = {
    FUND: ["amfi", "morningstar", "finect", "ft", "quefondos", "yahoo"],
    ETF: ["finect", "morningstar", "ft", "yahoo"],
    PLAN: ["finect", "comparadorfondos", "quefondos"],
}

QUOTE_TTL = "-6 hours"
HISTORY_TTL = "-24 hours"
SEARCH_TTL = "-24 hours"
# A source that answered "not found" is not asked again for this long (errors and timeouts are not remembered).
MISS_TTL = "-24 hours"



def _date(value) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _valid_point(day, price) -> bool:
    """Data from the web is checked once, here: a real calendar date and a finite, positive price."""
    try:
        return bool(day) and date.fromisoformat(str(day)[:10]) is not None and math.isfinite(price) and price > 0
    except (TypeError, ValueError):
        return False


class Client:
    """
    cache: True for the platform's cache folder (or $FUNDLY_CACHE_DIR), a path for another directory,
           or False to keep everything in memory.
    min_interval: minimum seconds between two requests to the same site.
    exclude: source names to leave out, e.g. ["morningstar", "finect", "yahoo"] to only use sites whose
             robots.txt allows automated access. Excluding "yahoo" also drops it as exchange-rate fallback.
    max_wait: seconds a single call may take in total, across every source it tries (None: no limit).
              When it runs out the call raises FundlyTimeout; a source still answering in the background
              will fill the cache for next time.
    """

    def __init__(self, cache: bool | str | Path = True, min_interval: float = 0.5,
                 sources: list[Source] | None = None, exclude: list[str] | tuple[str, ...] = (),
                 fx_providers: list[RateSource] | None = None, max_wait: float | None = 120):
        directory = default_cache_dir() if cache is True else (Path(cache).expanduser() if cache else None)
        self._cache = Cache(directory)
        sources = sources if sources is not None else [cls(min_interval) for cls in DEFAULT_SOURCES]
        unknown = set(exclude) - {s.name for s in sources}
        if unknown:
            raise UnknownSource(f"{sorted(unknown)}; available: {[s.name for s in sources]}")
        self._sources = {s.name: s for s in sources if s.name not in exclude}
        for s in self._sources.values():
            s.files_dir = directory / "files" / s.name if directory else None
        for s in sources:
            if s.name in exclude:
                s.close()
        self.max_wait = max_wait
        # Room for calls that outlive their deadline (they finish in the background) without starving new ones.
        self._pool = ThreadPoolExecutor(max_workers=max(8, 2 * len(self._sources)))
        if fx_providers is None:
            fx_providers = [ECBRates(min_interval)] + ([] if "yahoo" in exclude else [YahooRates(min_interval)])
        self._fx_providers = fx_providers
        self._fx = FX(self._cache, fx_providers)

    @property
    def sources(self) -> dict[str, list[str]]:
        """Source name -> instrument types it covers."""
        return {n: sorted(s.types) for n, s in self._sources.items()}

    # --- public API ---------------------------------------------------------------------------

    def search(self, q: str = "", type: str | None = None, category: str | None = None,
               live: bool = True, limit: int = 20) -> list[Instrument]:
        """Search by name (any words, accents optional), ISIN, ticker or any other id.

        An ISIN, DGS code or "scheme:code" is looked up directly. Text goes only to the sources that can
        match it (see fundly.search); with live=False only the local catalogue is searched. Live hits are
        remembered locally, and the same query is not sent again for a day.
        """
        dl = self._deadline()
        exact = q.strip().upper()
        q = rewrite(q)
        qn = normalize(q)
        exact_ids = {exact}
        parsed = None
        with contextlib.suppress(InvalidId):
            parsed = parse(q)
        if parsed and (parsed[0] == DGS or (parsed[0] == ISIN and is_valid_isin(parsed[1])) or ":" in q):
            return self._search_id(q, type, category, live, dl)
        if parsed:
            exact_ids.add(self._alias(canonical(*parsed)) or canonical(*parsed))
        if live and parsed and parsed[0] == TICKER and q.strip().isupper() and TICKER_RE.match(exact):
            # A single upper-case word reads as a ticker: resolve it (one request) and keep searching text too.
            with contextlib.suppress(Exception):
                exact_ids.add(self._canon(q, dl))
        live_ids = self._search_live(q, dl, type) if live and len(qn) >= 3 else []

        tokens = qn.split()
        where, params = [], []
        if qn:
            ph = ", ".join("?" * len(live_ids)) or "NULL"
            ex = ", ".join("?" * len(exact_ids))
            where.append(f"(id IN ({ex}) OR ({' AND '.join('search_text LIKE ?' for _ in tokens)}) "
                         f"OR id IN ({ph}))")
            params += [*exact_ids, *(f"%{t}%" for t in tokens), *live_ids]
        if type:
            where.append("type = ?")
            params.append(type)
        if category:
            where.append("category LIKE ?")
            params.append(f"%{category}%")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self._cache.all(f"SELECT * FROM instruments {clause}", params)
        # Exact id first, then how well the name matches, then the biggest fund, then source relevance.
        rank = {id: i for i, id in enumerate(live_ids)}
        rows.sort(key=lambda r: (
            r["id"] not in exact_ids,
            match_rank(qn, r["name"], r["search_text"]),
            -(r["size_eur"] or 0),
            rank.get(r["id"], len(rank)),
        ))
        return [self._instrument(r) for r in rows[:limit]]

    def _search_id(self, q: str, type: str | None, category: str | None, live: bool, dl) -> list[Instrument]:
        try:
            cid = self._canon(q, dl) if live else canonical(*parse(q))
            row = self._row(cid) or (self._resolve(cid, dl) and self._row(cid)) if live else self._row(cid)
        except (NotFound, FundlyTimeout):
            return []
        if not row or (type and row["type"] != type) or (category and category.lower() not in (row["category"] or "").lower()):
            return []
        return [self._instrument(row)]

    def instrument(self, id: str) -> Instrument:
        dl = self._deadline()
        inst = self._resolve(self._canon(id, dl), dl)
        row = self._row(inst.id)
        if not row:
            raise NotFound(inst.id)
        return self._instrument(row)

    def quote(self, id: str, source: str | None = None, currency: str | None = None) -> Quote:
        """Latest price. Without `source`, sources are tried in priority order for the instrument type.

        With `currency` (ISO code) the price is converted at the ECB reference rate of the price's date
        (Yahoo for currencies the ECB doesn't publish); the original figure and the rate stay in the result.
        """
        dl = self._deadline()
        inst = self._resolve(self._canon(id, dl), dl)
        suspect: Quote | None = None
        for src in self._candidates(inst, source):
            try:
                q = self._run(dl, self._quote_from, src, inst)
            except FundlyTimeout:
                if suspect:
                    break
                raise
            except Exception as e:
                log.warning("quote %s from %s failed: %r", inst.id, src.name, e)
                continue
            if not q:
                continue
            result = self._quote(inst.id, q)
            ref = None if source else self._reference(inst.id, result)
            if suspect is None and ref and apart(result.price, ref[0]):
                # Far from what we already know: let the next source arbitrate before answering.
                log.warning("quote %s from %s is %s off the last known price; asking another source",
                            inst.id, src.name, how_far(result.price, ref[0]))
                suspect = self._with_warning(result, f"outlier: {result.price:g} vs {ref[0]:g} from {ref[2]} on "
                                                     f"{ref[1]} ({how_far(result.price, ref[0])})")
                continue
            if suspect is not None:
                if not apart(result.price, suspect.price, AGREE_FACTOR):
                    result = suspect  # two sources agree: the reference was the odd one out
                    result = replace(result, warnings=tuple(w for w in result.warnings if not w.startswith("outlier")))
                else:
                    result = self._with_warning(result, f"replaced: {suspect.source} said {suspect.price:g}, "
                                                        f"{how_far(suspect.price, result.price)} off")
            return self._finish_quote(result, currency)
        if suspect is not None:
            return self._finish_quote(suspect, currency)
        raise NotFound(inst.id)

    def _finish_quote(self, q: Quote, currency: str | None) -> Quote:
        stale = stale_warning(q.date, q.price_type)
        if stale:
            q = self._with_warning(q, stale)
        return self._convert_quote(q, currency) if currency else q

    @staticmethod
    def _with_warning(q, warning: str):
        return replace(q, warnings=(*q.warnings, warning))

    def _reference(self, id: str, q: Quote) -> tuple[float, str, str] | None:
        """Most recent other price known for this instrument in the same currency: (price, date, source)."""
        lo = (q.date - timedelta(days=REFERENCE_WINDOW_DAYS)).isoformat()
        row = self._cache.one(
            "SELECT price, price_date AS day, source FROM quote_cache WHERE id = ? AND source != ? AND currency = ? "
            "AND price_date >= ? ORDER BY price_date DESC LIMIT 1", (id, q.source, q.currency, lo))
        hist = self._cache.one(
            "SELECT p.price, p.date AS day, p.source FROM price_cache p JOIN history_fetches f "
            "ON f.id = p.id AND f.source = p.source WHERE p.id = ? AND p.date >= ? AND p.date <= ? "
            "AND f.currency = ? AND NOT (p.source = ? AND p.date = ?) ORDER BY p.date DESC LIMIT 1",
            (id, lo, q.date.isoformat(), q.currency, q.source, q.date.isoformat()))
        best = max((r for r in (row, hist) if r), key=lambda r: r["day"], default=None)
        return (best["price"], best["day"], best["source"]) if best else None

    def compare(self, id: str) -> Comparison:
        """Latest price from every source that covers the instrument, side by side."""
        dl = self._deadline()
        inst = self._resolve(self._canon(id, dl), dl)
        srcs = self._candidates(inst)
        futures = [self._pool.submit(self._quote_from, s, inst) for s in srcs]
        quotes, missing = [], {}
        for s, f in zip(srcs, futures, strict=True):
            try:
                r = f.result(timeout=self._remaining(dl))
            except FutureTimeout:
                missing[s.name] = "timeout"
                continue
            except Exception as e:
                log.warning("compare %s from %s failed: %r", inst.id, s.name, e)
                missing[s.name] = f"error: {type(e).__name__}"
                continue
            if r is None:
                missing[s.name] = "not found"
            else:
                quotes.append(self._quote(inst.id, r))
        quotes = [self._compare_warnings(q, quotes) for q in quotes]
        navs = [q for q in quotes if q.price_type == "nav"]
        spread = None
        if navs:
            key = Counter((q.date, q.currency) for q in navs).most_common(1)[0][0]
            same = [q.price for q in navs if (q.date, q.currency) == key]
            if len(same) > 1:
                spread = round((max(same) - min(same)) / min(same) * 100, 4)
        return Comparison(id=inst.id, name=self._name(inst.id), quotes=quotes, missing=missing, nav_spread_pct=spread)

    def _compare_warnings(self, q: Quote, all_quotes: list[Quote]) -> Quote:
        stale = stale_warning(q.date, q.price_type)
        if stale:
            q = self._with_warning(q, stale)
        others = sorted(o.price for o in all_quotes if o is not q and o.currency == q.currency)
        if len(others) >= 2:
            median = others[len(others) // 2]
            if apart(q.price, median):
                q = self._with_warning(q, f"outlier: {q.price:g} vs {median:g} from the other sources "
                                          f"({how_far(q.price, median)})")
        return q

    def history(self, id: str, start: str | date | None = None, end: str | date | None = None,
                source: str | None = None, currency: str | None = None) -> History:
        """Daily prices between start and end (default: the last year), in the share class currency,
        or converted to `currency` with each day's exchange rate."""
        end_d = _date(end) or date.today()
        start_d = _date(start) or end_d - timedelta(days=365)
        if start_d > end_d:
            raise ValueError("start must be before end")
        dl = self._deadline()
        inst = self._resolve(self._canon(id, dl), dl)
        s, e = start_d.isoformat(), end_d.isoformat()
        for src in self._candidates(inst, source):
            h = self._cached_history(inst.id, src.name, s, e)
            if not h:
                if self._missed(inst.id, src.name, f"history:{s}:{e}"):
                    continue
                try:
                    h = self._run(dl, src.get_history, inst, s, e)
                except FundlyTimeout:
                    raise
                except Exception as ex:
                    log.warning("history %s from %s failed: %r", inst.id, src.name, ex)
                    continue
                h = self._checked_history(h, src.name, inst.id)
                if not h:
                    self._miss(inst.id, src.name, f"history:{s}:{e}")
                    continue
                self._save_history(inst.id, s, e, h)
            points = [PricePoint(date=cast(date, _date(x.date)), price=x.nav) for x in h.entries]
            result = History(
                id=inst.id, name=self._name(inst.id), currency=h.currency, price_type=cast(PriceType, h.price_type),
                source=h.source, cached=h.cached, points=points,
                warnings=tuple(history_warnings([(p.date, p.price) for p in points])),
            )
            return self._convert_history(result, currency) if currency else result
        raise NotFound(inst.id)

    def close(self):
        self._pool.shutdown(wait=False)
        for s in [*self._sources.values(), *self._fx_providers]:
            s.close()
        self._cache.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- internals ----------------------------------------------------------------------------

    def _deadline(self) -> float | None:
        return None if self.max_wait is None else time.monotonic() + self.max_wait

    @staticmethod
    def _remaining(dl: float | None) -> float | None:
        return None if dl is None else max(0.0, dl - time.monotonic())

    def _run(self, dl: float | None, fn, *args):
        """fn(*args), but stop waiting at the deadline (the call itself finishes in the background)."""
        if dl is None:
            return fn(*args)
        remaining = self._remaining(dl)
        if not remaining:
            raise FundlyTimeout(f"gave up after {self.max_wait}s")
        try:
            return self._pool.submit(fn, *args).result(timeout=remaining)
        except FutureTimeout:
            name = getattr(getattr(fn, "__self__", None), "name", None) or (args[0].name if args else "?")
            raise FundlyTimeout(f"gave up after {self.max_wait}s waiting for {name}") from None

    def _alias(self, cid: str) -> str | None:
        row = self._cache.one("SELECT id FROM aliases WHERE alias = ?", (cid,))
        return row["id"] if row else None

    def _canon(self, raw: str, dl: float | None = None) -> str:
        """Canonical id for any accepted input; other codes (tickers, AMFI codes...) become the ISIN when known."""
        scheme, code = parse(raw)
        cid = canonical(scheme, code)
        if scheme in (ISIN, DGS) or self._row(cid):
            return cid
        known = self._alias(cid)
        if known:
            return known
        hit = self._ask_resolve(scheme, code, dl)
        if not hit:
            return cid
        hit["aliases"] = [*hit.get("aliases", []), cid]
        self._cache.upsert_instruments([self._with_domicile(hit)])
        return hit["id"]

    def _ask_resolve(self, scheme: str, code: str, dl: float | None = None) -> dict | None:
        for src in self._sources.values():
            if scheme not in src.schemes:
                continue
            try:
                hit = self._run(dl, src.resolve, scheme, code)
            except FundlyTimeout:
                return None
            except Exception as e:
                log.warning("resolving %s:%s on %s failed: %r", scheme, code, src.name, e)
                continue
            if hit and hit.get("id") and hit.get("name"):
                return dict(hit)
        return None

    @staticmethod
    def _with_domicile(hit: dict) -> dict:
        scheme = split(hit["id"])[0]
        if scheme == ISIN:
            hit.setdefault("domicile", hit["id"][:2])
        elif scheme == DGS:
            hit.setdefault("domicile", "ES")
        elif ("-" in scheme and len(scheme.split("-")[0]) == 2) or scheme == "cn":  # "cl-afp", "in-amfi", "cn"...
            hit.setdefault("domicile", scheme.split("-")[0].upper())
        return hit

    def _row(self, id: str):
        return self._cache.one("SELECT * FROM instruments WHERE id = ?", (id,))

    def _name(self, id: str) -> str | None:
        row = self._row(id)
        return row["name"] if row else None

    @staticmethod
    def _instrument(row) -> Instrument:
        ids = {k: row[k] for k in ("ticker", "morningstar_id", "finect_id") if row[k]}
        return Instrument(
            id=row["id"], id_type=row["id_type"], name=row["name"], type=row["type"], currency=row["currency"],
            domicile=row["domicile"], provider=row["provider"], category=row["category"],
            ter=row["ter"], size_eur=row["size_eur"], identifiers=ids,
        )

    def _convert_quote(self, q: Quote, currency: str) -> Quote:
        currency = check_currency(currency)
        if currency == q.currency:
            return q
        day = q.date.isoformat()
        convert, fx_source = self._fx.converter(q.currency, currency, day, day)
        rate, rate_day = convert(day)
        return replace(q, price=q.price * rate, currency=currency, original_price=q.price,
                       original_currency=q.currency, fx_rate=rate, fx_date=_date(rate_day), fx_source=fx_source)

    def _convert_history(self, h: History, currency: str) -> History:
        currency = check_currency(currency)
        if currency == h.currency or not h.points:
            return h
        convert, fx_source = self._fx.converter(h.currency, currency, h.points[0].date.isoformat(),
                                                h.points[-1].date.isoformat())
        points = [PricePoint(date=p.date, price=p.price * convert(p.date.isoformat())[0]) for p in h.points]
        return replace(h, points=points, currency=currency, original_currency=h.currency, fx_source=fx_source)

    @staticmethod
    def _checked_history(h: HistoryResult | None, source: str, id: str) -> HistoryResult | None:
        if not h or not h.currency:
            return None
        good = [e for e in h.entries if _valid_point(e.date, e.nav)]
        if len(good) < len(h.entries):
            log.warning("history %s from %s: dropped %d invalid points", id, source, len(h.entries) - len(good))
        if not good:
            return None
        currency = to_major(1.0, h.currency)[1] or h.currency
        entries = [NavEntry(e.date[:10], to_major(e.nav, h.currency)[0]) for e in good]
        return replace(h, entries=entries, currency=currency)

    def _quote(self, id: str, q: QuoteResult) -> Quote:
        return Quote(id=id, name=self._name(id), price=q.nav, date=cast(date, _date(q.date)), currency=q.currency,
                     price_type=cast(PriceType, q.price_type), source=q.source, cached=q.cached)

    def _inst(self, id: str) -> Inst:
        row = self._row(id)
        if not row:
            return Inst(id=id, type=PLAN if split(id)[0] == DGS else None)
        ids = {k: row[k] for k in ("ticker", "morningstar_id", "finect_id") if row[k]}
        return Inst(id=id, type=row["type"], currency=row["currency"], ids=ids)

    def _resolve(self, id: str, dl: float | None = None) -> Inst:
        # Ids not in the local catalogue are looked up first: the type decides source order and whether
        # prices are NAVs or exchange closes, and the currency picks the right share class on some sources.
        # Known ids are never re-resolved, even if a field is missing: that would cost a request per call.
        inst = self._inst(id)
        if not self._row(id):
            hit = self._ask_resolve(inst.scheme, inst.code, dl)
            if hit and hit["id"] == id:
                self._cache.upsert_instruments([self._with_domicile(hit)])
            else:
                try:
                    # Last resort for ids no source could resolve: a text search for the code itself,
                    # worldwide sources only (national catalogues are not downloaded for this).
                    self._search_live(inst.code, dl, second_wave=False)
                except Exception as e:
                    log.warning("resolving %s failed: %r", id, e)
            inst = self._inst(id)
        return inst

    def _candidates(self, inst: Inst, only: str | None = None) -> list[Source]:
        if only:
            if only not in self._sources:
                raise UnknownSource(f"{only!r}; available: {list(self._sources)}")
            return [self._sources[only]] if self._sources[only].supports(inst) else []
        names = list(ORDER.get(inst.type or FUND, ORDER[FUND]))
        names += [n for n in self._sources if n not in names]
        return [self._sources[n] for n in names if n in self._sources and self._sources[n].supports(inst)]

    def _quote_from(self, src: Source, inst: Inst) -> QuoteResult | None:
        row = self._cache.one(
            "SELECT * FROM quote_cache WHERE id = ? AND source = ? AND fetched_at > datetime('now', ?)",
            (inst.id, src.name, QUOTE_TTL),
        )
        if row:
            return QuoteResult(nav=row["price"], date=row["price_date"], currency=row["currency"],
                               source=src.name, price_type=row["price_type"], cached=True)
        if self._missed(inst.id, src.name, "quote"):
            return None
        q = src.get_quote(inst)
        if q and not (_valid_point(q.date, q.nav) and q.currency):
            log.warning("quote %s from %s rejected: %r", inst.id, src.name, q)
            q = None
        if not q:
            self._miss(inst.id, src.name, "quote")
        if q:
            q.date = q.date[:10]
            q.nav, q.currency = to_major(q.nav, q.currency)[0], to_major(1.0, q.currency)[1] or q.currency
            with self._cache.tx() as c:
                c.execute(
                    "INSERT OR REPLACE INTO quote_cache (id, source, price, price_date, currency, price_type, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
                    (inst.id, q.source, q.nav, q.date, q.currency, q.price_type),
                )
        return q

    def _missed(self, id: str, source: str, kind: str) -> bool:
        return self._cache.one("SELECT 1 FROM misses WHERE id = ? AND source = ? AND kind = ? "
                               "AND fetched_at > datetime('now', ?)", (id, source, kind, MISS_TTL)) is not None

    def _miss(self, id: str, source: str, kind: str):
        with self._cache.tx() as c:
            c.execute("INSERT OR REPLACE INTO misses (id, source, kind, fetched_at) VALUES (?, ?, ?, datetime('now'))",
                      (id, source, kind))

    def _cached_history(self, id: str, source: str, start: str, end: str) -> HistoryResult | None:
        # Only a hit if one earlier fetch covered the whole requested range.
        fetch = self._cache.one(
            "SELECT currency, price_type FROM history_fetches "
            "WHERE id = ? AND source = ? AND start_date <= ? AND end_date >= ? "
            "AND fetched_at > datetime('now', ?) ORDER BY fetched_at DESC LIMIT 1",
            (id, source, start, end, HISTORY_TTL),
        )
        if not fetch:
            return None
        rows = self._cache.all(
            "SELECT date, price FROM price_cache WHERE id = ? AND source = ? AND date BETWEEN ? AND ? ORDER BY date",
            (id, source, start, end),
        )
        return HistoryResult(entries=[NavEntry(r["date"], r["price"]) for r in rows], currency=fetch["currency"],
                             source=source, price_type=fetch["price_type"], cached=True)

    def _save_history(self, id: str, start: str, end: str, h: HistoryResult):
        with self._cache.tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO price_cache (id, source, date, price) VALUES (?, ?, ?, ?)",
                [(id, h.source, e.date, e.nav) for e in h.entries],
            )
            c.execute(
                "INSERT INTO history_fetches (id, source, start_date, end_date, currency, price_type) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (id, h.source, start, end, h.currency, h.price_type),
            )

    def _ask_search(self, srcs: list[Source], q: str, dl) -> tuple[list[list[dict]], bool]:
        """Ask several sources in parallel. -> (non-empty hit lists, whether every source answered in time)."""
        futures = [self._pool.submit(s.search, q) for s in srcs]
        lists, complete = [], True
        for s, f in zip(srcs, futures, strict=True):
            try:
                hits = f.result(timeout=self._remaining(dl))
            except FutureTimeout:
                log.warning("search %r on %s: no answer before the deadline", q, s.name)
                complete = False
                continue
            except Exception as e:
                log.warning("search %r on %s failed: %r", q, s.name, e)
                continue
            if hits:
                lists.append(hits)
        return lists, complete

    @staticmethod
    def _fold_unlisted(merged: dict[str, dict], order: list[str]) -> list[str]:
        """Morningstar also lists some products (e.g. Spanish pension plans) under its own id only. When such
        an entry has exactly the name of one product with a public id from another source, they are the same
        product: keep the public id, take the missing fields, and remember the Morningstar id as an alias."""
        by_name: dict[str, list[str]] = {}
        for id, h in merged.items():
            if split(id)[0] != MS:
                by_name.setdefault(normalize(h["name"]), []).append(id)
        for id in [i for i in order if split(i)[0] == MS]:
            twins = by_name.get(normalize(merged[id]["name"]), [])
            if len(twins) != 1:
                continue
            target, extra = merged[twins[0]], merged.pop(id)
            for k, v in extra.items():
                if k not in ("id", "type") and target.get(k) is None:
                    target[k] = v
            target["aliases"] = [*target.get("aliases", []), id]
        return [i for i in order if i in merged]

    def _search_live(self, q: str, dl: float | None = None, type: str | None = None,
                     second_wave: bool = True) -> list[str]:
        key = f"{normalize(q)}|{type or ''}"
        row = self._cache.one(
            "SELECT ids FROM search_log WHERE q = ? AND fetched_at > datetime('now', ?)", (key, SEARCH_TTL)
        )
        if row:
            return json.loads(row["ids"])
        first, second = plan(self._sources.values(), q, type)
        lists, complete = self._ask_search(first, q, dl)
        qn = normalize(q)
        if second_wave and second and not any(match_rank(qn, h["name"], normalize(h["name"])) < 9
                              for hits in lists for h in hits if h.get("name")):
            # Nothing in the first wave contains every word: try the national sources that weren't asked.
            more, done = self._ask_search(second, q, dl)
            lists, complete = lists + more, complete and done
        merged: dict[str, dict] = {}
        order: list[str] = []
        # Interleave sources so each one's best hits come first.
        for i in range(max((len(x) for x in lists), default=0)):
            for hits in lists:
                h = hits[i] if i < len(hits) else None
                if not h or not h.get("id") or not h.get("name"):
                    continue
                if h["id"] not in merged:
                    merged[h["id"]] = dict(h)
                    order.append(h["id"])
                    continue
                m = merged[h["id"]]
                for k, v in h.items():
                    if k == "name" and v != m["name"]:
                        m["alt_name"] = " ".join(filter(None, [m.get("alt_name"), v]))
                    elif m.get(k) is None:
                        m[k] = v
        order = self._fold_unlisted(merged, order)
        if merged:
            self._cache.upsert_instruments([self._with_domicile(m) for m in merged.values()])
        if complete:  # a search cut short by the deadline is not remembered, so the next one asks again
            with self._cache.tx() as c:
                c.execute("INSERT OR REPLACE INTO search_log (q, ids, fetched_at) VALUES (?, ?, datetime('now'))",
                          (key, json.dumps(order)))
        return order
