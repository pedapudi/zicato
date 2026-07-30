"""``zicato register`` — record adapter entrypoint and mutable trees.

ADVANCED / DEBUGGING — off the happy path. ``zicato evolve`` resolves
and uses the evaluation contract on its own; you only run ``register``
by hand to set the contract paths up front or to inspect/change them.

After ``zicato init`` creates the workspace, ``zicato register``
records *which* agent to run and *which* source trees the proposer is
allowed to mutate. The entrypoint follows the common Python convention
``module.path:symbol``; mutable trees are filesystem roots (one or
many, passed as repeated ``--mutable-tree`` flags).

The two are NOT independent. A generation snapshot copies each mutable
tree under its BASENAME and the harness loader prepends the snapshot
root to ``sys.path`` — which resolves TOP-LEVEL packages only. So the
entrypoint's top-level module must be one of the mutable trees'
basenames, or the import silently returns whatever copy is already
installed and every round scores unmutated code (issue #110).
``register`` refuses such a registration up front, import-free (see
:func:`_validate_entrypoint`); the adapter re-checks the invariant
against the real ``module.__file__`` at load time.

``register`` also records the canonical *contract source paths* — the
operator's live, editable ``board.jsonl`` / proposer brief (``brief.md``)
/ ``scoring.json``. These default to the conventional location next to
the ``.zicato/`` workspace, but ``--board`` / ``--brief`` /
``--scoring`` override the default. Contract-hash auto-epoching reads
these paths back on every ``evolve`` to decide whether the evaluation
contract has drifted (see ``docs/design/EPOCHS-AND-JOURNALING.md``).

``--proposer-path`` optionally points the workspace at a proposer dir
(``proposers/<name>/`` — skills, plus an optional custom ``agent.py``).
Absent ⇒ the built-in default proposer. The proposer is itself a
contract input: configuring a proposer dir — or editing one of its
skills — rolls the epoch on the next ``evolve`` (see
``docs/design/PROPOSER.md``).

The values are persisted to ``{workspace}/config.json`` so subsequent
subcommands can read them back without re-asking the operator.
"""

from __future__ import annotations

from pathlib import Path

import click

from zicato.workspace.config_io import (
    read_workspace_config,
    workspace_is_initialized,
    write_workspace_config,
)


def _validate_entrypoint(entrypoint: str, mutable_trees: tuple[str, ...] = ()) -> None:
    """Ensure ``entrypoint`` is well-formed AND the trees can be under test.

    Two static checks plus one notice, all IMPORT-FREE (milliseconds) so
    ``register`` works in environments where the agent's runtime deps
    aren't installed yet:

    1. **Syntax** — ``module.path:symbol``, both halves non-empty.
    2. **Tree importability** (issue #110) — every ``--mutable-tree``
       basename must be a possible top-level module name, because a
       generation snapshot copies each tree under its basename and the
       loader only prepends the snapshot root to ``sys.path`` (which
       resolves top-level names only). A tree Python cannot name can never
       be shown to have run from the snapshot, so every mutation to it
       would be a scored no-op. The rule lives with the adapter that owns
       the snapshot layout
       (:func:`zicato.adapters.adk.entrypoint_snapshot_origin_error`);
       skipped when no mutable tree was passed.
    3. **Dependency-shape notice** — an entrypoint OUTSIDE every mutable
       tree is legitimate (the tree is a dependency the harness imports —
       target 2's shape) and is ACCEPTED, with a printed notice saying
       what carries the verification instead: the per-tree resolution
       assert at load time and the post-run record in ``harness_load.json``
       (:func:`zicato.adapters.adk.entrypoint_outside_trees_notice`).
    """
    if ":" not in entrypoint:
        raise click.BadParameter(
            f"entrypoint {entrypoint!r} must be of the form 'module.path:symbol'",
            param_hint="--adk",
        )
    module_part, _, symbol_part = entrypoint.partition(":")
    if not module_part or not symbol_part:
        raise click.BadParameter(
            f"entrypoint {entrypoint!r} must have both module and symbol",
            param_hint="--adk",
        )
    # Import-free: the adapter module's own imports are lazy, so this does
    # not pull in google-adk / goldfive.
    from zicato.adapters.adk import (  # noqa: PLC0415
        entrypoint_outside_trees_notice,
        entrypoint_snapshot_origin_error,
    )

    refusal = entrypoint_snapshot_origin_error(entrypoint, mutable_trees)
    if refusal is not None:
        raise click.BadParameter(refusal, param_hint="--adk")
    notice = entrypoint_outside_trees_notice(entrypoint, mutable_trees)
    if notice is not None:
        click.echo(f"NOTICE: {notice}")


