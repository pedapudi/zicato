"""Detector registry — bind new pattern detectors at import time.

Most operators will compose the built-in detector tuple
:data:`zicato.patterns.detectors.ALL_DETECTORS` directly. The registry
here is a convenience for plugins / experimental detectors that want to
be discoverable from a single entry point without having to edit
``ALL_DETECTORS``.

Usage::

    from zicato.patterns import register_detector, get_all_detectors

    @register_detector
    def my_detector(inp):
        ...

    # Combine the built-ins with every registered detector:
    detectors = ALL_DETECTORS + get_all_detectors()
    patterns = detect_patterns(inp, detectors=detectors)

The registry is process-global and intentionally append-only. There is
no ``unregister`` because production callers want a deterministic set of
detectors per process; test cases that want to scope a detector to a
single test should call the detector directly rather than registering
it.
"""

from __future__ import annotations

from zicato.patterns.detectors import DetectorFn

#: Process-global list of registered detectors. Append-only.
_REGISTRY: list[DetectorFn] = []


def register_detector(fn: DetectorFn) -> DetectorFn:
    """Register *fn* in the process-global detector list.

    Idempotent: registering the same callable twice (e.g. on a module
    reload) is a no-op. Returns the function unchanged so the call can
    be used as a decorator.
    """

    if fn not in _REGISTRY:
        _REGISTRY.append(fn)
    return fn


def get_all_detectors() -> tuple[DetectorFn, ...]:
    """Return a snapshot tuple of every registered detector.

    The result is a tuple so callers can safely concatenate it with
    :data:`zicato.patterns.detectors.ALL_DETECTORS` without worrying
    about list aliasing.
    """

    return tuple(_REGISTRY)


__all__ = ["get_all_detectors", "register_detector"]
