"""Read the most recent recorded racing tournament in an epoch."""

from __future__ import annotations

from typing import Any

from zicato.epoch._storage import RecordError
from zicato.query.inputs import EpochInputs
from zicato.query.paths import WorkspacePaths, _resolve_epoch_id
from zicato.query.promoted_head import champion_history
from zicato.tournament.records import field_tournament_records


def build_racing_field(
    paths: WorkspacePaths, epoch_id: str | None = None, *, inputs: EpochInputs | None = None
) -> dict[str, Any]:
    """Serve recorded rungs and the final comparison without inferring a winner."""
    try:
        epoch_id = _resolve_epoch_id(paths, epoch_id)
        if inputs is not None:
            inputs.check(paths, epoch_id)
        if epoch_id is None:
            return {"epoch_id": None, "present": False}
        racing = [
            body
            for record in field_tournament_records(paths.root, epoch_id)
            if (body := record.to_dict())["structure"] == "racing" and body["rounds"]
        ]
    except (ValueError, RecordError) as exc:
        return {"epoch_id": epoch_id, "present": False, "unreadable": str(exc)}
    if not racing:
        return {"epoch_id": epoch_id, "present": False}
    record = max(racing, key=lambda body: body["ran_at"])
    return {
        **record,
        "present": True,
        "champion_lineage": champion_history(paths, epoch_id),
        "source": "record",
    }
