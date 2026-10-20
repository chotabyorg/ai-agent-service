"""Local completion timings and optional, closed routing observations.

Elapsed times are always local; provider metadata never supplies timing values.
"""

from dataclasses import dataclass, field
import time

from services.router_metadata_observation import RouterObservation


@dataclass(slots=True)
class CompletionTiming:
    started_at: float = field(default_factory=time.monotonic, repr=False)
    first_delta_ms: int | None = None
    model_ms: int | None = None
    router_observation: RouterObservation | None = field(default=None, repr=False)

    def first_delta(self) -> None:
        if self.first_delta_ms is None:
            self.first_delta_ms = max(0, round((time.monotonic() - self.started_at) * 1000))

    def complete(self) -> None:
        self.model_ms = max(0, round((time.monotonic() - self.started_at) * 1000))
