import json

import anyio
from inspect_scout import Transcript

from evals_app.analysis import business_scanner, findings
from evals_app.calibration import analyzer_calibration


def test_findings_require_arguments_and_separate_fact_from_cause():
    first = {"id": "1", "name": "edit", "arguments": {"path": "a", "value": 1}}
    different = {**first, "id": "2", "arguments": {"path": "a", "value": 2}}
    assert findings({"facts": {"process": {"calls": [first, different]}}}) == []
    assert findings({"facts": {"process": {"calls": ["edit", "edit"]}}}) == []
    duplicated = findings({"facts": {"process": {"calls": [first, {**first, "id": "3"}]}}})
    assert len(duplicated) == 1
    assert duplicated[0]["path"] == "process.calls.1"
    assert duplicated[0]["related_paths"] == ["process.calls.0"]
    assert "may" in duplicated[0]["hypothesis"]


def test_analyzer_calibration_covers_missing_negative_and_recovery_cases():
    result = analyzer_calibration()
    assert result["correct"] == result["expected"] == 13
    assert findings(None)[0]["path"] == "attempts"


def test_required_retrieval_losses_are_attributed_to_observed_stages():
    retrieval = {
        "required": ["expiry", "jurisdiction"],
        "recalled": ["expiry", "jurisdiction", "policy", "unlabeled"],
        "reranked": ["jurisdiction", "policy"],
        "context": ["policy"],
    }
    result = findings({"facts": {"retrieval": retrieval}})
    assert [(row["stage"], row["document_ids"]) for row in result] == [
        ("rerank", ["expiry"]),
        ("context", ["jurisdiction"]),
    ]
    assert result[0]["path"] == "retrieval.reranked"
    assert "retrieval.recalled" in result[0]["related_paths"]
    assert "retrieval.required" in result[0]["related_paths"]


def test_missing_stage_and_unlabeled_partial_drop_do_not_attribute_reranking():
    retrieval = {"recalled": ["a", "b"], "context": ["b"]}
    assert findings({"facts": {"retrieval": retrieval}}) == []
    required = {**retrieval, "required": ["a"]}
    (detected,) = findings({"facts": {"retrieval": required}})
    assert detected["stage"] == "unobserved"
    assert detected["document_ids"] == ["a"]
    assert findings({"facts": {"retrieval": {**required, "context": None}}}) == []


def test_scout_uses_sample_observation_online_and_from_persisted_logs():
    sample = {
        "evals_observation": {
            "facts": {
                "retrieval": {
                    "required": ["expiry"],
                    "recalled": ["expiry", "policy"],
                    "reranked": ["policy"],
                    "context": ["policy"],
                }
            }
        }
    }

    async def scan():
        scanner = business_scanner()
        for metadata in (sample, {"sample_metadata": sample}):
            result = await scanner(Transcript(transcript_id="rerank", metadata=metadata))
            assert result.value is True
            (detected,) = json.loads(result.explanation)
            assert detected["stage"] == "rerank" and detected["document_ids"] == ["expiry"]

    anyio.run(scan)
