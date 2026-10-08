# Endpoints de jobs en segundo plano: 202 + 409 si ya hay uno en curso.
import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from permisos import usuario_autenticado
from routers import jobs
from jobs import amazon_ventas


def _app():
    app = FastAPI()
    app.include_router(jobs.router)
    app.dependency_overrides[usuario_autenticado] = lambda: "tester"
    return TestClient(app)


def test_run_responde_202_y_segundo_intento_es_409():
    import time

    async def fake_run(dry_run=None, motivo=""):
        await asyncio.sleep(3)
        amazon_ventas.LAST_RUN.clear()
        amazon_ventas.LAST_RUN.update({"estado": "ok", "motivo": motivo})

    amazon_ventas.run_job = fake_run
    amazon_ventas.LAST_RUN.clear()
    amazon_ventas.LAST_RUN.update({"estado": "nunca"})
    with _app() as client:
        r1 = client.post("/jobs/amazon/run", json={"motivo": "test"})
        assert r1.status_code == 202, r1.text
        assert r1.json()["estado"] == "running"

        r2 = client.post("/jobs/amazon/run", json={"motivo": "test"})
        assert r2.status_code == 409
        assert "ya en curso" in r2.json()["detail"]

        st = client.get("/jobs/amazon/status")
        assert st.json()["ultimo"]["estado"] == "running"

        time.sleep(4)
        st = client.get("/jobs/amazon/status")
        assert st.json()["ultimo"]["estado"] == "ok"
