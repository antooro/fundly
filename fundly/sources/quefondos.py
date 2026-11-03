import re
from datetime import datetime

from .base import DGS, ISIN, Source, Inst, QuoteResult, FUND, PLAN

NAV_RE = re.compile(r'Valor liquidativo: </span><span class="floatright">([\d.,]+) ([A-Z]{3})')
DATE_RE = re.compile(r'Fecha: </span><span class="floatright">(\d{2}/\d{2}/\d{4})')


class QuefondosSource(Source):
    """VDOS-fed Spanish portal. Latest NAV only (its history is month-end closes)."""

    name = "quefondos"
    types = frozenset({FUND, PLAN})
    schemes = frozenset({ISIN, DGS})

    def get_quote(self, inst: Inst) -> QuoteResult | None:
        kind = "planes" if inst.type == PLAN else "fondos"
        r = self._get(f"https://www.quefondos.com/es/{kind}/ficha/index.html", params={"isin": inst.id})
        if r.status_code == 404 or "{NFONDO}" in r.text:
            return None
        r.raise_for_status()
        nav, dt = NAV_RE.search(r.text), DATE_RE.search(r.text)
        if not nav or not dt:
            return None
        currency = nav.group(2)
        # Quefondos converts non-EUR classes to EUR, so a EUR figure is only trusted for EUR classes.
        if inst.type != PLAN and inst.currency != currency:
            return None
        return QuoteResult(
            nav=float(nav.group(1).replace(".", "").replace(",", ".")),
            date=datetime.strptime(dt.group(1), "%d/%m/%Y").strftime("%Y-%m-%d"),
            currency=currency, source=self.name,
        )
