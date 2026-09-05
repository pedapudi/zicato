"""Temporary identity storage for tests that construct telemetry clients."""

from pathlib import Path

import pytest


def sentinel_operator_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect default identity lookup to a corrupt, temporary operator record."""
    import harmonograf_client.identity as identity

    root = tmp_path / "operator-registry"
    agents = root / "agents"
    agents.mkdir(parents=True)
    sentinel = agents / "zicato.json"
    sentinel.write_bytes(b'{"operator": true} trailing data\n')
    monkeypatch.setattr(identity, "_default_root", lambda: root)
    return sentinel