@click.command(
    name="register",
    short_help="Advanced: record the adapter entrypoint, mutable trees, and contract paths.",
)
@click.option(
    "--workspace",
    default=".zicato",
    type=click.Path(file_okay=False, dir_okay=True),
    show_default=True,
    help="Workspace directory to update.",
)
@click.option(
    "--adk",
    "entrypoint",
    required=True,
    help=(
        "Adapter entrypoint in 'module.path:agent_symbol' form. Either inside a "
        "--mutable-tree (its TOP-LEVEL module is the tree's basename) or outside "
        "every tree, which is the dependency shape: the harness imports the "
        "mutable trees, and each tree is verified to have loaded from the "
        "generation snapshot per run instead."
    ),
)
@click.option(
    "--mutable-tree",
    "mutable_trees",
    multiple=True,
    type=click.Path(),
    help=(
        "Source root the proposer is allowed to mutate (repeatable). Its "
        "BASENAME must be the importable package name — the snapshot exposes "
        "each tree under its basename on sys.path."
    ),
)
@click.option(
    "--board",
    "board_path",
    default=None,
    type=click.Path(),
    help="Canonical board.jsonl path (default: <workspace_parent>/board.jsonl).",
)
@click.option(
    "--brief",
    "brief_path",
    default=None,
    type=click.Path(),
    help="Canonical proposer-brief path (default: <workspace_parent>/brief.md).",
)
@click.option(
    "--scoring",
    "scoring_path",
    default=None,
    type=click.Path(),
    help="Canonical scoring.json path (default: <workspace_parent>/scoring.json).",
)
@click.option(
    "--proposer-path",
    "proposer_path",
    default=None,
    type=click.Path(),
    help=(
        "Proposer dir (proposers/<name>/ — skills + optional agent.py). "
        "Absent ⇒ the built-in default proposer. Part of the contract: "
        "configuring it (or editing a skill) rolls the epoch."
    ),
)
def register_cmd(
    workspace: str,
    entrypoint: str,
    mutable_trees: tuple[str, ...],
    board_path: str | None,
    brief_path: str | None,
    scoring_path: str | None,
    proposer_path: str | None,
) -> None:
    """Advanced: record the adapter entrypoint, mutable trees, and contract paths.

    Off the happy path — `zicato evolve` resolves the contract itself.
    Run `register` by hand only to pin the contract source paths up
    front, or to point the workspace at a different agent / brief.

    Merges into the existing config.json rather than replacing it, so
    any keys `zicato init` wrote (instance_id, created_at) are
    preserved.

    The canonical contract source paths (board / proposer brief /
    scoring) default to the conventional location alongside the
    workspace. They are stored under the `contract` key and read back
    by contract-hash auto-epoching on every `evolve`.

    `--proposer-path` is optional and stored under the same `contract`
    key as `contract.proposer_path` (absolutised). It is itself a
    contract input — configuring a proposer dir, or editing one of its
    skills, rolls the epoch on the next `evolve`. Omitting the flag
    leaves the key unset, which resolves to the built-in default
    proposer.
    """
    _validate_entrypoint(entrypoint, mutable_trees)
    workspace_root = Path(workspace)
    if not workspace_is_initialized(workspace_root):
        raise click.UsageError(
            f"workspace {workspace_root!s} is not initialized; run `zicato init` first"
        )

    config = read_workspace_config(workspace_root)
    config["adk_entrypoint"] = entrypoint
    # ``mutable_trees`` and ``source_roots`` are the same concept under
    # two historical names: ``zicato mutations`` and ``zicato propose``
    # read ``source_roots``; the adapter factory reads ``mutable_trees``.
    # Writing both keeps the readers consistent without forcing a
    # workspace-format migration.
    config["mutable_trees"] = list(mutable_trees)
    config["source_roots"] = list(mutable_trees)

    # Canonical contract source paths. The operator's live, editable
    # copies — frozen into epochs/{id}/ on each epoch creation / roll.
    from zicato.epoch.contract import default_contract_paths  # noqa: PLC0415

    defaults = default_contract_paths(workspace_root)
    # The proposer brief is recorded under the contract's ``rubric_path``
    # key: that key name is the on-disk contract format read back by
    # ``resolve_contract_inputs`` (a non-CLI module). The operator-facing
    # flag is ``--brief``; only the persisted key keeps the older name.
    contract_block: dict[str, str] = {
        "board_path": str(
            Path(board_path).resolve() if board_path is not None else defaults["board_path"]
        ),
        "rubric_path": str(
            Path(brief_path).resolve() if brief_path is not None else defaults["rubric_path"]
        ),
        "scoring_path": str(
            Path(scoring_path).resolve() if scoring_path is not None else defaults["scoring_path"]
        ),
    }
    # ``contract.proposer_path`` is optional. It is written only when the
    # operator passes ``--proposer-path`` — an absent flag leaves the key
    # out so ``resolve_contract_inputs`` falls back to the built-in
    # default proposer (``None``). Absolutised like the other contract
    # paths so the persisted value is stable regardless of CWD.
    if proposer_path is not None:
        contract_block["proposer_path"] = str(Path(proposer_path).resolve())
    config["contract"] = contract_block
    write_workspace_config(workspace_root, config)

    click.echo(
        f"registered entrypoint {entrypoint!r} with {len(mutable_trees)} "
        f"mutable tree(s) in {workspace_root!s}"
    )


__all__ = ["register_cmd"]
