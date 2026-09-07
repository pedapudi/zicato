"""The environment report and direct access sites share one declaration set."""

from pathlib import Path

import pytest

from tools.check_environment_boundaries import boundary_errors, environment_accesses
from zicato.config import ENVIRONMENT_BOUNDARIES, describe_env_vars


def test_every_environment_access_has_a_documented_owner():
    assert boundary_errors(Path(__file__).resolve().parents[1]) == []
    assert len(describe_env_vars()) == sum(len(owner.variables) for owner in ENVIRONMENT_BOUNDARIES)


@pytest.mark.parametrize(
    "source",
    [
        "import os\ndef caller(): return os.getenv('UNDECLARED')",
        "import os as system\ndef caller(): return system.environ['UNDECLARED']",
        "from os import environ as process\ndef caller(): return process.get('UNDECLARED')",
        "from os import getenv as read\ndef caller(): return read('UNDECLARED')",
        "import os\nmodule = os\ndef caller(): return module.environ",
    ],
)
def test_aliases_do_not_hide_environment_access(source):
    assert environment_accesses(source) == {"caller"}
