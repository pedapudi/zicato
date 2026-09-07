"""The read-only workspace view every validator shares.

One context object per run, every field lazy. Two reasons the laziness
matters. **Cost**: the gate runs at the head of every ``evolve``, before
any spend, so a validator that never asks for a fact must not pay to
build it. **Extensibility**: health detectors take their inputs
positionally, so a new detector needing a new fact means editing the
orchestrator (13-recipes.md Recipe 1, step 6). One shared object makes
that one ``cached_property`` here and nothing anywhere else.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from contextlib import ExitStack
from dataclasses import replace
from functools import cached_property
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from zicato.epoch.execution import EpochExecutionContract

from zicato.config import HealthConfig, health_config_from_workspace
from zicato.core.adapter_config import DriverImportContext
from zicato.core.types import BoardEntry, MutationPoint, ScoringWeights
from zicato.core.workspace import board_path, scoring_path
from zicato.driver_imports import driver_import_scope
from zicato.epoch.lifecycle import current_epoch_id
from zicato.mutation.enumerator import UnboundSpanMarker
from zicato.workspace.config_io import WorkspaceConfig, read_workspace_config


class CheckContext:
    """Lazily-loaded facts about one workspace.

    ``epoch_id`` defaults to the ``current_epoch`` marker. A workspace
    with no epoch yet still yields a usable context whose contract facts
    are empty, so the gate works on a freshly-registered workspace.

    ``live_contract`` selects the operator's editable board and scoring
    over the epoch's frozen copies. It belongs on the path where
    ``evolve`` was not pinned to an explicit epoch: auto-epoching is
    about to freeze whatever the live files now say, so the live files
    ARE the contract the round will run. Checking the frozen copy there
    would validate the contract the last round used and miss a defect
    the operator introduced since.
    """

    def __init__(
        self,
        workspace_root: Path,
        *,
        epoch_id: str | None = None,
        live_contract: bool = False,
        execution_contract: EpochExecutionContract | None = None,
        candidate_scoring: dict[str, Any] | None = None,
    ) -> None:
        self.workspace_root = workspace_root
        if execution_contract is not None:
            if live_contract or (epoch_id is not None and epoch_id != execution_contract.epoch_id):
                raise ValueError("captured execution inputs conflict with the requested contract")
            if execution_contract.workspace_root != workspace_root:
                raise ValueError("captured execution inputs belong to another workspace")
            epoch_id = execution_contract.epoch_id
        self.execution_contract = execution_contract
        self._epoch_override = epoch_id
        self._live_contract = live_contract
        self._candidate_scoring = (
            None if candidate_scoring is None else json.loads(json.dumps(candidate_scoring))
        )
        self._live_digests: dict[Path, str] = {}
        from zicato.workspace.contract_publication import assert_contract_publication_complete

        self._live_revision = (
            assert_contract_publication_complete(workspace_root) if live_contract else None
        )
        self._temporary_snapshot: TemporaryDirectory[str] | None = None
        self._imports = ExitStack()
        self._config_error: str | None = None

    def _require_live_snapshot(self) -> None:
        if not self._live_contract:
            return
        from zicato.workspace.contract_publication import require_contract_revision

        require_contract_revision(self.workspace_root, self._live_revision)
        for path, digest in self._live_digests.items():
            try:
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
            except FileNotFoundError:
                actual = ""
            if actual != digest:
                raise ValueError(f"{path}: live contract changed during reading; reload the check")

    def _read_contract_text(self, path: Path) -> str:
        self._require_live_snapshot()
        data = path.read_bytes()
        if self._live_contract:
            self._live_digests[path] = hashlib.sha256(data).hexdigest()
        self._require_live_snapshot()
        return data.decode("utf-8")

    def __enter__(self) -> CheckContext:
        self._imports.enter_context(driver_import_scope(self.driver_imports))
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Release a fresh-workspace snapshot materialised for this check."""
        self._imports.close()
        if self._temporary_snapshot is not None:
            self._temporary_snapshot.cleanup()
            self._temporary_snapshot = None

    @cached_property
    def driver_imports(self) -> DriverImportContext:
        try:
            return DriverImportContext.from_config(self.config.raw, self.workspace_root)
        except ValueError:
            # Adapter construction reports malformed declarations as gate defects.
            return DriverImportContext()

    @cached_property
    def _live_paths(self) -> dict[str, Path | None]:
        from zicato.epoch.contract import default_contract_paths  # noqa: PLC0415

        contract = self.config.contract
        defaults = default_contract_paths(self.workspace_root)
        return {
            key: Path(str(contract[key])) if contract.get(key) else default
            for key, default in defaults.items()
        }

    @cached_property
    def epoch_id(self) -> str | None:
        return self._epoch_override or current_epoch_id(self.workspace_root)

    @property
    def uses_live_contract(self) -> bool:
        """Whether validators are inspecting the editable contract files."""
        return self._live_contract

    @cached_property
    def config(self) -> WorkspaceConfig:
        """The workspace ``config.json``; absent or malformed reads as empty."""
        self._require_live_snapshot()
        try:
            config = read_workspace_config(self.workspace_root)
        except (OSError, ValueError) as exc:
            self._config_error = str(exc)
            return WorkspaceConfig.absent(self.workspace_root)
        if self.execution_contract is not None:
            raw = {**config.raw, **self.execution_contract.adapter_configuration}
            return replace(config, raw=raw, source_roots=self.execution_contract.mutable_trees)
        if self._live_contract and config.exists:
            if json.loads(self._read_contract_text(config.path)) != config.raw:
                raise ValueError("workspace config changed during reading; reload the check")
        elif self._live_contract:
            self._live_digests[config.path] = ""
            self._require_live_snapshot()
        return config

    @property
    def config_error(self) -> str | None:
        """The original configuration failure, before dependent checks run."""
        _ = self.config
        return self._config_error

    @cached_property
    def health_config(self) -> HealthConfig:
        """The board-quality thresholds the ``health`` block declares.

        The workspace ``config.json``'s ``health`` block is the one
        operator surface for these thresholds, so a validator judging a
        static board property reads the value the loop-health detectors
        will read after the round. A block that does not parse falls back
        to the defaults rather than turning a gate run into a crash; the
        ``zicato health`` command reports the typo itself.
        """
        try:
            return health_config_from_workspace(self.config.raw)
        except (KeyError, TypeError, ValueError):
            return HealthConfig()

    @property
    def proposer_configuration(self) -> dict[str, Any]:
        """The declaration that constructs the selected epoch's proposer."""
        if self.execution_contract is not None:
            external = self.execution_contract.external_proposer
            return {} if external is None else dict(external.workspace_config)
        return dict(self.config.raw)

    @cached_property
    def _scoring_path(self) -> Path | None:
        """Whichever scoring file this round will actually be graded by."""
        if self._live_contract:
            return self._live_paths.get("scoring_path")
        epoch = self.epoch_id
        return None if epoch is None else scoring_path(self.workspace_root, epoch)

    @cached_property
    def _board_path(self) -> Path | None:
        """Whichever board file this round will actually run."""
        if self._live_contract:
            return self._live_paths.get("board_path")
        epoch = self.epoch_id
        return None if epoch is None else board_path(self.workspace_root, epoch)

    @cached_property
    def raw_scoring(self) -> dict[str, Any]:
        """The selected ``scoring.json`` as written — a partial document."""
        return self._scoring_or_error[0]

    @cached_property
    def scoring_error(self) -> str | None:
        """Why scoring could not be read and validated, or ``None``."""
        return self._scoring_or_error[2]

    @cached_property
    def _scoring_or_error(self) -> tuple[dict[str, Any], ScoringWeights, str | None]:
        from zicato.mutation.markers import syntax_table_from_config  # noqa: PLC0415
        from zicato.workspace_loader import scoring_weights_from_dict  # noqa: PLC0415

        if self.execution_contract is not None:
            return self.execution_contract.raw_scoring, self.execution_contract.scoring, None
        path = self._scoring_path
        if self._candidate_scoring is None and (path is None or not path.exists()):
            if self._live_contract and path is not None:
                self._live_digests[path] = ""
                self._require_live_snapshot()
            return {}, ScoringWeights(), None
        try:
            self._require_live_snapshot()
            if self._candidate_scoring is not None:
                loaded = self._candidate_scoring
            else:
                assert path is not None
                loaded = json.loads(self._read_contract_text(path))
            if not isinstance(loaded, dict):
                raise ValueError(f"expected a JSON object, got {type(loaded).__name__}")
            weights = scoring_weights_from_dict(loaded)
            syntax_table_from_config(weights.mutation_surface)
        except (OSError, TypeError, ValueError) as exc:
            return {}, ScoringWeights(), f"{path}: {exc}"
        return loaded, weights, None

    @cached_property
    def scoring(self) -> ScoringWeights:
        """The selected scoring, defaults filled in when valid."""
        return self._scoring_or_error[1]

    @cached_property
    def has_evaluation_contract(self) -> bool:
        """``True`` once an epoch exists to carry a board and a scoring.

        A registered-but-unopened workspace has an adapter and a surface
        to check but no contract yet, and reporting a missing board there
        would be noise rather than a defect. On the live-contract path
        the editable files are the contract, so their presence is what
        counts rather than an epoch marker.
        """
        if self._live_contract:
            return True
        return self.epoch_id is not None

    @cached_property
    def registered_trees(self) -> tuple[Path, ...]:
        """The source roots the baseline seeder will copy into ``v0``.

        Reads the current shape first — ``config["adapter"]["mutable_trees"]``,
        which is where :func:`~zicato.adapter_factory.make_adapter_from_config`
        looks — then falls back to the top-level keys the older
        ``zicato epoch register`` flow persisted.
        """
        from zicato.core.adapter_config import registered_mutable_trees

        try:
            return registered_mutable_trees(self.config.raw, self.workspace_root)
        except ValueError:
            return ()

    @cached_property
    def models(self) -> Any:
        """The workspace's configured model roles."""
        from zicato.models_config import load_models_config  # noqa: PLC0415

        return load_models_config(self.config.raw)

    @cached_property
    def adapter(self) -> Any | None:
        """The adapter instance the orchestrator constructs from config."""
        return self._adapter_or_error[0]

    @cached_property
    def has_adapter_config(self) -> bool:
        """Whether config names adapter wiring, under either accepted key."""
        return self.config.raw.get("adapter") is not None or bool(
            self.config.raw.get("adk_entrypoint")
        )

    @cached_property
    def adapter_error(self) -> str | None:
        """Why the adapter could not be constructed or serialised."""
        return self._adapter_or_error[2]

    @cached_property
    def _adapter_or_error(self) -> tuple[Any | None, dict[str, Any] | None, str | None]:
        from zicato.adapter_factory import make_adapter_from_config  # noqa: PLC0415
        from zicato.tournament.worker_transport import adapter_worker_spec  # noqa: PLC0415

        try:
            adapter = make_adapter_from_config(self.config.raw, workspace_root=self.workspace_root)
            return adapter, adapter_worker_spec(adapter), None
        except Exception as exc:  # noqa: BLE001 — any construction failure is the defect
            return None, None, str(exc)

    @cached_property
    def generation_snapshot(self) -> Path | None:
        """The reigning generation's realized source tree, if any."""
        epoch = self.epoch_id
        if epoch is not None:
            from zicato.evolve.generation_phase import (  # noqa: PLC0415
                current_generation,
                snapshot_root,
            )

            try:
                root = snapshot_root(
                    self.workspace_root, epoch, current_generation(self.workspace_root, epoch)
                )
            except (FileNotFoundError, OSError, ValueError):
                pass
            else:
                if root.exists():
                    return root

        # A fresh epoch will seed v0 by copying each registered tree under
        # its basename. Reproduce that layout off-workspace so both surface
        # resolution and adapter.load see exactly what round zero will see.
        if not self.registered_trees:
            return None
        from zicato.epoch.snapshot_scope import copytree_ignore  # noqa: PLC0415

        self._temporary_snapshot = TemporaryDirectory(prefix="zicato-check-")
        root = Path(self._temporary_snapshot.name) / "snapshot"
        root.mkdir()
        for raw in self.registered_trees:
            source = raw.resolve()
            if not source.exists():
                continue
            target = root / source.name
            if source.is_file():
                shutil.copy2(source, target)
            else:
                shutil.copytree(source, target, ignore=copytree_ignore())
        return root

    @property
    def uses_temporary_snapshot(self) -> bool:
        """Whether this check materialised the would-be fresh ``v0``."""
        return self._temporary_snapshot is not None

    def source_paths(self, *paths: Path) -> tuple[Path, ...]:
        """Translate temporary check paths to the registered source locations."""
        snapshot = self.generation_snapshot
        if not self.uses_temporary_snapshot or snapshot is None:
            return paths
        result: list[Path] = []
        for path in paths:
            if path == snapshot:
                result.extend(self.registered_trees)
                continue
            for source in self.registered_trees:
                mounted = snapshot / source.resolve().name
                if path.is_relative_to(mounted):
                    result.append(source / path.relative_to(mounted))
                    break
            else:
                result.append(path)
        return tuple(dict.fromkeys(result))

    @cached_property
    def mutable_trees(self) -> tuple[Path, ...]:
        """The roots runtime gives the mutation enumerator."""
        return self._mutable_trees_or_error[0]

    @cached_property
    def mutable_trees_error(self) -> str | None:
        """Why the adapter could not resolve its roots, or ``None``.

        An adapter that raises during root resolution has an empty
        surface *and* a cause, and only the cause tells the operator what
        to fix. Reporting the empty surface alone would name the symptom.
        """
        return self._mutable_trees_or_error[1]

    @cached_property
    def _mutable_trees_or_error(self) -> tuple[tuple[Path, ...], str | None]:
        snapshot = self.generation_snapshot
        adapter = self.adapter
        if snapshot is None or adapter is None:
            return (), None
        from zicato.evolve.generation_phase import mutable_trees  # noqa: PLC0415

        try:
            return tuple(mutable_trees(adapter, snapshot)), None
        except Exception as exc:  # noqa: BLE001 — any resolution failure is the defect
            return (), f"{type(exc).__name__}: {exc}"

    @cached_property
    def surface(self) -> tuple[MutationPoint, ...]:
        """Every mutation point under :attr:`mutable_trees`.

        Uses the same adapter-resolved roots and declared syntax table as
        generation preparation.
        """
        return self._surface_and_unbound[0]

    @cached_property
    def unbound_span_markers(self) -> tuple[UnboundSpanMarker, ...]:
        """Span markers the single surface walk resolved to no literal.

        Structural facts from the enumerator itself rather than a scrape of its
        log: each carries the id, the file, the line, and which of the
        two ways it failed to bind.
        """
        return self._surface_and_unbound[1]

    @cached_property
    def _surface_and_unbound(
        self,
    ) -> tuple[tuple[MutationPoint, ...], tuple[UnboundSpanMarker, ...]]:
        from zicato.mutation.enumerator import (  # noqa: PLC0415
            collect_unbound_span_markers,
            enumerate_mutations,
        )
        from zicato.mutation.markers import swap_syntax_table  # noqa: PLC0415

        trees = [tree for tree in self.mutable_trees if tree.exists()]
        if not trees or self.scoring_error is not None:
            return (), ()

        with (
            collect_unbound_span_markers() as unbound,
            swap_syntax_table(self.scoring.mutation_surface),
        ):
            points = tuple(enumerate_mutations(trees))
        return points, tuple(unbound)

    @cached_property
    def board(self) -> tuple[BoardEntry, ...]:
        """The epoch's frozen board. Empty when absent or unparseable.

        :func:`~zicato.check.validators.contract_integrity` reports the
        parse failure itself, via :attr:`board_error`.
        """
        return self._board_or_error[0]

    @cached_property
    def board_error(self) -> str | None:
        """Why the board could not be read, or ``None``."""
        return self._board_or_error[1]

    @cached_property
    def _board_or_error(self) -> tuple[tuple[BoardEntry, ...], str | None]:
        from zicato.board.jsonl import parse_board_with_meta  # noqa: PLC0415

        if self.execution_contract is not None:
            return tuple(self.execution_contract.board_with_meta[0]), None
        path = self._board_path
        if path is None:
            return (), None
        if not path.exists():
            return (), f"board not found at {path}"
        try:
            return tuple(
                parse_board_with_meta(self._read_contract_text(path), source=path)[0]
            ), None
        except (ValueError, OSError) as exc:
            return (), f"{path}: {exc}"

    @cached_property
    def adapter_spec(self) -> dict[str, Any] | None:
        """The canonical worker spec emitted by the constructed adapter."""
        return self._adapter_or_error[1]

    @cached_property
    def worker_env(self) -> dict[str, str] | None:
        """The environment a tournament worker would be given, or inheritance."""
        from zicato.tournament.worker_transport import (  # noqa: PLC0415
            adapter_uses_integration,
            scrubbed_worker_env,
        )

        # The two knobs are read straight from the ``runtime`` block rather
        # than through ``make_runtime_config``, which also imports the
        # workspace's call_llm dotted paths — work this check has no use for
        # and whose failure is a different defect. Both reads mirror
        # ``runtime_factory``.
        runtime = self.config.runtime
        if not runtime.get("scrub_worker_env", False):
            return None
        passthrough = tuple(str(name) for name in runtime.get("worker_env_passthrough") or ())
        goldfive = (
            self.scoring.goldfive
            if adapter_uses_integration(self.adapter_spec, "goldfive")
            else None
        )
        goldfive_secret_names: tuple[str, ...] = ()
        if goldfive is not None:
            try:
                from zicato.integrations.goldfive import secret_env_names  # noqa: PLC0415

                goldfive_secret_names = secret_env_names(goldfive)
            except (ImportError, TypeError, ValueError):
                # The Goldfive validator reports a missing runtime or malformed
                # document. Other checks must still be able to inspect the
                # worker environment without crashing first.
                pass
        return scrubbed_worker_env(
            models=self.models,
            secret_env_keys=goldfive_secret_names,
            extra_env_keys=passthrough,
        )


__all__ = ["CheckContext"]
