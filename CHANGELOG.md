# Changelog

## 0.1.0 (unreleased)

First public release.

- `quote`, `history`, `search`, `instrument` and `compare` for mutual funds, ETFs and pension plans worldwide.
- 17 sources: Morningstar, Finect, Financial Times, Quefondos, comparadorfondos, Yahoo Finance, AMFI (+ mfapi.in),
  npsnav.in, Eastmoney, Superintendencia de Pensiones (Chile), datos.gov.co, CVM (Brazil), CAFCI (+ argentinadatos.com),
  analizy.pl, Gemel-net/Pensia-net, fmarket and Pasardana (+ Batavia, Trimegah, Avrist and Syailendra histories).
- Identifiers: ISIN, ticker, Spanish DGS codes (`N####`, `S####`) and `scheme:code` for national codes; aliases
  are translated to the ISIN when one exists.
- Name search routed by script and hint words, so a query only goes to the sources that can answer it.
- Optional currency conversion at ECB reference rates (Yahoo for the rest), with minor units (GBp, ZAc, ILA) handled.
- Data-quality warnings: `stale` and `suspicious` quotes, arbitration between sources, spikes and jumps in histories.
- `to_dict()` on every result and `to_pandas()` on histories.
- Local SQLite cache, safe across threads and processes; bulk files kept on disk and revalidated.
- Gentle by default: honest User-Agent, one request every 0.5 s per site, pauses on HTTP 429, `max_wait` per call.
