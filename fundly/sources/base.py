import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import httpx

from .._version import __version__
from ..ids import DGS, DGS_RE, ISIN, ISIN_RE, split  # noqa: F401  (re-exported for sources)

FUND, ETF, PLAN = "fund", "etf", "plan"

# Identify the library honestly; no source here needs a browser disguise.
USER_AGENT = f"fundly/{__version__} (Python library; +https://github.com/antooro/fundly)"
DEFAULT_COOLDOWN = 60.0
# Large files a source downloads (e.g. monthly bulk data) are kept on disk and revalidated with the server.
FILE_FRESH_SECONDS = 3600
FILES_MAX_BYTES = 500 * 1024 * 1024


class RateLimited(Exception):
    """The site answered 429; the source is paused and the next source is tried instead."""


@dataclass
class Inst:
    """What a source gets to work with: the canonical id plus whatever the local catalogue already knows."""
    id: str
    type: str | None = None  # None: not seen before
    currency: str | None = None
    ids: dict = field(default_factory=dict)  # morningstar_id, finect_id, ticker, ...

    @property
    def scheme(self) -> str:
        return split(self.id)[0]

    @property
    def code(self) -> str:
        return split(self.id)[1]

    @property
    def isin(self) -> str | None:
        return self.id if self.scheme == ISIN else None


@dataclass
class QuoteResult:
    nav: float
    date: str
    currency: str
    source: str
    price_type: str = "nav"  # "nav": official net asset value; "market": exchange close
    cached: bool = False


@dataclass
class NavEntry:
    date: str
    nav: float


@dataclass
class HistoryResult:
    entries: list[NavEntry]
    currency: str
    source: str
    price_type: str = "nav"
    cached: bool = False


class Source:
    name: str
    types: frozenset = frozenset({FUND, ETF})
    schemes: frozenset = frozenset({ISIN})  # id schemes this source understands (see fundly.ids)
    # Name-search routing (see fundly.search): "global" sources get every Latin-script query, "national"
    # ones only queries in one of their scripts or mentioning one of their hint words (or as a fallback),
    # "none" sources have no name search.
    search_scope: str = "none"
    search_scripts: frozenset = frozenset()
    search_hints: frozenset = frozenset()
    headers: ClassVar[dict] = {}
    timeout = 20
    min_interval = 0.5  # seconds between two requests to the same site

    def __init__(self, min_interval: float | None = None):
        if min_interval is not None:
            self.min_interval = min_interval
        # Set by the Client to a folder in its cache directory; None keeps downloads in memory only.
        self.files_dir: Path | None = None
        self._lock = threading.Lock()
        self._last_sent = float("-inf")
        self._cooldown_until = 0.0
        self.http = httpx.Client(
            timeout=self.timeout, follow_redirects=True,
            headers={"User-Agent": USER_AGENT, **self.headers},
        )

    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        # The interval counts from when the previous request actually went out: sleep() overshoots by a few
        # ms, so fixed slots would let a late request and a punctual next one go out closer than min_interval.
        # perf_counter, not monotonic: on Windows before Python 3.13 monotonic only ticks every ~16 ms.
        while True:
            with self._lock:
                now = time.perf_counter()
                if now < self._cooldown_until:
                    raise RateLimited(f"{self.name} asked us to slow down; retrying in {self._cooldown_until - now:.0f}s")
                wait = self._last_sent + self.min_interval - now
                if wait <= 0:
                    self._last_sent = now
                    break
            time.sleep(wait)
        r = self.http.request(method, url, **kw)
        if r.status_code == 429:
            # Leave the site alone for as long as it asks (or a minute) instead of retrying.
            try:
                delay = float(r.headers.get("Retry-After", DEFAULT_COOLDOWN))
            except ValueError:
                delay = DEFAULT_COOLDOWN
            with self._lock:
                self._cooldown_until = time.perf_counter() + delay
            raise RateLimited(f"{self.name} answered 429; pausing it for {delay:.0f}s")
        return r

    def _get(self, url: str, **kw) -> httpx.Response:
        return self._request("GET", url, **kw)

    def _fetch_file(self, url: str, name: str) -> bytes | None:
        """GET a large file, kept on disk between sessions. A copy younger than an hour is used as is; an older
        one is revalidated (If-None-Match / If-Modified-Since), so an unchanged file costs a 304 and no bytes.
        None if the server has no such file."""
        if self.files_dir is None:
            r = self._get(url)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.content
        path, meta_path = self.files_dir / name, self.files_dir / f"{name}.meta.json"
        meta = {}
        if path.exists() and meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
            except ValueError:
                meta = {}
            if time.time() - meta.get("checked", 0) < FILE_FRESH_SECONDS:
                os.utime(path)
                return path.read_bytes()
        headers = {}
        if meta.get("etag"):
            headers["If-None-Match"] = meta["etag"]
        if meta.get("last_modified"):
            headers["If-Modified-Since"] = meta["last_modified"]
        r = self._get(url, headers=headers)
        if r.status_code == 304 and path.exists():
            data = path.read_bytes()
        elif r.status_code == 404:
            return None
        else:
            r.raise_for_status()
            data = r.content
            meta = {"etag": r.headers.get("ETag"), "last_modified": r.headers.get("Last-Modified")}
            self._write_atomic(path, data)
        meta["checked"] = time.time()
        self._write_atomic(meta_path, json.dumps(meta).encode())
        self._trim_files()
        return data

    def _write_atomic(self, path: Path, data: bytes):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)  # other processes see either the old file or the new one, never half of it

    def _trim_files(self):
        assert self.files_dir is not None
        files = [p for p in self.files_dir.iterdir() if p.is_file() and not p.name.endswith(".meta.json")
                 and not p.name.startswith(".tmp-")]
        total = sum(p.stat().st_size for p in files)
        for p in sorted(files, key=lambda p: p.stat().st_mtime):
            if total <= FILES_MAX_BYTES:
                break
            total -= p.stat().st_size
            p.unlink(missing_ok=True)
            (self.files_dir / f"{p.name}.meta.json").unlink(missing_ok=True)

    def _post(self, url: str, **kw) -> httpx.Response:
        return self._request("POST", url, **kw)

    def supports(self, inst: Inst) -> bool:
        if inst.scheme not in self.schemes:
            return False
        if inst.type:
            return inst.type in self.types
        return bool(self.types & {FUND, ETF})

    def resolve(self, scheme: str, code: str) -> dict | None:
        """Turn an alias (e.g. a ticker) into a search-style hit, ideally with an ISIN as its id."""
        return None

    def get_quote(self, inst: Inst) -> QuoteResult | None:
        return None

    def get_history(self, inst: Inst, start: str, end: str) -> HistoryResult | None:
        return None

    def search(self, q: str) -> list[dict]:
        """Name search. Each hit: id, name, type, plus any of currency/provider/category/ter/size_eur/<source>_id."""
        return []

    def close(self):
        self.http.close()


class Cached:
    """A value loaded at most once per `ttl` seconds and shared across threads (e.g. a whole-universe list).

    The lock is held while loading so concurrent callers wait for one download instead of starting their own.
    """

    def __init__(self, load, ttl: float):
        self._load, self._ttl = load, ttl
        self._lock = threading.Lock()
        self._at, self._value = 0.0, None

    def get(self):
        with self._lock:
            if self._value is None or time.monotonic() - self._at > self._ttl:
                self._value, self._at = self._load(), time.monotonic()
            return self._value


def in_range(entries: list[NavEntry], start: str, end: str) -> list[NavEntry]:
    return sorted((e for e in entries if start <= e.date <= end), key=lambda e: e.date)
