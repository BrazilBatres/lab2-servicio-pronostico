"""API FastAPI (contrato del Anexo). Los endpoints solo orquestan: la limpieza vive en `cleaning`,
el pronóstico en `service`/`model` y la persistencia en `db`."""
from __future__ import annotations

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .cleaning import Rechazo, clean_observaciones, clean_prediccion, estado_calidad
from .schemas import (Calidad, EstadoReentreno, HealthResponse, ModeloResponse, ObservacionRequest,
                      ObservacionResponse, PrediccionRequest, PrediccionResponse, ReentrenarResponse)
from .service import Background, Health, ModelManager, forecast, replaced_notes

log = logging.getLogger("novamercado.api")


def _calidad(notas) -> Calidad:
    return Calidad(estado=estado_calidad(notas), observaciones=[n.dict() for n in notas])


def _reject(detail: str, status: int = 422) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail})


def create_app(repo, models: ModelManager, health: Health, airflow=None, background: bool = True,
               model_poll_s: float = 20, health_poll_s: float = 10) -> FastAPI:
    bg = Background()

    @asynccontextmanager
    async def lifespan(app):
        if background:
            models.load_cache()
            bg.every(model_poll_s, models.refresh)
            bg.every(health_poll_s, health.run_once)
        yield
        bg.stop.set()

    app = FastAPI(title="NovaMercado - pronóstico diario", version="1.0", lifespan=lifespan)

    async def _body(request: Request):
        raw = await request.body()
        try:
            return json.loads(raw.decode("utf-8-sig") or "null")
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise Rechazo(f"el cuerpo no es JSON válido: {e}")

    def _log(*a, **kw):
        try:
            return repo.log_solicitud(*a, **kw)
        except Exception as e:  # el registro no debe tumbar la respuesta de un rechazo
            log.warning("no se pudo registrar la solicitud: %s", e)
            return None

    @app.post("/v1/prediccion", response_model=PrediccionResponse)
    async def prediccion(request: Request):
        loaded = models.current
        try:
            raw = await _body(request)
            if loaded is None:
                return _reject("no hay un modelo cargado todavía", 503)
            limpio, notas = clean_prediccion(raw, loaded.cat)
            req = PrediccionRequest.model_validate(limpio)
        except Rechazo as e:
            _log("/v1/prediccion", locals().get("raw"), 422, None, e.detail)
            return _reject(e.detail)
        except ValidationError as e:
            _log("/v1/prediccion", locals().get("raw"), 422, None, str(e))
            return _reject(f"la solicitud no cumple el contrato: {e.errors()[0]['msg']}")
        try:
            rows, extra = forecast(loaded, repo, limpio)
        except Exception as e:
            log.exception("error al pronosticar")
            return _reject(f"no se pudo consultar la historia de ventas: {type(e).__name__}", 503)
        notas += extra
        resp = PrediccionResponse(
            tienda_id=req.tienda_id, fecha=req.fecha.isoformat(), version_modelo=loaded.version,
            predicciones=[{k: r[k] for k in ("familia_id", "venta_esperada", "cantidad_sugerida")} for r in rows],
            calidad=_calidad(notas))
        sid = _log("/v1/prediccion", raw, 200, resp.calidad.estado, None, req.tienda_id, limpio["fecha"], notas)
        try:
            repo.save_predicciones(sid, req.tienda_id, limpio["fecha"], rows, loaded.version)
        except Exception as e:
            log.warning("no se pudo guardar la predicción: %s", e)
        return resp

    @app.post("/v1/observaciones", response_model=ObservacionResponse)
    async def observaciones(request: Request):
        loaded = models.current
        try:
            raw = await _body(request)
            if loaded is None:
                return _reject("el servicio aún no tiene catálogos/modelo cargados", 503)
            limpio, notas = clean_observaciones(raw, loaded.cat)
            ObservacionRequest.model_validate(limpio)
        except Rechazo as e:
            _log("/v1/observaciones", locals().get("raw"), 422, None, e.detail)
            return _reject(e.detail)
        except ValidationError as e:
            _log("/v1/observaciones", locals().get("raw"), 422, None, str(e))
            return _reject(f"la solicitud no cumple el contrato: {e.errors()[0]['msg']}")
        try:
            notas += replaced_notes(repo, limpio)
            sid = repo.log_solicitud("/v1/observaciones", raw, 200, estado_calidad(notas), None,
                                     limpio["tienda_id"], limpio["fecha"], notas)
            n = repo.upsert_observaciones(sid, limpio["tienda_id"], limpio["fecha"], limpio["ventas"])
        except Exception as e:
            log.exception("error al guardar observaciones")
            return _reject(f"no se pudo guardar la observación: {type(e).__name__}", 503)
        return ObservacionResponse(registros_guardados=n, calidad=_calidad(notas))

    @app.post("/admin/reentrenar", status_code=202, response_model=ReentrenarResponse)
    def reentrenar():
        rid = f"reentreno_{uuid.uuid4().hex[:12]}"
        if airflow is None:
            return _reject("Airflow no está configurado", 503)
        try:
            repo.create_reentreno(rid)
        except Exception as e:
            return _reject(f"no se pudo registrar la ejecución: {type(e).__name__}", 503)
        try:
            airflow.trigger(rid)
        except Exception as e:
            repo.delete_reentreno(rid)
            return _reject(f"no se pudo iniciar el reentrenamiento en Airflow: {type(e).__name__}", 503)
        return ReentrenarResponse(id=rid)

    @app.get("/admin/reentrenar/{rid}", response_model=EstadoReentreno, response_model_exclude_none=True)
    def estado_reentreno(rid: str):
        row = repo.get_reentreno(rid)
        if row is None:
            return _reject(f"no existe la ejecución '{rid}'", 404)
        if row["estado"] in ("exitoso", "fallido"):
            if row.get("version") is None and models.current is not None:
                row = {**row, "version": models.current.version}  # vigente al terminar
            if row.get("promovido") is None:
                row = {**row, "promovido": False}
            return EstadoReentreno(**row)
        st = None
        if airflow is not None:
            try:
                st = airflow.state(rid)
            except Exception:
                st = None
        if st == "fallido":  # la tarea murió antes de escribir su resultado
            v = models.current.version if models.current else None
            return EstadoReentreno(estado="fallido", promovido=False, version=v,
                                   motivo="La ejecución en Airflow falló; el modelo vigente no cambió")
        return EstadoReentreno(estado="en_curso")

    @app.get("/modelo", response_model=ModeloResponse)
    def modelo():
        if models.current is None:
            return _reject("no hay un modelo cargado todavía", 503)
        return ModeloResponse(nombre=models.name, alias=models.alias, version=models.current.version)

    @app.get("/health", response_model=HealthResponse, response_model_exclude_none=True)
    def salud():
        dep = dict(health.estado)
        if models.current is None:
            return JSONResponse(status_code=503, content={"estado": "degradado", "motivo": "no hay modelo cargado",
                                                          "dependencias": dep})
        malos = [f"{k}: {v}" for k, v in dep.items() if v != "ok"]
        if len(dep) < len(health.checks):
            malos.append("verificación de dependencias en curso")
        if malos:
            return HealthResponse(estado="degradado", motivo="; ".join(malos), dependencias=dep,
                                  version_modelo=models.current.version)
        return HealthResponse(estado="ok", dependencias=dep, version_modelo=models.current.version)

    return app


