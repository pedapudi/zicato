"""Validate public evolve calls while holding exclusive mutation ownership."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zicato.core.runtime_context import TelemetryEndpoints
from zicato.core.settings import InvocationOverlay, ResolvedConfiguration, resolve_configuration
from zicato.epoch.execution import EpochExecutionContract
from zicato.runtime.lock import WorkspaceLock, acquire_workspace_lock, release_workspace_lock

if TYPE_CHECKING:
    from zicato.core.types import RuntimeConfig
    from zicato.logging_stream import LogStreamHandle

log = logging.getLogger(__name__)


@dataclass(slots=True)
class InvocationContext:
    """Owned settings and evaluation inputs retained through descendant cleanup."""

    writer: WorkspaceLock
    configuration: ResolvedConfiguration
    workspace_config_bytes: bytes
    resources: AsyncExitStack = field(default_factory=AsyncExitStack)
    execution_contract: EpochExecutionContract | None = None
    runtime_config: RuntimeConfig | None = None
    telemetry: TelemetryEndpoints = TelemetryEndpoints()
    log_stream: LogStreamHandle | None = None
    _imports: ExitStack = field(default_factory=ExitStack)

    @property
    def workspace_config(self) -> dict[str, Any]:
        """Return an independent copy of the captured workspace declaration."""
        result: dict[str, Any] = json.loads(self.workspace_config_bytes)
        return result

    def select_epoch(
        self, epoch_id: str, *, intentional_roll: bool = False
    ) -> EpochExecutionContract:
        """Bind one verified epoch, retaining it until an intentional contract roll."""
        from zicato.check import require_workspace_valid  # noqa: PLC0415
        from zicato.core.adapter_config import DriverImportContext  # noqa: PLC0415
        from zicato.driver_imports import driver_import_scope  # noqa: PLC0415
        from zicato.epoch.execution import load_epoch_execution_contract  # noqa: PLC0415
        from zicato.runtime.lock import validate_workspace_lock  # noqa: PLC0415

        validate_workspace_lock(self.writer, self.writer.workspace_root)
        if self.execution_contract is not None:
            if self.execution_contract.epoch_id == epoch_id:
                self.execution_contract.verify_implementation()
                return self.execution_contract
            if not intentional_roll:
                raise ValueError("invocation epoch changes require an intentional contract roll")
            self._imports.close()
        selected = load_epoch_execution_contract(self.writer.workspace_root, epoch_id)
        require_workspace_valid(self.writer.workspace_root, execution_contract=selected)
        self._imports.enter_context(
            driver_import_scope(
                DriverImportContext.from_config(
                    selected.adapter_configuration, self.writer.workspace_root
                )
            )
        )
        self.execution_contract = selected
        return selected

    async def _close(self) -> BaseException | None:
        """Finish worker ownership before releasing services and the writer."""
        from zicato.tournament.runner import drain_worker_cleanup  # noqa: PLC0415

        failure: BaseException | None = None
        try:
            await drain_worker_cleanup(self.writer.workspace_root)
        except asyncio.CancelledError as exc:
            # Cancellation propagates only after worker ownership has drained.
            failure = exc
        try:
            await self.resources.__aexit__(
                type(failure) if failure is not None else None,
                failure,
                failure.__traceback__ if failure is not None else None,
            )
        except BaseException as exc:
            failure = failure or exc
        return failure

    def _repair_index(self) -> None:
        """Project settled records after producers close, retaining pending markers on failure."""
        from zicato.evolve.ingest import index_preflight  # noqa: PLC0415

        if self.execution_contract is None:
            return
        try:
            index_preflight(self.writer.workspace_root, writer=self.writer)
        except Exception as exc:  # noqa: BLE001 — canonical execution remains authoritative
            log.warning("index repair required after invocation: %s", exc)

    def _close_logs(self) -> None:
        """Retain durable diagnostics through the final projection attempt."""
        if self.log_stream is not None:
            self.log_stream.close()


@asynccontextmanager
async def validated_invocation(
    workspace_root: Path,
    epoch_id: str | None,
    instance_id: str,
    *,
    overlay: InvocationOverlay | None = None,
    prepare_contract: Callable[[WorkspaceLock], None] | None = None,
) -> AsyncIterator[InvocationContext]:
    """Refuse competing invocations before validation or any execution writes."""
    from zicato.check import require_workspace_valid  # noqa: PLC0415
    from zicato.contract_draft.publication import recover_contract_publication  # noqa: PLC0415
    from zicato.epoch.lifecycle import recover_epoch_publication  # noqa: PLC0415
    from zicato.workspace.config_io import read_workspace_config  # noqa: PLC0415

    writer = acquire_workspace_lock(workspace_root, instance_id)
    resources = AsyncExitStack()
    resources.callback(release_workspace_lock, writer)
    invocation: InvocationContext | None = None
    primary_failure: BaseException | None = None
    try:
        recover_contract_publication(writer.workspace_root, writer=writer)
        recover_epoch_publication(writer.workspace_root, writer=writer)
        if prepare_contract is not None:
            prepare_contract(writer)
        workspace_config = read_workspace_config(writer.workspace_root).raw
        invocation = InvocationContext(
            writer,
            resolve_configuration(workspace_config, overlay=overlay),
            json.dumps(workspace_config).encode(),
            resources,
        )
        resources.callback(invocation._close_logs)
        resources.callback(invocation._repair_index)
        resources.callback(invocation._imports.close)
        from zicato.telemetry.sink import (  # noqa: PLC0415
            resolve_harmonograf_grpc_target,
            resolve_harmonograf_url,
        )

        url = resolve_harmonograf_url(config=invocation.configuration.values.integration)
        if url:
            invocation.telemetry = TelemetryEndpoints(url, resolve_harmonograf_grpc_target(url))
        else:
            from zicato.telemetry.harmonograf_supervisor import (
                find_workspace_harmonograf,  # noqa: PLC0415
            )

            handle = find_workspace_harmonograf(writer.workspace_root)
            if handle is not None:
                invocation.telemetry = TelemetryEndpoints(handle.web_url, handle.grpc_target)
        if epoch_id is None:
            from zicato.core.adapter_config import DriverImportContext  # noqa: PLC0415
            from zicato.driver_imports import driver_import_scope  # noqa: PLC0415

            require_workspace_valid(writer.workspace_root, live_contract=True)
            invocation._imports.enter_context(
                driver_import_scope(
                    DriverImportContext.from_config(workspace_config, writer.workspace_root)
                )
            )
        else:
            invocation.select_epoch(epoch_id)
        yield invocation
    except BaseException as exc:
        primary_failure = exc
        raise
    finally:

        async def close() -> BaseException | None:
            if invocation is not None:
                return await invocation._close()
            await resources.aclose()
            return None

        cleanup = asyncio.create_task(close())
        cancelled: asyncio.CancelledError | None = None
        while True:
            try:
                cleanup_failure = await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError as exc:
                if cleanup.cancelled():
                    raise
                cancelled = cancelled or exc
        if primary_failure is None:
            if cancelled is not None:
                raise cancelled
            if cleanup_failure is not None:
                raise cleanup_failure
        elif cleanup_failure is not None:
            log.warning("invocation cleanup failed: %s", cleanup_failure)
