"""The service client — the TUI's only source of truth.

Every number the TUI renders arrives through this module, as JSON, from the
dashboard service. There is no filesystem read, no query-layer call and no
re-derivation anywhere behind it: the browser and the terminal consume the same
bytes, which is what makes "two renderers, one model" a structural fact rather
than a promise.

Deliberately stdlib-only (``urllib``). The ``tui`` extra buys Textual and
nothing else, so a terminal install stays small and the client works in an
environment where the dashboard's own optional deps are absent — you can review
a workspace from a machine that could not serve one.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class ServiceError(RuntimeError):
    """The service could not be reached, or answered with something unusable.

    Carries an operator-facing ``hint``: a lens renders the degraded state with
    the exact command that would fix it, never a bare traceback.
    """

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class Client(Protocol):
    """What a lens is allowed to do: fetch a served payload by path."""

    def get(self, path: str) -> Any:
        """Return the decoded JSON at ``path``, or raise :class:`ServiceError`."""


@dataclass(frozen=True)
class Event:
    """One decoded SSE frame."""

    name: str
    data: Any


class HttpClient:
    """A live client against a running dashboard service.

    Responses are cached for the lifetime of ONE refresh pass (see
    :meth:`begin_pass`), so a lens that fetches ``/api/epoch`` twice in a
    render costs one request and — more importantly — cannot observe two
    different states inside a single painted frame. A screen that mixed two
    snapshots would show numbers that never coexisted.
    """

    def __init__(self, base_url: str, *, timeout: float = 8.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._cache: dict[str, Any] = {}
        self._lock = threading.Lock()

    def begin_pass(self) -> None:
        """Drop the per-pass cache; the next fetch of each path hits the wire."""
        with self._lock:
            self._cache.clear()

    def url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def get(self, path: str) -> Any:
        with self._lock:
            if path in self._cache:
                return self._cache[path]
        payload = self._fetch(path)
        with self._lock:
            self._cache[path] = payload
        return payload

    def _fetch(self, path: str) -> Any:
        url = self.url(path)
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            # A 404 is a legitimate answer for an absent artefact (no index, no
            # such generation). Lenses degrade on None; they do not crash.
            if exc.code == 404:
                return None
            raise ServiceError(
                f"{url} answered HTTP {exc.code}",
                hint="the dashboard service is running but rejected the request",
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ServiceError(
                f"cannot reach the dashboard service at {self.base_url} ({exc})",
                hint=(
                    "start one with `zicato dashboard --workspace <ws>` and pass "
                    "`zicato tui --url <url>`, or run `zicato tui --workspace <ws>` "
                    "to have one started for you"
                ),
            ) from exc
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ServiceError(f"{url} did not return JSON") from exc

    def events(self, *, path: str = "/events") -> Iterator[Event]:
        """Yield decoded SSE frames until the stream closes.

        Blocking; the app runs it on a worker thread. A dropped connection ends
        the iterator rather than raising — the app reconnects and shows the
        connection state in the status band, because a stale screen that LOOKS
        live is the failure mode worth engineering against.
        """
        request = urllib.request.Request(self.url(path), headers={"Accept": "text/event-stream"})
        try:
            response = urllib.request.urlopen(request, timeout=None)
        except (urllib.error.URLError, TimeoutError, OSError):
            return
        with response:
            name = "message"
            data_lines: list[str] = []
            while True:
                try:
                    raw = response.readline()
                except (TimeoutError, OSError):
                    return
                if not raw:
                    return
                line = raw.decode("utf-8", "replace").rstrip("\n").rstrip("\r")
                if line.startswith(":"):
                    continue  # a comment / keep-alive
                if line == "":
                    if data_lines:
                        yield Event(name, _decode(data_lines))
                    name, data_lines = "message", []
                    continue
                field, _, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if field == "event":
                    name = value
                elif field == "data":
                    data_lines.append(value)


def _decode(lines: list[str]) -> Any:
    text = "\n".join(lines)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


class SnapshotClient:
    """A client backed by a fixed ``path -> payload`` mapping.

    The whole test surface runs on this: a lens is exercised against exact
    served payloads with no server, no sockets and no timing. Paths absent from
    the mapping return ``None`` — the same shape the live client returns for a
    404, so the degraded-payload tests exercise the real degrade path.
    """

    def __init__(self, payloads: Mapping[str, Any], *, missing: Any = None) -> None:
        self._payloads = dict(payloads)
        self._missing = missing
        self.requested: list[str] = []

    def begin_pass(self) -> None:
        self.requested.clear()

    def get(self, path: str) -> Any:
        self.requested.append(path)
        return self._payloads.get(path, self._missing)


class FailingClient:
    """A client that always raises — the "service absent" test double."""

    def __init__(self, error: ServiceError | None = None) -> None:
        self._error = error or ServiceError(
            "cannot reach the dashboard service",
            hint="start one with `zicato dashboard --workspace <ws>`",
        )

    def begin_pass(self) -> None:
        return None

    def get(self, path: str) -> Any:
        raise self._error


__all__ = [
    "Client",
    "Event",
    "FailingClient",
    "HttpClient",
    "ServiceError",
    "SnapshotClient",
]
