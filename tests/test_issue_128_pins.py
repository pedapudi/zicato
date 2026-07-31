"""Triage pins for issue #128 — ``derive_generation(reconcile=...)``.

VERDICT: not reproducible on this repo. No ``reconcile`` parameter exists on
``GenerationStore.derive_generation`` or on either implementation, no docstring
or design document claims one, and neither store raises ``NotImplementedError``
anywhere. ``git log -S"reconcile="`` over all of history returns nothing — the
string has never appeared in the tree. There is no documented argument that
raises, because there is no argument.

Both remedies the issue offers are therefore already satisfied by the status
quo: the parameter is absent from the protocol signature, and nothing documents
it. What remains worth holding is the invariant the issue would have violated —
a parameter must not be accepted, or described, unless it works. These pins are
plain guards (they PASS today, and are NOT xfail: there is nothing to fix).
They fail the moment someone lands a documented-but-unimplemented ``reconcile``.

If the reporter is running a fork or a local branch, the evidence to reopen on
is a ``NotImplementedError`` traceback naming a file in this tree.
"""

from __future__ import annotations

import inspect

from zicato.epoch.genstore import DirectoryGenerationStore, GenerationStore
from zicato.epoch.git_genstore import GitGenerationStore

_STORES = (GenerationStore, DirectoryGenerationStore, GitGenerationStore)


def test_no_store_accepts_an_undocumented_reconcile_parameter() -> None:
    """``reconcile`` is on neither the protocol nor either implementation."""
    for store in _STORES:
        params = inspect.signature(store.derive_generation).parameters
        assert "reconcile" not in params, f"{store.__name__} grew a reconcile parameter"


def test_no_store_documents_a_reconcile_parameter() -> None:
    """Nothing in the ``derive_generation`` docstrings promises reconciliation.

    The issue's premise is a doc claim without an implementation. Holding the
    doc side too means the pair can never drift back apart in either order.
    """
    for store in _STORES:
        doc = inspect.getdoc(store.derive_generation) or ""
        assert "reconcile" not in doc.lower(), f"{store.__name__} documents reconcile"


def test_neither_store_implementation_raises_not_implemented_error() -> None:
    """No ``derive_generation`` path is a stub.

    The issue reports both stores raising ``NotImplementedError``. Read the
    source of each concrete implementation rather than calling it, so the guard
    holds without materialising a workspace.
    """
    for store in (DirectoryGenerationStore, GitGenerationStore):
        source = inspect.getsource(store.derive_generation)
        assert "NotImplementedError" not in source, f"{store.__name__} has a stub path"