def app_from_env() -> FastAPI:
    """Ensambla la app real (Postgres, MLflow, Airflow) a partir de variables de entorno."""
    from . import registry
    from .airflow_client import AirflowClient
    from .db import PostgresRepo
    from .service import http_check

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    repo = PostgresRepo(os.environ["PRODUCT_DB_DSN"])
    models = ModelManager(registry.vigente_version, registry.load_version, os.getenv("MODEL_CACHE_DIR", "/cache"),
                          registry.MODEL_NAME, registry.ALIAS)
    airflow = AirflowClient(os.getenv("AIRFLOW_URL", "http://airflow-apiserver:8080"),
                            os.getenv("AIRFLOW_USER"), os.getenv("AIRFLOW_PASSWORD"),
                            os.getenv("RETRAIN_DAG_ID", "reentrenamiento"))
    mlflow_url = os.getenv("MLFLOW_TRACKING_URI", "http://mlflow:5000").rstrip("/")
    health = Health({
        "base_de_datos": repo.ping,
        "mlflow": http_check(f"{mlflow_url}/health"),
        "airflow": airflow.health,
        "modelo": lambda: None if models.last_error is None else (_ for _ in ()).throw(RuntimeError(models.last_error)),
    })
    return create_app(repo, models, health, airflow,
                      model_poll_s=float(os.getenv("MODEL_POLL_SECONDS", "20")),
                      health_poll_s=float(os.getenv("HEALTH_POLL_SECONDS", "10")))
