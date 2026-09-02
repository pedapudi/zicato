"""Construction of the storage seam's backends.

Every :class:`StorageBackend` the process uses is built here.
:func:`make_storage_backend` maps a backend *name* onto a concrete
backend. :func:`workspace_backend` is the form the domains use: it names
a workspace root rather than a backend name, and its signature states
whether the backend it returns has been started.

The default is, and must stay, the file backend, because files are zicato's
canonical store of record. Selecting a backend by name exists to make the
*non*-default backends reachable (config-driven tests, future deployments)
rather than to make the store of record swappable in production. A future
remote record backend would be a one-branch addition here and a one-line
addition to the conformance suite's registry. Generation source storage is a
separate abstraction; adding a record backend here must not absorb
generation-tree operations.
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
    Callers holding a workspace root should use :func:`workspace_backend`,
    which states that decision in its own signature.
    """
    del opts  # reserved for future backends
    if kind == "files":
        if root is None:
            raise ValueError("the 'files' storage backend requires a root path")
        return FileStorageBackend(root)
    if kind == "memory":
        return InMemoryStorageBackend()
    raise ValueError(f"unknown storage backend {kind!r}; known backends: 'files', 'memory'")


def workspace_backend(workspace_root: Path | str, *, start: bool) -> StorageBackend:
    """Return the canonical backend for a workspace root.

    Every domain that persists records against a workspace — ``epoch/``,
    ``runtime/``, ``workspace/``, and the dashboard's readers — obtains its
    backend here, so "the canonical backend for this workspace" has exactly
    one definition.

    ``start`` is keyword-only and has no default: the two lifecycles differ
    in their side effects, and the caller is the one that knows which it
    needs. ``start=True`` calls :meth:`StorageBackend.start`, creating the
    workspace root directory if it is missing. ``start=False`` returns a
    backend that has touched no filesystem — resolving a key to a path needs
    nothing on disk, a read of an absent key yields ``None``, and
    :meth:`StorageBackend.write_json` creates the parent directories it needs
    on the first write regardless. Readers ask for ``start=False`` so that
    inspecting a workspace that does not exist leaves the filesystem alone.
    """
    backend = make_storage_backend(root=workspace_root)
    if start:
        backend.start()
    return backend


__all__ = ["make_storage_backend", "workspace_backend", "DEFAULT_BACKEND"]
