"""Several processes using the same cache directory at the same time (scripts, notebooks, cron jobs)."""
import multiprocessing as mp
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


def worker(cache_dir: str, n: int, rounds: int) -> str:
    from fundly import Client
    from fundly.sources.base import NavEntry
    from test_client import Fake, q

    hist = [NavEntry(f"2026-10-{d:02d}", 100 + d + n) for d in range(1, 28)]
    src = Fake("morningstar", quote=q(10.0 + n), history=hist,
               hits=[{"id": f"IE00000000{n:02d}", "name": f"Shared Fund {n}", "type": "fund", "currency": "EUR"}])
    try:
        with Client(cache=cache_dir, sources=[src], fx_providers=[], max_wait=None) as c:
            for i in range(rounds):
                c._cache.upsert_instruments([{"id": f"LU00000{n:02d}{i:03d}", "name": f"Fund {n}-{i}", "type": "fund",
                                              "currency": "EUR", "ticker": f"T{n}X{i}"}])
                c.quote(f"LU00000{n:02d}{i:03d}")
                c.history(f"LU00000{n:02d}{i:03d}", "2026-10-01", "2026-10-27")
                c.search(f"shared fund {n}")  # distinct per process: an identical query would be served from the log
                c.search(f"fund {n}", live=False)
        return "ok"
    except Exception as e:  # reported back to the parent test
        return f"{type(e).__name__}: {e}"


@pytest.mark.parametrize("processes, rounds", [(6, 25), (8, 200)])
def test_processes_share_one_cache_without_locking_errors(tmp_path, processes, rounds):
    ctx = mp.get_context("spawn")
    with ctx.Pool(processes) as pool:
        results = pool.starmap(worker, [(str(tmp_path), n, rounds) for n in range(processes)])
    assert results == ["ok"] * processes

    from fundly import Client
    with Client(cache=tmp_path, sources=[], fx_providers=[]) as c:
        found = c.search("fund", live=False, limit=100_000)
        assert len(found) == processes * rounds + processes  # every per-round fund + one "Shared Fund" each
        assert c.search(f"T{processes - 1}X{rounds - 1}", live=False)[0].id.endswith(f"{rounds - 1:03d}")
