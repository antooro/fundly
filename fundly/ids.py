"""Instrument identifiers.

Accepted input: a bare ISIN, a bare Spanish pension-plan DGS code (N#### or S####), a bare ticker, or
"scheme:code" for anything else (e.g. "ticker:VOO", "ms:F00001H9R0").

Canonical form (what the library stores and returns): the ISIN or DGS code as-is, otherwise
"scheme:code". Aliases such as tickers are translated to an ISIN whenever a source can resolve them.
"""
import re

from .errors import InvalidId

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
# Spanish pension plans in the DGSFP register: N#### for plans, S#### for sub-plans of employment plans.
DGS_RE = re.compile(r"^[NS]\d{4}$")
TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")

ISIN, DGS, TICKER, MS = "isin", "dgs", "ticker", "ms"
SCHEME_ALIASES = {"es-dgs": DGS}

# scheme -> validator for the code part; sources may register more.
VALIDATORS = {
    ISIN: ISIN_RE.match,
    DGS: DGS_RE.match,
    TICKER: TICKER_RE.match,
    MS: re.compile(r"^[A-Z0-9]{10}$").match,
}
# Codes in these schemes are case-insensitive and stored upper-case.
UPPER = {ISIN, DGS, TICKER, MS}


def isin_check_digit(body: str) -> str:
    """Check digit for the first 11 characters of an ISIN (Luhn over the letters-as-numbers string)."""
    digits = "".join(str(int(c, 36)) for c in body.upper())
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d) * (2 if i % 2 == 0 else 1)
        total += n // 10 + n % 10
    return str((10 - total % 10) % 10)


def is_valid_isin(s: str) -> bool:
    return bool(ISIN_RE.match(s)) and isin_check_digit(s[:11]) == s[11]


def register(scheme: str, validator, upper: bool = False):
    VALIDATORS[scheme] = validator
    if upper:
        UPPER.add(scheme)


def parse(raw) -> tuple[str, str]:
    """-> (scheme, code). Raises InvalidId."""
    s = str(raw).strip()
    if ":" in s:
        scheme, code = (p.strip() for p in s.split(":", 1))
        scheme = SCHEME_ALIASES.get(scheme.lower(), scheme.lower())
    else:
        up = s.upper()
        if ISIN_RE.match(up):
            scheme, code = ISIN, up
        elif DGS_RE.match(up):
            scheme, code = DGS, up
        elif TICKER_RE.match(up):
            scheme, code = TICKER, up
        else:
            raise InvalidId(raw)
    validator = VALIDATORS.get(scheme)
    if scheme in UPPER:
        code = code.upper()
    if not validator or not code or not validator(code):
        raise InvalidId(raw)
    return scheme, code


def canonical(scheme: str, code: str) -> str:
    return code if scheme in (ISIN, DGS) else f"{scheme}:{code}"


def split(canonical_id: str) -> tuple[str, str]:
    if ":" in canonical_id:
        scheme, code = canonical_id.split(":", 1)
        return scheme, code
    return (DGS, canonical_id) if DGS_RE.match(canonical_id) else (ISIN, canonical_id)
