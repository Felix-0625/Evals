"""Worker requests to the single owning process before and after application calls."""

import json
import os
from pathlib import Path
from typing import Any

import anyio

from evals_app.models import identifier


class CallDenied(RuntimeError):
    pass


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, allow_nan=False), "utf-8")
    os.replace(temporary, path)


class CallGate:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    async def request(self, action: str, **values: Any) -> dict[str, Any]:
        name = identifier("call")
        path = self.directory / f"{name}.request.json"
        reply = self.directory / f"{name}.reply.json"
        atomic_json(path, {"action": action, **values})
        while not reply.exists():
            await anyio.sleep(0.025)
        deadline = anyio.current_time() + 1
        while True:
            try:
                result = json.loads(reply.read_text("utf-8"))
                break
            except PermissionError:
                # A newly replaced reply can be visible before Windows allows it to be opened.
                if anyio.current_time() >= deadline:
                    raise
                await anyio.sleep(0.025)
        if "error" in result:
            raise CallDenied(result["error"])
        return result

    async def reserve(self, attempt_id: str, sample_id: str, epoch: int) -> str:
        result = await self.request(
            "reserve", attempt_id=attempt_id, sample_id=sample_id, epoch=epoch
        )
        return str(result["reservation_id"])

    async def settle(
        self,
        reservation_id: str,
        actual: float | None,
        duration: float,
        usage: dict[str, Any] | None = None,
    ) -> None:
        await self.request(
            "settle", reservation_id=reservation_id, actual=actual, duration=duration, usage=usage
        )
