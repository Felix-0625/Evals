import json
import socket
from pathlib import Path

import anyio
import httpx
import pytest
from inspect_ai._control.server import ControlServer
from inspect_ai._control.transport import control_connection


def test_transport_rejects_nonlocal_or_malformed_records(tmp_path: Path):
    path = tmp_path / "control.tcp"
    for port in (0, 65536, "80", True):
        path.write_text(
            json.dumps({"protocol": "inspect-control-tcp-v1", "port": port, "token": "a" * 64}),
            "utf-8",
        )
        with pytest.raises(ValueError):
            control_connection(path)
    assert control_connection(tmp_path / "unix.sock").uds is not None


@pytest.mark.anyio
async def test_native_tcp_requires_credential():
    if hasattr(socket, "AF_UNIX"):
        pytest.skip("Windows fallback exercised on the supported Windows runtime")
    previous_headers = None
    for run in ("transport-acceptance", "transport-restarted"):
        server = ControlServer(run_id=run)
        await server.start()
        try:
            with anyio.fail_after(5):
                while not server._uvicorn_server.started:
                    await anyio.lowlevel.checkpoint()
            connection = control_connection(server.socket_path)
            assert connection.uds is None
            async with httpx.AsyncClient(base_url=connection.base_url) as client:
                assert (await client.get("/tasks")).status_code == 401
                if previous_headers is not None:
                    assert connection.headers != previous_headers
                    response = await client.get("/tasks", headers=previous_headers)
                    assert response.status_code == 401
                response = await client.get("/tasks", headers=connection.headers)
                assert response.status_code == 200
                assert isinstance(response.json(), list)
            previous_headers = connection.headers
        finally:
            await server.stop()


@pytest.fixture
def anyio_backend():
    return "asyncio"
