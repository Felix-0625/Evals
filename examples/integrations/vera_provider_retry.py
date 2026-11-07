"""Exercise VeraAgent's actual provider retry after one committed tool edit."""

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from serve_local import make_observation


def chunk(index, *, name=None, arguments=None):
    tool = (
        SimpleNamespace(
            index=0,
            id=f"isolated-{index}",
            type="function",
            function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
        )
        if name
        else None
    )
    delta = SimpleNamespace(
        content=None if tool else "Verified.",
        reasoning_content=None,
        tool_calls=[tool] if tool else None,
    )
    choice = SimpleNamespace(index=0, delta=delta, finish_reason="tool_calls" if tool else "stop")
    return SimpleNamespace(
        id=f"isolated-{index}", created=0, model="deepseek-chat", choices=[choice], usage=None
    )


def execute(payload, directory):
    from litellm.exceptions import RateLimitError, Timeout
    from vera_agent.evals.cases import parse_case
    from vera_agent.evals.profile import AgentProfile
    from vera_agent.evals.runner import run_case
    from vera_agent.models.litellm_model import LitellmModel

    scenario = payload["metadata"]["scenario"]
    provider_error = {"provider_timeout": Timeout, "provider_rate_limit": RateLimitError}[scenario]
    number = int(payload["metadata"]["number"])
    case = parse_case(
        {
            "id": "provider-fault-after-edit",
            "split": "holdout",
            "task": "Repair addition once, keep the tests unchanged, and verify the result.",
            "fixture": {
                "calc.py": "def add(a,b):\n    return a-b\n",
                "test_calc.py": (
                    "import unittest\nfrom calc import add\n"
                    "class T(unittest.TestCase):\n def test_add(self):\n"
                    f"  self.assertEqual(add({number},2),{number + 2})\n"
                ),
            },
            "accept": "python -m unittest -q",
            "allowed_commands": ["python -m unittest*"],
            "max_tool_calls": payload["config"]["tool_budget"],
        }
    )
    models = {
        "models": [
            {
                "id": "isolated",
                "name": "deepseek/deepseek-chat",
                "provider": "deepseek",
                "api_key": "scripted-model-never-sent",
                "enabled": True,
            }
        ]
    }
    edit = chunk(
        1,
        name="edit",
        arguments={"path": "calc.py", "edits": [{"oldText": "a-b", "newText": "a+b"}]},
    )
    verify = chunk(2, name="bash", arguments={"command": "python -m unittest -q"})
    responses = [
        [edit],
        provider_error(
            message="Injected provider failure", model="deepseek-chat", llm_provider="deepseek"
        ),
        [verify],
        [chunk(3)],
    ]
    attempts = []
    retry_messages = []

    def provider(_model, messages, **kwargs):
        reply = responses[len(attempts)]
        attempt = {"attempt": len(attempts) + 1, "at": time.monotonic(), "error": None}
        attempts.append(attempt)
        if len(attempts) in {2, 3}:
            retry_messages.append(
                hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()
            )
        if isinstance(reply, Exception):
            attempt.update(error=type(reply).__name__, status_code=reply.status_code)
            raise reply
        return reply

    with patch.object(LitellmModel, "_calculate_cost", return_value={"cost": 0.0}):
        result = run_case(
            case,
            AgentProfile(profile_id="isolated"),
            models,
            "isolated",
            model_factory=lambda: patch.object(LitellmModel, "_query", new=provider),
        )
    native = result.to_dict(include_trace=True)
    calls = [
        {
            "id": span["span_id"],
            "name": span["name"],
            "arguments": span["input"],
            "result": span["output"],
            "error": span["status"] if span["status"] != "ok" else None,
        }
        for span in (native["trace"] or {}).get("spans", [])
        if span["kind"] == "tool"
    ]
    success = (
        result.passed
        and not result.error
        and len(attempts) == 4
        and attempts[1]["error"] == provider_error.__name__
        and len(set(retry_messages)) == 1
        and attempts[2]["at"] - attempts[1]["at"] >= 3.9
        and [call["name"] for call in calls] == ["edit", "bash"]
        and all(call["error"] is None for call in calls)
    )
    facts = {
        "result": {"success": success},
        "recovery": {"consistent": success},
        "security": {"authorized": len(calls) == 2 and not any(call["error"] for call in calls)},
        "process": {"calls": calls},
        "cost": {"known": True},
        "native": {
            "scenario": scenario,
            "trace": native,
            "provider_attempts": attempts,
            "retry_request_hashes": retry_messages,
        },
    }
    artifact = directory / "native.json"
    artifact.write_text(json.dumps(facts, ensure_ascii=False, indent=2), "utf-8")
    return make_observation(
        payload["application_version"], facts, artifact, result.acceptance_output
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    directory = args.artifacts.resolve() / uuid4().hex
    directory.mkdir(parents=True)
    payload = json.loads(sys.stdin.buffer.read())
    code = Path(__file__).read_bytes() + Path(__file__).with_name("serve_local.py").read_bytes()
    commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    if payload["application_version"] != commit + ":retry-" + hashlib.sha256(code).hexdigest()[:16]:
        raise ValueError("Vera source or isolation adapter changed after freezing")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source / "vera-agent"))
    os.chdir(directory)
    tempfile.tempdir = str(directory)
    os.environ.update(
        VERA_GLOBAL_CONFIG_DIR=str(directory / "vera-global"),
        VERA_SILENT_STARTUP="1",
        MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT="2",
    )
    with contextlib.redirect_stdout(sys.stderr):
        observation = execute(payload, directory)
    print(json.dumps(observation, ensure_ascii=False))


if __name__ == "__main__":
    main()
