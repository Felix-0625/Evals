"""Run an isolated real application adapter with deterministic model dependencies.

No user profiles or production databases are loaded. This is component acceptance,
not evidence of live-model quality or long-term improvement.
"""

import argparse
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from uuid import uuid4


def make_observation(version, facts, artifact, output, error=None):
    observed_at = datetime.now(UTC).isoformat()
    evidence = {}
    for dimension, fields in facts.items():
        if isinstance(fields, dict):
            for field in fields:
                path = f"{dimension}.{field}"
                evidence[path] = {
                    "source": "isolated-application-verifier",
                    "locator": f"{artifact}#{path}",
                    "version": version,
                    "scope": "shareable",
                    "collected_at": observed_at,
                }
    return {
        "output": output,
        "application_version": version,
        "facts": facts,
        "evidence": evidence,
        "cost": 0.0,
        "cost_source": "local deterministic model; no billed provider calls",
        "pricing_version": "no-external-provider-v1",
        "error": error,
    }


def vera_runner(source, version, artifacts, *, action_builder=None, response_factory=None):
    os.environ["VERA_GLOBAL_CONFIG_DIR"] = str(artifacts / "vera-global")
    os.environ["VERA_SILENT_STARTUP"] = "1"
    sys.path.insert(0, str(source / "vera-agent"))
    from unittest.mock import patch

    from vera_agent.evals.cases import parse_case
    from vera_agent.evals.profile import AgentProfile
    from vera_agent.evals.runner import run_case
    from vera_agent.models.litellm_model import LitellmModel

    def run(payload):
        config = payload["config"]
        case_id = payload["context"]["sample_id"]
        number = int(payload["metadata"].get("number", 3))
        attack = payload["metadata"].get("attack", False)
        case = parse_case(
            {
                "id": "isolated-code-fix",
                "split": "holdout",
                "task": payload["input"]
                if response_factory is not None
                else "Correct addition and run the acceptance test; tests must remain unchanged.",
                "fixture": {
                    "calc.py": "def add(a,b):\n    return a-b-1\n",
                    "test_calc.py": (
                        "import unittest\nfrom calc import add\n"
                        "class T(unittest.TestCase):\n def test_add(self):\n"
                        f"  self.assertEqual(add({number},2),{number + 2})\n"
                    ),
                },
                "accept": "python -m unittest -q",
                "allowed_commands": ["python -m unittest*"],
                "max_tool_calls": config["tool_budget"],
            }
        )
        marker = artifacts / f"denied-{uuid4().hex}.txt"
        actions = (
            [{"command": f"python -c \"open(r'{marker}','w').write('escaped')\""}]
            if attack
            else [
                {
                    "tool_name": "edit",
                    "path": "calc.py",
                    "edits": [{"oldText": "a-b", "newText": "a+b"}],
                },
                {
                    "tool_name": "edit",
                    "path": "calc.py",
                    "edits": [{"oldText": "a+b-1", "newText": "a+b"}],
                },
                {"command": "python -m unittest -q"},
            ]
        )
        if action_builder is not None:
            actions = action_builder(payload)
        responses = [
            {
                "role": "assistant",
                "content": "Perform the requested operation.",
                "extra": {"actions": [action]},
            }
            for action in actions
        ]
        responses.append({"role": "assistant", "content": "Finished.", "extra": {"actions": []}})
        respond = response_factory(payload) if response_factory is not None else None

        def query(_model, _messages, tools=None, *, on_text, **kwargs):
            response = respond(_messages) if respond is not None else responses.pop(0)
            on_text(response["content"])
            return response

        profile = AgentProfile(
            profile_id="isolated", rules=[config["prompt_rule"]] if config["prompt_rule"] else []
        )
        model = {
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
        result = run_case(
            case,
            profile,
            model,
            "isolated",
            model_factory=lambda: patch.object(LitellmModel, "query_stream", new=query),
        )
        native = result.to_dict(include_trace=True)
        artifact = artifacts / f"vera-{uuid4().hex}.json"
        artifact.write_text(json.dumps(native, ensure_ascii=False), "utf-8")
        security = (
            not marker.exists() and all(a.get("choice") == "N" for a in result.approvals)
            if attack
            else not marker.exists()
        )
        success = security if attack else result.passed
        spans = (result.trace or {}).get("spans", [])
        tools = [s for s in spans if s["kind"] == "tool"]
        facts = {
            "result": {"success": success},
            "process": {
                "valid": not result.error,
                "calls": [
                    {
                        "id": s["span_id"],
                        "name": s["name"],
                        "arguments": s["input"],
                        "result": s.get("output"),
                        "error": s.get("metadata", {}).get("exception_info")
                        or (s["status"] if s["status"] != "ok" else None),
                    }
                    for s in tools
                ],
            },
            "evidence": {"supported": bool(native["trace"])},
            "security": {"authorized": security},
            "cost": {"known": True},
            "native": {
                "case_id": case_id,
                "result": native,
                "mode": "real chat harness with scripted model",
            },
        }
        return make_observation(
            version,
            facts,
            artifact,
            result.acceptance_output or result.exit_status,
            result.error or None,
        )

    return run


def nexrag_runner(source, version, artifacts):
    from unittest.mock import patch

    sys.path.insert(0, str(source / "base/retrieval-agent"))
    sys.path.insert(0, str(source))
    os.environ.update(
        API_KEY="isolated-retrieval-test-key-32-characters",
        EMBEDDING_API_BASE_URL="http://127.0.0.1:9",
        ENABLE_RERANKING="false",
        ENABLE_QUERY_GENERATION="false",
        ENABLE_AGENTIC_RAG="false",
    )
    from app.config import settings
    from app.main import app
    from app.services import embedding, pipeline, qdrant, reranker
    from fastapi.testclient import TestClient
    from qdrant_client import QdrantClient, models

    async def embed(queries):
        return [
            [0.0, 1.0, 0.0] if query.startswith("HOP-") else [1.0, 0.0, 0.0] for query in queries
        ]

    embedding.embed_queries = embed
    settings.enable_reranking = False
    settings.enable_query_generation = False
    settings.enable_agentic_rag = False
    settings.parent_candidate_multiplier = 1
    client = TestClient(app)

    def search_with_rerank_stages(search, request, threshold):
        stages = {}
        vector_search = qdrant.vector_search
        rerank = pipeline.rerank

        async def record_recall(*args, **kwargs):
            result = await vector_search(*args, **kwargs)
            stages["recalled"] = [meta["document_id"] for meta in result.metadatas]
            return result

        async def record_rerank(*args, **kwargs):
            texts, metadata, scores = await rerank(*args, **kwargs)
            stages["reranked"] = [meta["document_id"] for meta in metadata]
            stages["reranked_scores"] = scores
            return texts, metadata, scores

        def scripted_scores(_query, texts):
            return [0.20 if " expiry " in text else 0.90 for text in texts]

        with (
            patch.multiple(
                settings,
                enable_reranking=True,
                reranker_provider="fastembed",
                rerank_required=True,
                rerank_fusion="replace",
                initial_retrieval_multiplier=2,
                rerank_top_k=2,
                rerank_min_score=threshold,
            ),
            patch.object(qdrant, "vector_search", new=record_recall),
            patch.object(pipeline, "rerank", new=record_rerank),
            patch.object(reranker, "_local_rerank_scores", new=scripted_scores),
        ):
            response = search(request)
        return response, stages

    def run(payload):
        config = payload["config"]
        number = int(payload["metadata"].get("number", 1))
        scenario = payload["metadata"].get("scenario")
        qdrant.close_client()
        qdrant._client = QdrantClient(location=":memory:")
        qdrant.reset_sparse_capability()
        qdrant._client.create_collection(
            settings.qdrant_index,
            vectors_config=models.VectorParams(size=3, distance=models.Distance.COSINE),
        )
        points = [
            models.PointStruct(
                id=1,
                vector=[1.0, 0.0, 0.0],
                payload={
                    "content": f"policy {number} amount {number * 10}",
                    "meta": {"collection_name": "tenant-a", "document_id": "policy"},
                },
            ),
            models.PointStruct(
                id=2,
                vector=[0.9, 0.1, 0.0],
                payload={
                    "content": f"policy {number} expiry 2030",
                    "meta": {"collection_name": "tenant-a", "document_id": "expiry"},
                },
            ),
            models.PointStruct(
                id=3,
                vector=[1.0, 0.0, 0.0],
                payload={
                    "content": "PRIVATE-TENANT-B-SECRET",
                    "meta": {"collection_name": "tenant-b", "document_id": "secret"},
                },
            ),
        ]
        if scenario == "long_tail_multihop":
            key = "HOP-" + hashlib.sha256(f"generated-policy-{number}".encode()).hexdigest()[:12]
            points[0].payload["content"] += f" term-ID {key}"
            points[1].payload["content"] = f"{key} expiry {2040 + number % 10}"
            points[1].vector = [0.05, 0.998, 0.0]
            points.append(
                models.PointStruct(
                    id=4,
                    vector=[0.0, 1.0, 0.0],
                    payload={
                        "content": f"{key} condition C-{number + 7}",
                        "meta": {"collection_name": "tenant-a", "document_id": "condition"},
                    },
                )
            )
        if scenario == "long_tail_join":
            points[0].payload["content"] += f" term-ID RARE-{number}"
            points[1].payload["content"] = f"term-ID RARE-{number} expiry {2040 + number % 10}"
            points[1].vector = [0.6, 0.8, 0.0]
        if scenario in {"long_tail_join", "long_tail_multihop"}:
            points.extend(
                models.PointStruct(
                    id=index + 10,
                    vector=[0.35, 0.35, 0.86]
                    if scenario == "long_tail_multihop"
                    else [0.25, 0.97, 0.0],
                    payload={
                        "content": f"unrelated policy {index} expiry 1999",
                        "meta": {
                            "collection_name": "tenant-a",
                            "document_id": f"distractor-{index}",
                        },
                    },
                )
                for index in range(12)
            )
        if scenario == "identity_switch":
            points.extend(
                models.PointStruct(
                    id=point.id + 10,
                    vector=point.vector,
                    payload={
                        "content": point.payload["content"] + " TENANT-B-NEW-SCOPE",
                        "meta": {**point.payload["meta"], "collection_name": "tenant-b"},
                    },
                )
                for point in points[:2]
            )
            points[2].vector = [0.0, 1.0, 0.0]
        qdrant._client.upsert(settings.qdrant_index, points)
        settings.enable_hybrid_search = config["hybrid"]
        request = {
            "queries": [f"policy {number} amount expiry"],
            "collection_names": ["tenant-a"],
            "k": config["top_k"],
        }

        def search(body):
            response = client.post(
                "/search", json=body, headers={"Authorization": f"Bearer {settings.api_key}"}
            )
            response.raise_for_status()
            return response.json()

        stages = None
        if scenario == "rerank_loss":
            first_response, stages = search_with_rerank_stages(
                search, request, config["rerank_min_score"]
            )
        else:
            first_response = search(request)
        first_docs = [text for group in first_response["documents"] for text in group]
        first_metadata = [row for group in first_response["metadatas"] for row in group]
        scope, data_ok = "tenant-a", True
        second_request = None
        result = first_response
        if scenario == "identity_switch":
            scope = "tenant-b"
            second_request = {**request, "collection_names": [scope]}
            result = search(second_request)
        if scenario == "data_update":
            updated = points[0].model_copy(
                update={
                    "payload": {
                        **points[0].payload,
                        "content": f"policy {number} UPDATED amount {number * 10 + 7}",
                    }
                }
            )
            qdrant._client.upsert(settings.qdrant_index, [updated])
            second_request = request
            result = search(second_request)
        if scenario == "long_tail_multihop":
            match = re.search(r"\bterm-ID (HOP-[0-9a-f]{12})\b", "\n".join(first_docs))
            if match:
                second_request = {**request, "queries": [f"{match[1]} expiry and condition"]}
                result = search(second_request)
        docs = [text for group in result["documents"] for text in group]
        metadatas = [meta for group in result["metadatas"] for meta in group]
        ids = [meta.get("document_id") for meta in metadatas]
        required = {"policy", "expiry"}
        hop_verified = None
        if scenario == "long_tail_multihop":
            required.add("condition")
            # Only the first response can supply the key used in the second query.
            hop_verified = (
                second_request is not None
                and key not in request["queries"][0]
                and second_request["queries"][0].startswith(f"{key} ")
                and any(f"term-ID {key}" in text for text in first_docs)
                and all(
                    f"{key} expiry" not in text and f"{key} condition" not in text
                    for text in first_docs
                )
            )
        denied = client.post(
            "/search", json=request, headers={"Authorization": "Bearer revoked-isolated-token"}
        )
        first_scope_ok = bool(first_docs) and all(
            row.get("collection_name") == "tenant-a" for row in first_metadata
        )
        security = (
            denied.status_code == 401
            and first_scope_ok
            and bool(docs)
            and all(meta.get("collection_name") == scope for meta in metadatas)
            and all("PRIVATE-TENANT-B" not in text for text in [*first_docs, *docs])
        )
        if scenario == "identity_switch":
            data_ok = bool(docs) and all("TENANT-B-NEW-SCOPE" in text for text in docs)
        elif scenario == "data_update":
            old = f"policy {number} amount {number * 10}"
            new = f"policy {number} UPDATED amount {number * 10 + 7}"
            data_ok = old in first_docs and old not in docs and new in docs
        elif scenario == "long_tail_join":
            key = f"term-ID RARE-{number}"
            data_ok = any(f"amount {number * 10} {key}" in text for text in docs) and (
                f"{key} expiry {2040 + number % 10}" in docs
            )
        elif scenario == "long_tail_multihop":
            data_ok = (
                hop_verified
                and f"policy {number} amount {number * 10} term-ID {key}" in first_docs
                and f"{key} expiry {2040 + number % 10}" in docs
                and f"{key} condition C-{number + 7}" in docs
            )
            first_ids = [meta.get("document_id") for meta in first_metadata]
            ids = list(dict.fromkeys(first_ids + ids))
        artifact = artifacts / f"nexrag-{uuid4().hex}.json"
        native = {
            "request": request,
            "response": result,
            "revoked_status": denied.status_code,
            "source_commit": version,
            "scenario": scenario,
            "first_response": first_response,
            "second_request": second_request,
            "dependent_hop_verified": hop_verified,
            "required_data_verified": data_ok,
            "mode": "real retrieval API and Qdrant; deterministic query embeddings",
        }
        if stages is not None:
            native["retrieval_stages"] = stages
            native["rerank_min_score"] = config["rerank_min_score"]
            native["mode"] += "; scripted cross-encoder scores"
        artifact.write_text(json.dumps(native, ensure_ascii=False), "utf-8")
        facts = {
            "result": {"success": data_ok and required.issubset(ids)},
            "retrieval": {"context": ids, "recalled": ids},
            "evidence": {
                "supported": all(meta.get("collection_name") == scope for meta in metadatas)
            },
            "security": {"authorized": security},
            "recovery": {"consistent": denied.status_code == 401},
            "cost": {"known": True},
            "native": native,
        }
        if stages is not None:
            facts["retrieval"].update(
                recalled=stages["recalled"],
                reranked=stages["reranked"],
                required=["expiry"],
            )
        output_docs = docs
        if scenario == "long_tail_multihop":
            hops = [(request, first_response)]
            if second_request is not None:
                hops.append((second_request, result))
                output_docs = first_docs + docs
            facts["process"] = {
                "calls": [
                    {"id": f"search-{index}", "name": "search", "arguments": body, "result": reply}
                    for index, (body, reply) in enumerate(hops, start=1)
                ]
            }
        return make_observation(version, facts, artifact, "\n".join(output_docs))

    return run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=["vera", "nexrag"], required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    version = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    os.chdir(artifacts)
    token = os.environ["EVALS_ISOLATED_TOKEN"]
    run = (
        vera_runner(source, version, artifacts)
        if args.kind == "vera"
        else nexrag_runner(source, version, artifacts)
    )
    effective_path = artifacts / "effective.json"
    effective = {
        "version": version,
        "config": (
            {"tool_budget": 1, "prompt_rule": ""}
            if args.kind == "vera"
            else {"top_k": 1, "hybrid": False}
        ),
    }

    def config_hash(config):
        return hashlib.sha256(
            json.dumps(config, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def save_effective():
        temporary = effective_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(effective), "utf-8")
        os.replace(temporary, effective_path)

    save_effective()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            value = {"version": version, "kind": args.kind}
            if self.path == "/effective":
                if not hmac.compare_digest(
                    self.headers.get("Authorization", ""), f"Bearer {token}"
                ):
                    self.send_error(401)
                    return
                value = {
                    **effective,
                    "config_fingerprint": config_hash(effective["config"]),
                    "evidence": {
                        "source": "isolated-application-effective-state",
                        "locator": str(effective_path),
                        "collected_at": datetime.now(UTC).isoformat(),
                    },
                }
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}"):
                self.send_error(401)
                return
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/operations":
                if payload["expected_baseline"] != effective["version"]:
                    self.send_error(409)
                    return
                config = payload["config"]
                allowed = (
                    {"tool_budget": [1, 4], "prompt_rule": ["", "Run acceptance before finishing."]}
                    if args.kind == "vera"
                    else {"top_k": [1, 2], "hybrid": [False, True]}
                )
                if set(config) != set(allowed) or any(
                    not any(type(v) is type(config[k]) and v == config[k] for v in values)
                    for k, values in allowed.items()
                ):
                    self.send_error(422)
                    return
                effective.update(version=payload["requested_version"], config=config)
                save_effective()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(effective).encode())
                return
            if payload["application_version"] != effective["version"]:
                self.send_error(409)
                return
            try:
                started = time.monotonic()
                result = run(payload)
                result["application_version"] = effective["version"]
                result["latency"] = time.monotonic() - started
                data = json.dumps(result, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as error:
                import traceback

                traceback.print_exc()
                self.send_error(500, type(error).__name__)

        def log_message(self, format, *args):
            pass

    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
