"""Backend selection for the storage seam.

:func:`make_storage_backend` is the one place that maps a backend *name*
onto a concrete :class:`StorageBackend`. Callers that want the default
(every production code path) name nothing and get the file backend; tests
ask for ``"memory"``. A future remote record backend would be a one-branch
addition here and a one-line addition to the conformance suite's registry.
Generation source storage is a separate abstraction; adding a record backend
here must not absorb generation-tree operations.

The default is, and must stay, the file backend: files are zicato's
canonical store of record. The factory exists to make the *non*-default
backends reachable by name (config-driven tests, future deployments),
not to make the store-of-record swappable in production.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from zicato.storage.base import StorageBackend
from zicato.storage.files import FileStorageBackend
from zicato.storage.memory import InMemoryStorageBackend

#: The backend used when no name is given. Files are canonical.
DEFAULT_BACKEND = "files"


def make_storage_backend(
    kind: str = DEFAULT_BACKEND,
    *,
    root: Path | str | None = None,
    **opts: Any,
) -> StorageBackend:
    """Construct a :class:`StorageBackend` by name.

    Parameters
    ----------
    kind:
        ``"files"`` (default) for the canonical file backend, or
        ``"memory"`` for the in-process test backend. An unknown name
        raises :class:`ValueError`.
    root:
        Required for ``kind="files"`` — the directory keys resolve under
        (by convention the ``.zicato/`` workspace). Ignored by the
        in-memory backend, which has no root.
    **opts:
        Reserved for future record backends (for example, connection
        settings for a remote store). Currently unused.

    The returned backend is NOT started — the caller decides when to
    :meth:`StorageBackend.start` it (or uses it as a context manager).
    """
    del opts  # reserved for future backends
    if kind == "files":
        if root is None:
            raise ValueError("the 'files' storage backend requires a root path")
        return FileStorageBackend(root)
    if kind == "memory":
        return InMemoryStorageBackend()
    raise ValueError(f"unknown storage backend {kind!r}; known backends: 'files', 'memory'")


def default_backend(workspace_root: Path) -> StorageBackend:
    """Return a started file backend rooted at ``workspace_root``.

    The convenience the ``runtime/`` domain uses: every runtime-state
    helper that is handed a ``workspace_root`` constructs its backend
    through this so there is exactly one definition of "the canonical
    backend for a workspace". The backend is :meth:`~StorageBackend.start`
    -ed before return so the caller can use it immediately.
    """
    backend = FileStorageBackend(workspace_root)
    backend.start()
    return backend


__all__ = ["make_storage_backend", "default_backend", "DEFAULT_BACKEND"]
