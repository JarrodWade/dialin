"""Request-scoped flags for /chat (avoids import cycles between bedrock and tools)."""

from __future__ import annotations

from contextvars import ContextVar

# True for turns where the trip place-discovery appendix is attached (heuristic router).
trip_place_discovery_active: ContextVar[bool] = ContextVar(
    "trip_place_discovery_active",
    default=False,
)
