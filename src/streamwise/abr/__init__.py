"""Adaptive bitrate algorithms behind a common interface.

Algorithms are created from a spec string ``name[:key=value,...]``, e.g. ``throughput``,
``buffer:reservoir_s=5,cushion_s=20`` or ``fixed:level=max``.
"""

from __future__ import annotations

from collections.abc import Callable

from streamwise.abr.base import AbrAlgorithm, AbrState
from streamwise.abr.rules import BufferAbr, FixedAbr, ThroughputAbr

REGISTRY: dict[str, Callable[..., AbrAlgorithm]] = {
    "throughput": ThroughputAbr,
    "buffer": BufferAbr,
    "fixed": FixedAbr,
}


def _coerce(value: str) -> int | float | str:
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            pass
    return value


def create_abr(spec: str) -> AbrAlgorithm:
    name, _, params = spec.partition(":")
    factory = REGISTRY.get(name)
    if factory is None:
        raise ValueError(f"unknown ABR {name!r}; choose from {', '.join(sorted(REGISTRY))}")
    kwargs: dict[str, int | float | str] = {}
    for item in filter(None, params.split(",")):
        key, sep, value = item.partition("=")
        if not sep:
            if name == "fixed" and not kwargs:  # allow the short form "fixed:3"
                key, value = "level", key
            else:
                raise ValueError(f"bad ABR parameter {item!r}, expected key=value")
        kwargs[key.strip()] = _coerce(value.strip())
    try:
        return factory(**kwargs)
    except TypeError as exc:
        raise ValueError(f"bad parameters for ABR {name!r}: {exc}") from exc


__all__ = ["AbrAlgorithm", "AbrState", "BufferAbr", "FixedAbr", "ThroughputAbr", "create_abr"]
