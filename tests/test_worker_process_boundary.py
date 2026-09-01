"""Architecture checks for tournament worker process isolation."""

from __future__ import annotations

import zicato.tournament.worker_transport as worker_transport


def test_worker_launch_has_no_process_global_override() -> None:
    """A board unit cannot replace the subprocess boundary for later units."""
    assert not hasattr(worker_transport, "use_worker_launcher")
