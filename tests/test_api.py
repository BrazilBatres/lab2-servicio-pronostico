"""Pruebas del contrato HTTP (Anexo) sin MLflow ni Postgres: repositorio en memoria y modelo local."""
import pytest
from fastapi.testclient import TestClient

from novamercado.api import create_app
from novamercado.db import MemoryRepo
from novamercado.features import sales_panel
from novamercado.model import train_all
from novamercado.service import Health, ModelManager
from conftest import valid_obs, valid_pred


@pytest.fixture(scope="module")
def trained(tables, cat):
    return train_all(sales_panel(tables["historico_ventas"], None), cat, val_dias=21)


class FakeRegistry:
    def __init__(self, modelos):
        self.modelos = modelos  # version -> ForecastModel
        self.vigente = "1"
        self.down = False

    def resolve(self):
        if self.down:
            raise ConnectionError("mlflow caído")
        return self.vigente

    def load(self, v):
        return self.modelos[v]


class FakeAirflow:
    def __init__(self):
        self.runs = {}
        self.fail = False

    def trigger(self, rid):
        if self.fail:
            raise ConnectionError("airflow caído")
        self.runs[rid] = "en_curso"

    def state(self, rid):
        return self.runs.get(rid)


@pytest.fixture
def env(tables, cat, trained):
    repo = MemoryRepo(tables["historico_ventas"], cat.to_dict())
    reg = FakeRegistry({"1": trained.modelos["baseline"], "2": trained.modelos["lgbm"]})
    models = ModelManager(reg.resolve, reg.load)
    models.refresh()
    health = Health({"base_de_datos": repo.ping, "mlflow": lambda: reg.resolve()})
    health.run_once()
    af = FakeAirflow()
    app = create_app(repo, models, health, af, background=False)
    return TestClient(app), repo, reg, models, health, af


def test_prediccion_valida_cumple_contrato(env):
    c, repo, *_ = env
    r = c.post("/v1/prediccion", json=valid_pred())
    assert r.status_code == 200
    b = r.json()
    assert set(b) >= {"tienda_id", "fecha", "version_modelo", "predicciones", "calidad"}
    assert b["tienda_id"] == 1 and b["fecha"] == "2024-05-20" and b["version_modelo"] == "1"
    assert isinstance(b["version_modelo"], str)
    assert b["calidad"] == {"estado": "ok", "observaciones": []}
    assert len(b["predicciones"]) == 12
    for p in b["predicciones"]:
        assert set(p) >= {"familia_id", "venta_esperada", "cantidad_sugerida"}
        assert isinstance(p["cantidad_sugerida"], int) and p["cantidad_sugerida"] >= 0
        assert isinstance(p["venta_esperada"], float)
    assert len(repo.predicciones) == 12 and repo.predicciones[0]["version_modelo"] == "1"


def test_prediccion_reparada_reporta_corregido(env):
    c, repo, *_ = env
    raw = valid_pred()
    raw["tienda_id"] = "T01"
    raw["familias"][2]["familia_id"] = "LÁCTEOS"
    b = c.post("/v1/prediccion", json=raw).json()
    assert b["calidad"]["estado"] == "corregido"
    obs = b["calidad"]["observaciones"]
    assert {o["accion"] for o in obs} == {"reparar"}
    assert any(o["campo"] == "familias[2].familia_id" for o in obs)
    assert len(repo.correcciones) == 2


def test_advertencia_si_hay_alguna_accion_advertir(env):
    c, *_ = env
    raw = valid_pred()
    raw["tienda_id"] = "1"
    raw["familias"] = raw["familias"][:10]
    b = c.post("/v1/prediccion", json=raw).json()
    assert b["calidad"]["estado"] == "advertencia"
    assert len(b["predicciones"]) == 10


@pytest.mark.parametrize("raw", [{"tienda_id": 77, "fecha": "2024-05-20", "familias": [{"familia_id": "abarrotes",
                                                                                         "en_promocion": 0}]},
                                 {"fecha": "2024-05-20"}, "no-json"])
def test_rechazo_422_con_detail(env, raw):
    c, repo, *_ = env
    r = c.post("/v1/prediccion", json=raw) if raw != "no-json" else c.post("/v1/prediccion", content=b"{malo")
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str) and r.json()["detail"]
    assert repo.solicitudes[-1]["http_status"] == 422


def test_prediccion_repetida_de_la_misma_fecha(env):
    c, *_ = env
    a = c.post("/v1/prediccion", json=valid_pred()).json()
    b = c.post("/v1/prediccion", json=valid_pred()).json()
    assert a["predicciones"] == b["predicciones"]


