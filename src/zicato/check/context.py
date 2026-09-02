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

import json
import shutil
from functools import cached_property
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from zicato.core.types import BoardEntry, MutationPoint, ScoringWeights
from zicato.core.workspace import board_path, scoring_path
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
    ) -> None:
        self.workspace_root = workspace_root
        self._epoch_override = epoch_id
        self._live_contract = live_contract
        self._temporary_snapshot: TemporaryDirectory[str] | None = None

    def __enter__(self) -> CheckContext:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Release a fresh-workspace snapshot materialised for this check."""
        if self._temporary_snapshot is not None:
            self._temporary_snapshot.cleanup()
            self._temporary_snapshot = None

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
        try:
            return read_workspace_config(self.workspace_root)
        except (OSError, ValueError):
            return WorkspaceConfig.absent(self.workspace_root)

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

        path = self._scoring_path
        if path is None or not path.exists():
            return {}, ScoringWeights(), None
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
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
        adapter = self.config.raw.get("adapter")
        raw: Any = None
        if isinstance(adapter, dict):
            raw = adapter.get("mutable_trees")
        if not raw:
            raw = self.config.raw.get("mutable_trees") or list(self.config.source_roots)
        return tuple(Path(str(entry)) for entry in raw)

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
            adapter = make_adapter_from_config(self.config.raw)
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
        from zicato.board.jsonl import load_board  # noqa: PLC0415

        path = self._board_path
        if path is None:
            return (), None
        if not path.exists():
            return (), f"board not found at {path}"
        try:
            return tuple(load_board(path)), None
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
