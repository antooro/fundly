"""Prices and search for European funds, ETFs and Spanish pension plans, keyed by ISIN or DGS code.

    import fundly
    fundly.search("azvalor")
    fundly.quote("ES0112611001")
    fundly.history("N5508", start="2024-01-01").to_pandas()
    fundly.compare("IE00B4L5Y983")

The module-level functions share one default Client; create your own Client for custom settings.
"""
import logging

from ._version import __version__
from .client import ORDER, Client
from .errors import FundlyError, FundlyTimeout, FXUnavailable, InvalidId, NotFound, UnknownSource
from .models import Comparison, History, Instrument, PricePoint, Quote

__all__ = [
    "ORDER",
    "Client",
    "Comparison",
    "FXUnavailable",
    "FundlyError",
    "FundlyTimeout",
    "History",
    "Instrument",
    "InvalidId",
    "NotFound",
    "PricePoint",
    "Quote",
    "UnknownSource",
    "__version__",
    "compare",
    "history",
    "instrument",
    "quote",
    "search",
]

# Silent unless the application configures logging; source fallbacks are logged at WARNING on "fundly".
logging.getLogger("fundly").addHandler(logging.NullHandler())

_default: Client | None = None


def _client() -> Client:
    global _default
    if _default is None:
        _default = Client()
    return _default


def search(q: str = "", **kwargs) -> list[Instrument]:
    return _client().search(q, **kwargs)


def instrument(id: str) -> Instrument:
    return _client().instrument(id)


def quote(id: str, source: str | None = None, currency: str | None = None) -> Quote:
    return _client().quote(id, source, currency)


def history(id: str, start=None, end=None, source: str | None = None, currency: str | None = None) -> History:
    return _client().history(id, start, end, source, currency)


def compare(id: str) -> Comparison:
    return _client().compare(id)