def test_observaciones_idempotentes(env):
    c, repo, *_ = env
    r = c.post("/v1/observaciones", json=valid_obs())
    assert r.status_code == 200
    assert r.json() == {"registros_guardados": 12, "calidad": {"estado": "ok", "observaciones": []}}
    r2 = c.post("/v1/observaciones", json=valid_obs())
    assert r2.json()["registros_guardados"] == 0 and r2.json()["calidad"]["estado"] == "ok"
    cambio = valid_obs()
    cambio["ventas"][0]["ventas"] = 5
    r3 = c.post("/v1/observaciones", json=cambio).json()
    assert r3["registros_guardados"] == 1 and r3["calidad"]["estado"] == "advertencia"
    assert len(repo.obs) == 12


def test_observacion_con_formatos(env):
    c, *_ = env
    raw = valid_obs(fecha="21/05/2024")
    raw["ventas"][0]["ventas"] = "1,234"
    raw["ventas"][1]["ventas"] = -3
    b = c.post("/v1/observaciones", json=raw).json()
    assert b["registros_guardados"] == 11 and b["calidad"]["estado"] == "advertencia"


def test_pronostico_usa_la_observacion_del_dia_anterior(env):
    c, *_ = env
    antes = c.post("/v1/prediccion", json=valid_pred(fecha="2024-05-31")).json()
    alto = valid_obs(fecha="2024-05-24", valor=5000)  # mismo día de la semana (baseline)
    c.post("/v1/observaciones", json=alto)
    despues = c.post("/v1/prediccion", json=valid_pred(fecha="2024-05-31")).json()
    assert despues["predicciones"][0]["venta_esperada"] > antes["predicciones"][0]["venta_esperada"]
    # una observación del mismo día o posterior no se usa
    c.post("/v1/observaciones", json=valid_obs(fecha="2024-05-31", valor=9000))
    igual = c.post("/v1/prediccion", json=valid_pred(fecha="2024-05-31")).json()
    assert igual["predicciones"] == despues["predicciones"]


def test_modelo_y_cambio_en_caliente(env):
    c, _, reg, models, *_ = env
    assert c.get("/modelo").json() == {"nombre": "novamercado_ventas", "alias": "vigente", "version": "1"}
    reg.vigente = "2"
    models.refresh()  # lo hace un hilo cada MODEL_POLL_SECONDS en producción
    assert c.get("/modelo").json()["version"] == "2"
    assert c.post("/v1/prediccion", json=valid_pred()).json()["version_modelo"] == "2"


def test_mlflow_caido_sigue_respondiendo_y_health_degradado(env):
    c, _, reg, models, health, _ = env
    assert c.get("/health").json()["estado"] == "ok"
    reg.down = True
    models.refresh()
    health.run_once()
    h = c.get("/health")
    assert h.status_code == 200 and h.json()["estado"] == "degradado" and "mlflow" in h.json()["motivo"]
    assert c.post("/v1/prediccion", json=valid_pred()).status_code == 200
    reg.down = False
    health.run_once()
    assert c.get("/health").json()["estado"] == "ok"


def test_health_503_sin_modelo(tables, cat):
    repo = MemoryRepo(tables["historico_ventas"], cat.to_dict())
    models = ModelManager(lambda: None, lambda v: None)
    c = TestClient(create_app(repo, models, Health({}), None, background=False))
    assert c.get("/health").status_code == 503
    assert c.post("/v1/prediccion", json=valid_pred()).status_code == 503


def test_reentrenar_flujo(env):
    c, repo, _, _, _, af = env
    r = c.post("/admin/reentrenar")
    assert r.status_code == 202 and isinstance(r.json()["id"], str)
    rid = r.json()["id"]
    assert c.get(f"/admin/reentrenar/{rid}").json() == {"estado": "en_curso"}
    repo.finish_reentreno(rid, "exitoso", True, "2", "Costo en validación 1 vs 2 del vigente")
    assert c.get(f"/admin/reentrenar/{rid}").json() == {"estado": "exitoso", "promovido": True, "version": "2",
                                                        "motivo": "Costo en validación 1 vs 2 del vigente"}
    assert c.get("/admin/reentrenar/no-existe").status_code == 404


def test_reentrenar_fallido_en_airflow_y_503(env):
    c, repo, _, _, _, af = env
    rid = c.post("/admin/reentrenar").json()["id"]
    af.runs[rid] = "fallido"
    b = c.get(f"/admin/reentrenar/{rid}").json()
    assert b["estado"] == "fallido" and b["promovido"] is False and b["version"] == "1"
    af.fail = True
    assert c.post("/admin/reentrenar").status_code == 503
