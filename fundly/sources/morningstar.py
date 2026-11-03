from ..ids import ISIN, MS, TICKER, canonical
from .base import Source, Inst, QuoteResult, HistoryResult, NavEntry, FUND, ETF, in_range

KEY = "klr5zyak8x"
SCREENER_URL = f"https://lt.morningstar.com/api/rest.svc/{KEY}/security/screener"
TIMESERIES_URL = f"https://lt.morningstar.com/api/rest.svc/timeseries_price/{KEY}"
FIELDS = "SecId|Name|ISIN|Ticker|ClosePrice|ClosePriceDate|CategoryName|FundTNAV|OngoingCharge|PriceCurrency|Universe"
# Worldwide universes: every fund and every ETF Morningstar tracks, in any country.
UNIVERSES = {"FOALL$$ALL": FUND, "ETALL$$ALL": ETF}
ALL = "|".join(UNIVERSES)


FILTER_FIELD = {ISIN: "ISIN", TICKER: "Ticker", MS: "SecId"}


class MorningstarSource(Source):
    name = "morningstar"
    search_scope = "global"
    types = frozenset({FUND, ETF})
    schemes = frozenset({ISIN, TICKER, MS})

    def _screener(self, **params) -> list[dict]:
        base = {"outputType": "json", "version": 1, "languageId": "en-GB", "securityDataPoints": FIELDS,
                "universeIds": ALL}
        r = self._get(SCREENER_URL, params={**base, **params})
        r.raise_for_status()
        return r.json().get("rows", [])

    def _rows(self, scheme: str, code: str) -> list[dict]:
        return self._screener(page=1, pageSize=10, filters=f"{FILTER_FIELD[scheme]}:EQ:{code}")

    def _row(self, inst: Inst) -> dict | None:
        rows = self._rows(inst.scheme, inst.code)
        if inst.currency:
            # An ETF has one row per listing; prefer one quoted in the class currency.
            rows.sort(key=lambda r: r.get("PriceCurrency") != inst.currency)
        return rows[0] if rows else None

    @staticmethod
    def _hit(r: dict) -> dict:
        return {
            "id": r["ISIN"] if r.get("ISIN") else canonical(MS, r["SecId"]),
            "name": r.get("Name"), "type": UNIVERSES.get(r.get("Universe") or "", FUND),
            "currency": r.get("PriceCurrency"), "category": r.get("CategoryName"),
            "ter": r.get("OngoingCharge"), "size_eur": int(r["FundTNAV"]) if r.get("FundTNAV") else None,
            "morningstar_id": r.get("SecId"), "ticker": r.get("Ticker"),
        }

    def resolve(self, scheme: str, code: str) -> dict | None:
        if scheme not in FILTER_FIELD:
            return None
        rows = [r for r in self._rows(scheme, code) if r.get("SecId")]
        return self._hit(rows[0]) if rows else None

    @staticmethod
    def _price_type(row: dict) -> str:
        # For ETFs Morningstar reports an exchange close, not the issuer's NAV.
        return "market" if UNIVERSES.get(row.get("Universe") or "") == ETF else "nav"

    def get_quote(self, inst: Inst) -> QuoteResult | None:
        row = self._row(inst)
        if not row or row.get("ClosePrice") is None or not row.get("PriceCurrency"):
            return None
        return QuoteResult(
            nav=float(row["ClosePrice"]), date=(row.get("ClosePriceDate") or "")[:10],
            currency=row["PriceCurrency"], source=self.name, price_type=self._price_type(row),
        )

    def get_history(self, inst: Inst, start: str, end: str) -> HistoryResult | None:
        row = self._row(inst)
        if not row or not row.get("SecId") or not row.get("PriceCurrency"):
            return None
        r = self._get(TIMESERIES_URL, params={
            # The plain SecId returns nothing outside Europe; the universe-qualified form works everywhere.
            "id": f"{row['SecId']}]2]0]{row.get('Universe') or 'FOALL$$ALL'}", "idtype": "Morningstar",
            # Without an explicit currency the endpoint picks an arbitrary one.
            "currencyId": row["PriceCurrency"], "frequency": "daily",
            "startDate": start, "endDate": end, "outputType": "json",
        })
        if r.status_code == 404:
            return None
        r.raise_for_status()
        securities = r.json().get("TimeSeries", {}).get("Security", [])
        if not securities:
            return None
        entries = [
            NavEntry(date=p["EndDate"][:10], nav=float(p["Value"]))
            for p in securities[0].get("HistoryDetail", []) if p.get("EndDate") and p.get("Value")
        ]
        return HistoryResult(entries=in_range(entries, start, end), currency=row["PriceCurrency"],
                             source=self.name, price_type=self._price_type(row))

    def search(self, q: str) -> list[dict]:
        hits, seen = [], set()
        for r in self._screener(page=1, pageSize=30, term=q, currencyId="EUR"):
            if not r.get("SecId"):
                continue
            hit = self._hit(r)
            if hit["id"] not in seen:
                seen.add(hit["id"])
                hits.append(hit)
        return hits
