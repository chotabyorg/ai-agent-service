"""Local completion timings; never sourced from provider metadata."""

from dataclasses import dataclass, field
import time


@dataclass(slots=True)
class CompletionTiming:
    started_at: float = field(default_factory=time.monotonic, repr=False)
    first_delta_ms: int | None = None
    model_ms: int | None = None

    def first_delta(self) -> None:
        if self.first_delta_ms is None:
            self.first_delta_ms = max(0, round((time.monotonic() - self.started_at) * 1000))

    def complete(self) -> None:
        self.model_ms = max(0, round((time.monotonic() - self.started_at) * 1000))
