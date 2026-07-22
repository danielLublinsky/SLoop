"""Usage gauge: self-imposed budget (cost + wall clock) as the primary gauge,
plus a hard backstop flag set when the executor sees a subscription
'usage limit reached' error.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class UsageGauge:
    max_cost_usd: float | None
    max_wall_clock_min: float | None
    started_at: float = field(default_factory=time.monotonic)
    total_cost_usd: float = 0.0
    total_jobs: int = 0
    limit_hit: bool = False  # backstop: real subscription limit reached

    def add_job(self, cost_usd: float) -> None:
        self.total_cost_usd += max(cost_usd or 0.0, 0.0)
        self.total_jobs += 1

    def elapsed_min(self) -> float:
        return (time.monotonic() - self.started_at) / 60.0

    def fraction(self) -> float:
        """0.0..1.0+ — the worse of cost-fraction and time-fraction."""
        fractions = [0.0]
        if self.max_cost_usd:
            fractions.append(self.total_cost_usd / self.max_cost_usd)
        if self.max_wall_clock_min:
            fractions.append(self.elapsed_min() / self.max_wall_clock_min)
        return max(fractions)

    def summary(self) -> str:
        parts = [f"{self.total_jobs} jobs", f"${self.total_cost_usd:.2f}",
                 f"{self.elapsed_min():.0f} min", f"{self.fraction() * 100:.0f}% of budget"]
        if self.limit_hit:
            parts.append("SUBSCRIPTION LIMIT HIT")
        return " · ".join(parts)
