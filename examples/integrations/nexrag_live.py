"""Exercise NexRAG's native tool and evidence loop with a real model and public fixtures."""

import argparse
import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from serve_local import make_observation


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    directory = args.artifacts.resolve() / uuid4().hex
    directory.mkdir(parents=True)
    source = args.source.resolve()
    os.chdir(directory)
    os.environ.update(
        ENABLE_MEMORY="false",
        ENABLE_TRACING="false",
        ENABLE_WEB_SEARCH="false",
        OPS_DB_PATH=":memory:",
        TENANT_ACCESS_PATH=str(directory / "absent-access.yaml"),
        RETRIEVAL_API_BASE_URL="http://127.0.0.1:9",
        EMBEDDING_API_BASE_URL="http://127.0.0.1:9/v1",
    )
    sys.path.insert(0, str(source))
    sys.dont_write_bytecode = True
    payload = json.loads(sys.stdin.buffer.read())
    model_name = os.environ.get("EVALS_LIVE_MODEL", "deepseek-flash")
    with contextlib.redirect_stdout(sys.stderr):
        from pydantic_ai import UsageLimits
        from pydantic_ai.messages import ModelMessagesTypeAdapter, ToolReturnPart
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.deepseek import DeepSeekProvider
        from src.agent.agent import AgenticRunner
        from src.agent.dependencies import AgentDeps, UserContext
        from src.agent.grounding import EvidenceLedger
        from src.agent.run_options import EvaluationRun
        from src.evolution.artifacts import default_params
        from src.midplatform.data_policy import save_account_policy, save_resource_policy

        email = "live-acceptance@example.invalid"
        scope = "generated-public-policy"
        save_resource_policy("knowledge", scope, "public", True, False, False, "acceptance")
        save_account_policy(
            email,
            {"external_models": True, "allowed_model_ids": ["live"]},
            "acceptance",
        )
        model_record = {
            "id": "live",
            "provider": "deepseek",
            "model_name": model_name,
            "base_url": "https://api.deepseek.com",
        }
        number = int(payload["metadata"].get("number", 41))
        expected = f"LIVE-{number}-{number * 17}"
        expiry = str(2040 + number % 10)
        documents = [
            {
                "source": "Generated acceptance policy",
                "content": (
                    f"公开测试政策第 {number} 版。申请代码为 {expected}，"
                    f"有效期为 {expiry} 年。只适用于本次生成测试。"
                ),
                "score": 1.0,
                "metadata": {"collection_name": scope, "version": str(number)},
            }
        ]
        context = UserContext(
            user_id="live-acceptance",
            email=email,
            tenant_id="",
            knowledge_scopes=[scope],
            capture_memory=False,
        )
        params = default_params()
        deps = AgentDeps(context, params=params)
        deps.model_record = model_record
        deps.ledger = EvidenceLedger(prompt=payload["input"])
        deps.evaluation = EvaluationRun(
            guidance=payload["config"]["prompt_rule"],
            params=params,
            knowledge_documents=documents,
        )
        model = OpenAIChatModel(
            model_name,
            provider=DeepSeekProvider(api_key=os.environ["DEEPSEEK_API_KEY"]),
        )
        agent = AgenticRunner()._build_agent(initial_model=model)

        async def run():
            return await agent.run(
                payload["input"],
                deps=deps,
                model_settings={
                    "max_tokens": 2048,
                    "extra_body": {"thinking": {"type": "disabled"}},
                },
                usage_limits=UsageLimits(request_limit=6),
            )

        result = asyncio.run(asyncio.wait_for(run(), timeout=120))
        messages = result.all_messages()
        usage = result.usage
        returns = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        retrieved = any(part.tool_name == "search_knowledge_documents" for part in returns)
        returned_documents = [
            row
            for part in returns
            if part.tool_name == "search_knowledge_documents" and isinstance(part.content, dict)
            for row in part.content.get("documents", [])
        ]
        output = str(result.output)
        cost = (usage.input_tokens * 0.15 + usage.output_tokens * 0.60) / 1_000_000
        native = {
            "messages": ModelMessagesTypeAdapter.dump_python(messages, mode="json"),
            "usage": {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "requests": usage.requests,
            },
            "expected": expected,
            "expected_expiry": expiry,
            "answer": output,
            "scope": scope,
        }
    artifact = directory / "native-result.json"
    artifact.write_text(json.dumps(native, ensure_ascii=False, indent=2), "utf-8")
    facts = {
        "result": {"success": expected in output and expiry in output},
        "retrieval": {"used_authorized_knowledge": retrieved},
        "security": {
            "authorized_scope": bool(returned_documents)
            and all(
                row.get("metadata", {}).get("collection_name") == scope
                for row in returned_documents
            )
        },
        "cost": {"model_calls": usage.requests},
        "native": native,
    }
    observation = make_observation(payload["application_version"], facts, artifact, output)
    observation.update(
        cost=cost,
        cost_source="provider token usage with frozen off-peak tariff; cache discount not deducted",
        pricing_version="deepseek-2026-11-07-offpeak-usd-upper-estimate",
    )
    print(json.dumps(observation, ensure_ascii=False))


if __name__ == "__main__":
    main()
