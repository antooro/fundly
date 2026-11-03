import re

# Prices quoted in a minor unit, e.g. London listings in pence: code -> (major currency, units per major unit).
MINOR_UNITS = {"GBp": ("GBP", 100), "GBX": ("GBP", 100), "GBx": ("GBP", 100), "ZAc": ("ZAR", 100),
               "ZAC": ("ZAR", 100), "ILA": ("ILS", 100), "ILa": ("ILS", 100), "USX": ("USD", 100)}
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def to_major(price: float, currency: str | None) -> tuple[float, str | None]:
    """A price in pence, cents or agorot becomes the same price in pounds, rand or shekels."""
    if currency in MINOR_UNITS:
        major, factor = MINOR_UNITS[currency]
        return price / factor, major
    return price, currency


def major(currency: str | None) -> str | None:
    return MINOR_UNITS[currency][0] if currency in MINOR_UNITS else currency


def check_currency(code: str) -> str:
    c = str(code).strip().upper()
    if not CURRENCY_RE.match(c):
        raise ValueError(f"not an ISO currency code: {code!r}")
    return c
