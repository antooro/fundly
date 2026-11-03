class FundlyError(Exception):
    pass


class InvalidId(FundlyError, ValueError):
    """Not an ISIN nor a Spanish pension-plan DGS code (N####)."""


class UnknownSource(FundlyError, ValueError):
    pass


class FXUnavailable(FundlyError, LookupError):
    """No exchange rate for this currency and date."""


class FundlyTimeout(FundlyError, TimeoutError):
    """The call ran out of its time budget (Client(max_wait=...)) before any source answered."""


class NotFound(FundlyError, LookupError):
    """No source returned data for this instrument."""
