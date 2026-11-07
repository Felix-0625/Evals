import json
import sys
from datetime import datetime

import anyio
import pytest

from evals_app.adapters import command_arguments, execute
from evals_app.fixtures import fixture_plan


def test_cli_paths_with_spaces_and_literal_arguments(tmp_path):
    directory = tmp_path / "path with spaces"
    directory.mkdir()
    script = directory / "application.py"
    script.write_text(
        "import sys,json\n"
        "payload=json.load(sys.stdin)\n"
        "print(json.dumps({'output': sys.argv[1], "
        "'application_version': payload['application_version'],"
        " 'cost': 0, 'cost_source': 'local executable', 'pricing_version': 'v1'}))\n",
        encoding="utf-8",
    )
    plan = fixture_plan()
    literal = 'space and "quotes" and \\slashes'
    app = plan.application.model_copy(
        update={"adapter": "cli", "entrypoint": json.dumps([sys.executable, str(script), literal])}
    )
    result = anyio.run(execute, app, plan.cases[0], app.config, {"epoch": 1}, 0, [])
    assert result["observation"]["output"] == literal
    attempt = result["attempts"][0]
    assert datetime.fromisoformat(attempt["started_at"]) <= datetime.fromisoformat(
        attempt["finished_at"]
    )
    assert attempt["cost"] == 0


@pytest.mark.parametrize("entrypoint", ["[]", "[1]", '["ok",null]'])
def test_invalid_cli_array_is_rejected(entrypoint):
    with pytest.raises(ValueError):
        command_arguments(entrypoint)
