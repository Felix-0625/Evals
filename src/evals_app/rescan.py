"""Offline Scout analysis of registered development logs without application execution."""

from typing import TYPE_CHECKING, Any

from evals_app.analysis import business_scanner
from evals_app.models import identifier, now
from inspect_scout import scan, scan_results_df, transcripts_from

if TYPE_CHECKING:
    from evals_app.service import Service


def rescan(service: "Service", experiment_id: str) -> dict[str, Any]:
    experiment = service.store.read()["experiments"][experiment_id]
    if experiment["status"] not in {"completed", "blocked", "interrupted", "cancelled"}:
        raise ValueError("Offline rescanning requires an inactive experiment")
    paths = sorted(
        {
            trial["log"]
            for key, run in experiment["runs"].items()
            if key.endswith((":train", ":selection"))
            for trial in run.get("trial_index", run.get("trials", []))
        }
    )
    if not paths:
        raise ValueError("No completed development logs are available")
    output = service.store.directory / "rescans" / experiment_id / identifier("scan")
    status = scan(
        scanners=[business_scanner()],
        transcripts=transcripts_from(paths),
        scans=str(output),
        model="mockllm/model",
        display="none",
        fail_on_error=True,
    )
    result = scan_results_df(status.location)
    tables = {name: frame.to_json(orient="records") for name, frame in result.scanners.items()}
    record = {
        "location": status.location,
        "complete": status.complete,
        "error_count": len(status.errors),
        "logs": paths,
        "results_json": tables,
        "analyzer_version": "scout-rules-v1",
        "application_calls": 0,
        "created_at": now(),
    }

    def update(state: dict[str, Any]) -> None:
        state["experiments"][experiment_id].setdefault("rescans", []).append(record)

    service.store.update(update)
    return record
