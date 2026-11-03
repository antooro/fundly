"""Data-quality checks: old prices and prices that disagree with everything else known about an instrument."""
import math
from datetime import date

# A price older than this (days) is flagged as stale. Index series are rebuilt from monthly returns.
STALE_DAYS = {"index": 45}
DEFAULT_STALE_DAYS = 10
# Two prices for the same instrument within a month further apart than this factor disagree.
# Funds rarely move 50% in a month; unit errors (pence vs pounds, ×100) are far beyond it.
OUTLIER_FACTOR = 1.5
# Two answers within this factor of each other agree.
AGREE_FACTOR = 1.1
REFERENCE_WINDOW_DAYS = 30


def stale_warning(day: date, price_type: str, today: date | None = None) -> str | None:
    age = ((today or date.today()) - day).days
    if age > STALE_DAYS.get(price_type, DEFAULT_STALE_DAYS):
        return f"stale: last price is {age} days old ({day.isoformat()})"
    return None


def apart(a: float, b: float, factor: float = OUTLIER_FACTOR) -> bool:
    if a <= 0 or b <= 0:
        return True
    return max(a, b) / min(a, b) > factor


def how_far(a: float, b: float) -> str:
    """'×100' for unit errors, otherwise a percentage."""
    ratio = a / b
    power = round(math.log10(ratio))
    if power and abs(math.log10(ratio) - power) < 0.05:
        return f"×{10 ** power:g}"
    return f"{(ratio - 1) * 100:+.0f}%"


def history_warnings(points: list[tuple[date, float]]) -> list[str]:
    """Isolated spikes (one point far from both neighbours that agree with each other) and level jumps
    (from one day to the next, the whole series moves by more than OUTLIER_FACTOR and stays there)."""
    out = []
    prices = [p for _, p in points]
    for i in range(1, len(points) - 1):
        before, here, after = prices[i - 1], prices[i], prices[i + 1]
        if apart(here, before) and apart(here, after) and not apart(before, after, AGREE_FACTOR):
            out.append(f"outlier: {points[i][0].isoformat()} is {here:g} between {before:g} and {after:g}")
    spikes = {w.split()[1] for w in out}
    for i in range(1, len(points)):
        day = points[i][0].isoformat()
        prev_day = points[i - 1][0].isoformat()
        if day in spikes or prev_day in spikes:
            continue
        if apart(prices[i], prices[i - 1]):
            out.append(f"jump: {how_far(prices[i], prices[i - 1])} from {prev_day} to {day}")
    return out
