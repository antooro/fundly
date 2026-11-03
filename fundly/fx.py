"""Exchange rates for optional currency conversion.

Rates are ECB euro reference rates (free reuse, the ECB must be cited as the source); currencies the ECB does
not publish fall back to Yahoo Finance. Conversions go through the euro, using the rate in force on each date.
"""
import bisect
import csv
import io
from datetime import date, datetime, timedelta, timezone

from .cache import Cache
from .errors import FXUnavailable
from .sources.base import Source
from .sources.yahoo import day_of

ECB_URL = "https://data-api.ecb.europa.eu/service/data/EXR/D.{cur}.EUR.SP00.A"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/EUR{cur}=X"
# Currencies with a current ECB daily reference rate (others were discontinued or never published).
_ECB = "AUD BRL CAD CHF CNY CZK DKK GBP HKD HUF IDR ILS INR ISK JPY KRW MXN MYR NOK NZD PHP PLN RON SEK SGD THB TRY USD ZAR"
ECB_CURRENCIES = frozenset(_ECB.split())
FX_TTL = "-12 hours"
LOOKBACK = 10  # days fetched before a range so its first day has a rate even after weekends and holidays


class RateSource(Source):
    """A source of euro exchange rates: units of a currency per euro, by day."""

    types = frozenset()
    schemes = frozenset()

    def covers(self, cur: str) -> bool:
        raise NotImplementedError

    def series(self, cur: str, start: str, end: str) -> list[tuple[str, float]]:
        raise NotImplementedError


class ECBRates(RateSource):
    name = "ecb"

    def covers(self, cur: str) -> bool:
        return cur in ECB_CURRENCIES

    def series(self, cur: str, start: str, end: str) -> list[tuple[str, float]]:
        r = self._get(ECB_URL.format(cur=cur), params={"format": "csvdata", "startPeriod": start, "endPeriod": end})
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return [(row["TIME_PERIOD"], float(row["OBS_VALUE"]))
                for row in csv.DictReader(io.StringIO(r.text)) if row.get("OBS_VALUE")]


class YahooRates(RateSource):
    name = "yahoo"

    def covers(self, cur: str) -> bool:
        return True

    def series(self, cur: str, start: str, end: str) -> list[tuple[str, float]]:
        p1 = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
        p2 = int(datetime.fromisoformat(end).replace(tzinfo=timezone.utc).timestamp()) + 86400
        r = self._get(YAHOO_URL.format(cur=cur), params={"period1": p1, "period2": p2, "interval": "1d"})
        if r.status_code == 404:
            return []
        r.raise_for_status()
        res = (r.json().get("chart", {}).get("result") or [None])[0]
        if not res or res.get("meta", {}).get("currency") != cur:
            return []
        closes = res.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        out = {day_of(ts, res["meta"]): float(v) for ts, v in zip(res.get("timestamp", []), closes, strict=True) if v}
        return sorted(out.items())


class FX:
    def __init__(self, cache: Cache, providers: list[RateSource]):
        self._cache, self._providers = cache, providers

    def _series(self, cur: str, start: str, end: str) -> tuple[list[str], list[float], str]:
        """Units of `cur` per euro, as sorted parallel lists of dates and rates, plus the provider name."""
        lo = (date.fromisoformat(start) - timedelta(days=LOOKBACK)).isoformat()
        for p in self._providers:
            if not p.covers(cur):
                continue
            fetched = self._cache.one(
                "SELECT 1 FROM fx_fetches WHERE currency = ? AND source = ? AND start_date <= ? AND end_date >= ? "
                "AND fetched_at > datetime('now', ?)", (cur, p.name, lo, end, FX_TTL))
            if not fetched:
                rows = p.series(cur, lo, end)
                if not rows:
                    continue
                with self._cache.tx() as c:
                    c.executemany("INSERT OR REPLACE INTO fx_rates (currency, source, date, per_eur) VALUES (?, ?, ?, ?)",
                                  [(cur, p.name, d, v) for d, v in rows])
                    c.execute("INSERT INTO fx_fetches (currency, source, start_date, end_date) VALUES (?, ?, ?, ?)",
                              (cur, p.name, lo, end))
            stored = self._cache.all("SELECT date, per_eur FROM fx_rates WHERE currency = ? AND source = ? "
                                     "AND date BETWEEN ? AND ? ORDER BY date", (cur, p.name, lo, end))
            if stored:
                return [r["date"] for r in stored], [r["per_eur"] for r in stored], p.name
        raise FXUnavailable(f"no exchange rate source has {cur}")

    @staticmethod
    def _at(dates: list[str], rates: list[float], day: str, cur: str) -> tuple[float, str]:
        i = bisect.bisect_right(dates, day) - 1
        if i < 0:
            raise FXUnavailable(f"no {cur} rate on or before {day}")
        return rates[i], dates[i]

    def converter(self, frm: str, to: str, start: str, end: str):
        """-> (convert(day) -> (rate, rate_date), source). Multiply a `frm` amount by the rate to get `to`."""
        legs = {}
        for cur in {frm, to} - {"EUR"}:
            legs[cur] = self._series(cur, start, end)
        sources = sorted({leg[2] for leg in legs.values()})

        def convert(day: str) -> tuple[float, str]:
            per_eur_from, d1 = self._at(*legs[frm][:2], day, frm) if frm != "EUR" else (1.0, day)
            per_eur_to, d2 = self._at(*legs[to][:2], day, to) if to != "EUR" else (1.0, day)
            return per_eur_to / per_eur_from, min(d1, d2)

        return convert, "+".join(sources)
