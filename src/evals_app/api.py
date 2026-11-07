"""Evals routes composed into the original local Inspect Viewer service."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from inspect_ai._view.fastapi_server import standalone_view_app, view_server_app
from inspect_ai._view.network import resolve_viewer_network_policy
from pydantic import Field

from evals_app.calibration import calibrate, calibrate_business
from evals_app.fixtures import fixture_plan
from evals_app.models import Application, Case, Contract, Plan, Proposal
from evals_app.monitoring import (
    active_experiences,
    disable_experience,
    evaluate_study,
    experience,
    freeze_study,
    observe,
    promote_strategy,
    regression_case,
)
from evals_app.policies import SearchPolicy
from evals_app.semantic import SemanticValidationCase, schedule_calibration
from evals_app.service import Service
from evals_app.store import Conflict


class CreateRequest(Contract):
    plan: Plan
    proposals: list[Proposal] = Field(default_factory=list, max_length=20)
    use_gepa: bool | None = None
    policy: SearchPolicy | None = None


class StartRequest(Contract):
    fingerprint: str


class RequeueRequest(Contract):
    sample_id: str
    epoch: int = Field(ge=1)


class OperationRequest(Contract):
    experiment_id: str
    candidate_id: str
    expected_baseline: str
    requested_version: str
    rollback: str | None = None


class ReceiptRequest(Contract):
    actual_version: str
    config_fingerprint: str
    status: Literal["applied", "partial_failure", "failed", "cancel_pending_confirmation"]
    evidence: dict[str, Any]


class RecoveryRequest(Contract):
    fingerprint: str
    costs: dict[str, float]
    evidence: dict[str, Any]


class ObservationRequest(Contract):
    operation_id: str
    plan: Plan
    compare_previous: bool = False


class StudyRequest(Contract):
    version: str
    arms: dict[str, list[str]]
    budget_per_arm: float = Field(gt=0)


class StrategyPromotionRequest(Contract):
    expected_default_version: str = Field(min_length=1)


class CalibrationRequest(Contract):
    application_id: str
    dataset_version: str
    annotations: list[dict[str, Any]]
    minimum_accuracy: float = Field(default=1, gt=0, le=1)


class SemanticCalibrationRequest(Contract):
    cases: list[SemanticValidationCase] = Field(min_length=3, max_length=500)


class RegressionRequest(Contract):
    experiment_id: str
    sample_id: str
    case: Case
    redaction_note: str = Field(min_length=1)


class OwnedLogs:
    def __init__(self, service: Service) -> None:
        self.service = service
        self.root = (service.store.directory / "logs").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def allowed(self, path: str) -> bool:
        if "://" in path and not path.startswith("file://"):
            return False
        try:
            from inspect_ai._util.file import local_path

            resolved = Path(local_path(path)).resolve()
            relative = resolved.relative_to(self.root)
            return (
                not relative.parts or relative.parts[0] in self.service.store.read()["experiments"]
            )
        except (ValueError, OSError):
            return False

    async def can_read(self, request: Request, file: str) -> bool:
        return self.allowed(file)

    async def can_list(self, request: Request, dir: str) -> bool:
        return self.allowed(dir)

    async def can_delete(self, request: Request, file: str) -> bool:
        return False

    async def can_write(self, request: Request, file: str) -> bool:
        return False


def routes(service: Service) -> APIRouter:
    router = APIRouter(prefix="/evals", tags=["Evals"])

    @router.get("/state")
    def state() -> dict[str, Any]:
        return service.store.public_state()

    @router.get("/templates/{kind}")
    def template(kind: Literal["harness", "rag"]) -> dict[str, Any]:
        return {
            "plan": fixture_plan(kind).model_dump(mode="json"),
            "proposals": [
                Proposal(
                    changes={"reuse": True},
                    source="config",
                    hypothesis="Reuse unchanged lookup results",
                    risk="Stale identity or data",
                    expected_baseline="fixture-v1",
                ).model_dump(mode="json"),
                Proposal(
                    changes={"reuse": True, "prompt": "grounded"},
                    source="manual",
                    hypothesis="Combine grounding and tool reuse",
                    risk="Prompt rule regression",
                    expected_baseline="fixture-v1",
                ).model_dump(mode="json"),
                Proposal(
                    changes={"policy": "unsafe", "reuse": True},
                    source="manual",
                    hypothesis="Unsafe high score adversarial control",
                    risk="Unauthorized access",
                    expected_baseline="fixture-v1",
                ).model_dump(mode="json"),
            ],
            "use_gepa": True,
        }

    @router.get("/calibration")
    def calibration() -> dict[str, Any]:
        return calibrate()

    @router.post("/calibrations")
    def business_calibration(body: CalibrationRequest, idempotency_key: str = Header()) -> Any:
        app = Application.model_validate(
            service.store.read()["applications"][body.application_id]["declaration"]
        )
        result = calibrate_business(
            app, body.annotations, body.dataset_version, body.minimum_accuracy
        )

        def update(state: dict[str, Any]) -> Any:
            state["calibrations"][result["calibration"]["artifact"]] = result
            return result

        return service.store.update(update, key=f"calibration:{idempotency_key}", payload=body)

    @router.post("/queue")
    def queue(body: list[str]) -> Any:
        return service.reorder(body)

    @router.post("/experiments/{experiment_id}/analyzer-calibration")
    def analyzer_calibration(
        experiment_id: str, body: SemanticCalibrationRequest, idempotency_key: str = Header()
    ) -> Any:
        return schedule_calibration(service, experiment_id, body.cases, key=idempotency_key)

    @router.get("/analyzer-calibrations/{calibration_id}")
    def analyzer_calibration_result(calibration_id: str) -> Any:
        return service.store.read().get("analyzer_calibrations", {})[calibration_id]

    @router.post("/experiments/{experiment_id}/grader-calibration")
    def grader_calibration(
        experiment_id: str, body: SemanticCalibrationRequest, idempotency_key: str = Header()
    ) -> Any:
        return schedule_calibration(
            service, experiment_id, body.cases, key=idempotency_key, role="grading"
        )

    @router.get("/grader-calibrations/{calibration_id}")
    def grader_calibration_result(calibration_id: str) -> Any:
        return service.store.read().get("grader_calibrations", {})[calibration_id]

    @router.post("/experiments/{experiment_id}/recover")
    def recover(experiment_id: str, body: RecoveryRequest) -> Any:
        return service.recover(experiment_id, body.fingerprint, body.costs, body.evidence)

    @router.post("/observations")
    def observation(body: ObservationRequest, idempotency_key: str = Header()) -> Any:
        return observe(
            service,
            body.operation_id,
            body.plan,
            key=idempotency_key,
            compare_previous=body.compare_previous,
        )

    @router.post("/strategies")
    def study(body: StudyRequest, idempotency_key: str = Header()) -> Any:
        return freeze_study(
            service, body.version, body.arms, body.budget_per_arm, key=idempotency_key
        )

    @router.post("/strategies/{study_id}/evaluate")
    def evaluate_strategy(study_id: str) -> Any:
        return evaluate_study(service, study_id)

    @router.post("/strategies/{study_id}/promote")
    def promote(
        body: StrategyPromotionRequest, study_id: str, idempotency_key: str = Header()
    ) -> Any:
        return promote_strategy(
            service, study_id, body.expected_default_version, key=idempotency_key
        )

    @router.post("/experiences")
    def register_experience(body: dict[str, Any], idempotency_key: str = Header()) -> Any:
        return experience(service, body, key=idempotency_key)

    @router.get("/applications/{application_id}/experiences")
    def experiences(application_id: str) -> Any:
        return active_experiences(service, application_id)

    @router.post("/experiences/{experience_id}/{version}/disable")
    def disable(experience_id: str, version: str) -> Any:
        return disable_experience(service, experience_id, version)

    @router.post("/regression-cases")
    def register_regression(body: RegressionRequest, idempotency_key: str = Header()) -> Any:
        return regression_case(
            service,
            body.experiment_id,
            body.sample_id,
            body.case,
            redaction_note=body.redaction_note,
            key=idempotency_key,
        )

    @router.post("/applications")
    def register(
        body: Application,
        idempotency_key: str = Header(),
        if_match: int | None = Header(default=None),
    ) -> Any:
        return service.register(body, key=idempotency_key, revision=if_match)

    @router.post("/experiments")
    def create(
        body: CreateRequest,
        idempotency_key: str = Header(),
        if_match: int | None = Header(default=None),
    ) -> Any:
        return service.create(
            body.plan,
            body.proposals,
            key=idempotency_key,
            revision=if_match,
            use_gepa=body.use_gepa,
            policy=body.policy,
        )

    @router.get("/experiments/{experiment_id}")
    def experiment(experiment_id: str, view: Literal["full", "summary"] = "full") -> Any:
        return (
            service.store.experiment_view(experiment_id)
            if view == "summary"
            else service.experiment(experiment_id)
        )

    @router.post("/experiments/{experiment_id}/candidates")
    def propose(experiment_id: str, body: Proposal, idempotency_key: str = Header()) -> Any:
        return service.submit(experiment_id, body, key=idempotency_key)

    @router.post("/experiments/{experiment_id}/configuration-candidates")
    def configurations(experiment_id: str, idempotency_key: str = Header()) -> Any:
        return service.configuration_candidates(experiment_id, key=idempotency_key)

    @router.post("/experiments/{experiment_id}/samples/requeue")
    def requeue(experiment_id: str, body: RequeueRequest) -> Any:
        return service.runtime.native(
            experiment_id, "requeue", sample_id=body.sample_id, epoch=body.epoch
        )

    @router.post("/experiments/{experiment_id}/start")
    def start(experiment_id: str, body: StartRequest) -> Any:
        return service.start(experiment_id, body.fingerprint)

    @router.post("/experiments/{experiment_id}/control/{action}")
    def control(experiment_id: str, action: Literal["pause", "resume", "cancel"]) -> Any:
        return service.control(experiment_id, action)

    @router.get("/experiments/{experiment_id}/native")
    def native(experiment_id: str) -> Any:
        return service.runtime.native(experiment_id, "tasks")

    @router.post("/operations")
    def operation(body: OperationRequest, idempotency_key: str = Header()) -> Any:
        return service.operation(
            body.experiment_id,
            body.candidate_id,
            body.expected_baseline,
            body.requested_version,
            key=idempotency_key,
            rollback=body.rollback,
        )

    @router.get("/operations/{operation_id}")
    def get_operation(operation_id: str) -> Any:
        return service.store.read()["operations"][operation_id]

    @router.post("/operations/{operation_id}/receipt")
    def receipt(operation_id: str, body: ReceiptRequest, idempotency_key: str = Header()) -> Any:
        return service.receipt(
            operation_id, executor="local", key=idempotency_key, **body.model_dump()
        )

    return router


def create_app(directory: Path, *, port: int = 7575, background: bool = True) -> Any:
    # First release deliberately supports one trusted operator on loopback only.
    service = Service(directory, background=background)
    logs = OwnedLogs(service)
    api = view_server_app(access_policy=logs, default_dir=str(logs.root), allow_direct_urls=False)
    api.include_router(routes(service))
    api.state.evals_service = service

    @api.exception_handler(Conflict)
    async def conflict(request: Request, error: Conflict) -> JSONResponse:
        return JSONResponse({"error": str(error)}, status_code=409)

    @api.exception_handler(KeyError)
    async def missing(request: Request, error: KeyError) -> JSONResponse:
        return JSONResponse({"error": "Object does not belong to this workspace"}, status_code=404)

    @api.exception_handler(ValueError)
    async def invalid(request: Request, error: ValueError) -> JSONResponse:
        return JSONResponse({"error": str(error)}, status_code=422)

    @api.exception_handler(PermissionError)
    async def unauthorized(request: Request, error: PermissionError) -> JSONResponse:
        return JSONResponse({"error": str(error)}, status_code=403)

    @api.exception_handler(RuntimeError)
    async def unavailable(request: Request, error: RuntimeError) -> JSONResponse:
        return JSONResponse({"error": str(error)}, status_code=409)

    policy = resolve_viewer_network_policy(bind_host="127.0.0.1", port=port)
    viewer = standalone_view_app(log_dir=str(logs.root), network_policy=policy, api_app=api)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            service.close()

    app = FastAPI(lifespan=lifespan)
    app.state.evals_service = service
    app.mount("/", viewer)
    return app
