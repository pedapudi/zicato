"""Process-scoped imports for fixed drivers and isolated candidate snapshots."""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import inspect
import sys
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, TypeVar, cast

from zicato.core.adapter_config import DriverImportContext

F = TypeVar("F", bound=Callable[..., Any])


class _DeclaredSourceLoader(importlib.machinery.SourceFileLoader):
    """Execute the source being measured, regardless of cached bytecode metadata."""

    def get_code(self, fullname: str) -> Any:
        return self.source_to_code(self.get_data(self.path), self.path)


class _DeclaredSourceFinder(importlib.abc.MetaPathFinder):
    def __init__(self, roots: tuple[Path, ...]) -> None:
        self.roots = roots

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None or not isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            return None
        source = Path(spec.loader.path).resolve()
        if not any(source.is_relative_to(root) for root in self.roots):
            return None
        spec.loader = _DeclaredSourceLoader(fullname, str(source))
        return spec


def _package_names(roots: tuple[Path, ...]) -> set[str]:
    names: set[str] = set()
    for root in roots:
        if root.is_dir():
            for entry in root.iterdir():
                name = entry.stem if entry.suffix == ".py" else entry.name
                if name.isidentifier() and (entry.is_dir() or entry.suffix == ".py"):
                    names.add(name)
    # The running framework retains its own class and module identities.
    names.discard("zicato")
    return names


@dataclass(slots=True)
class _ImportState:
    context: DriverImportContext
    snapshot: Path | None
    cleanup: ExitStack
    users: int = 0


_active_scope: _ImportState | None = None
_scope_lock = threading.RLock()


@contextmanager
def driver_import_scope(
    context: DriverImportContext, *, snapshot_root: Path | None = None
) -> Iterator[None]:
    """Keep imports mounted until every overlapping use of this context exits.

    Python import tables belong to the process. Concurrent different
    workspaces or candidate snapshots require separate processes. Calls
    sharing the same context may overlap, including asynchronous board work.
    """
    global _active_scope
    snapshot = snapshot_root.resolve() if snapshot_root is not None else None
    if not context.roots and snapshot is None:
        yield
        return
    with _scope_lock:
        state = _active_scope
        if state is None:
            cleanup = ExitStack()
            cleanup.enter_context(_mounted_import_tables(context, snapshot))
            state = _ImportState(context, snapshot, cleanup)
            _active_scope = state
        elif state.context != context or (snapshot is not None and state.snapshot != snapshot):
            raise RuntimeError("different driver import contexts require separate processes")
        state.users += 1
    try:
        yield
    finally:
        with _scope_lock:
            state.users -= 1
            if state.users == 0:
                _active_scope = None
                state.cleanup.close()


@contextmanager
def _mounted_import_tables(context: DriverImportContext, snapshot: Path | None) -> Iterator[None]:
    if snapshot is not None and "zicato" in context.mutable_packages:
        raise ValueError(
            "candidate package zicato requires a separate target process from the running framework"
        )
    roots = ((snapshot,) if snapshot is not None else ()) + context.roots
    names = _package_names(roots) | {
        name for name in context.mutable_packages if name.isidentifier()
    }
    saved_path = sys.path[:]
    saved_finders = sys.meta_path[:]
    saved_modules = {
        name: module for name, module in sys.modules.copy().items() if name.split(".")[0] in names
    }
    for name in saved_modules:
        del sys.modules[name]
    sys.path[:] = [str(root) for root in dict.fromkeys(roots)] + saved_path
    sys.meta_path.insert(0, _DeclaredSourceFinder(roots))
    importlib.invalidate_caches()
    try:
        yield
    finally:
        for name in tuple(sys.modules):
            if name.split(".")[0] in names:
                del sys.modules[name]
        sys.modules.update(saved_modules)
        sys.path[:] = saved_path
        sys.meta_path[:] = saved_finders
        importlib.invalidate_caches()


def imported_sources(context: DriverImportContext, snapshot_root: Path) -> dict[str, str]:
    """Report loaded candidate modules and reject imports outside the snapshot."""
    root = snapshot_root.resolve()
    sources: dict[str, str] = {}
    for name, module in tuple(sys.modules.items()):
        if name.split(".")[0] not in context.mutable_packages:
            continue
        locations = list(getattr(module, "__path__", ()))
        source = getattr(module, "__file__", None)
        if source:
            locations.append(source)
            sources[name] = str(Path(source).resolve())
        for location in locations:
            if not Path(location).resolve().is_relative_to(root):
                raise RuntimeError(
                    f"candidate module {name!r} loaded from {location}; "
                    f"expected source under {root}"
                )
    return sources


def workspace_driver_imports(workspace_root: Path) -> DriverImportContext:
    """Resolve authored roots relative to the workspace, never the process cwd."""
    from zicato.workspace.config_io import read_workspace_config

    return DriverImportContext.from_config(
        read_workspace_config(workspace_root).raw, workspace_root
    )


def with_workspace_imports(function: F) -> F:
    """Own imports for an orchestration entry point receiving workspace_root."""
    signature = inspect.signature(function)

    def context(args: tuple[Any, ...], kwargs: dict[str, Any]) -> DriverImportContext:
        arguments = signature.bind_partial(*args, **kwargs).arguments
        configured = arguments.get("workspace_config")
        runtime = configured.get("runtime", {}) if configured else {}
        if not isinstance(runtime, Mapping):
            runtime = {}
        explicit = getattr(arguments.get("config"), "driver_imports", None)
        if isinstance(explicit, DriverImportContext):
            return explicit
        root = Path(
            arguments.get("workspace_root")
            or arguments.get("workspace")
            or runtime.get("workspace_root")
            or ".zicato"
        )
        return (
            DriverImportContext.from_config(configured, root)
            if configured is not None
            else workspace_driver_imports(root)
        )

    if inspect.iscoroutinefunction(function):

        @wraps(function)
        async def asynchronous(*args: Any, **kwargs: Any) -> Any:
            with driver_import_scope(context(args, kwargs)):
                return await function(*args, **kwargs)

        return cast("F", asynchronous)

    @wraps(function)
    def synchronous(*args: Any, **kwargs: Any) -> Any:
        with driver_import_scope(context(args, kwargs)):
            return function(*args, **kwargs)

    return cast("F", synchronous)
