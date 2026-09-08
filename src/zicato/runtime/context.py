"""The child-only environment pointer used by uncontrolled nested invocations."""

from __future__ import annotations

import json
import os
from pathlib import Path

from zicato.core.configuration import ConfigurationError
from zicato.core.runtime_context import WorkerRuntimeContext

RUNTIME_CONTEXT_ENV = "ZICATO_RUNTIME_CONTEXT"


def inherited_runtime_context() -> WorkerRuntimeContext | None:
    """Read a parent's typed worker context; a configured broken pointer is an error."""
    pointer = os.environ.get(RUNTIME_CONTEXT_ENV)
    if pointer is None:
        return None
    path = Path(pointer)
    if not pointer or not path.is_absolute():
        raise ConfigurationError(RUNTIME_CONTEXT_ENV, "value", "expected an absolute file path")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError(
            RUNTIME_CONTEXT_ENV, "value", f"cannot read {path}: {exc}"
        ) from exc
    if not isinstance(document, dict) or "runtime_context" not in document:
        raise ConfigurationError(RUNTIME_CONTEXT_ENV, "missing", "worker runtime_context is absent")
    return WorkerRuntimeContext.from_json(document["runtime_context"])


def bind_worker_runtime_context(args_path: Path) -> None:
    """Bind inheritance only inside the isolated worker process, before target loading."""
    os.environ[RUNTIME_CONTEXT_ENV] = str(args_path.resolve())
