"""Reentrenamiento (lo invoca el DAG de Airflow) y arranque desde cero (servicio `bootstrap`).

Garantía: el alias del modelo vigente solo se mueve al final, si el candidato supera el criterio.
Cualquier fallo antes de ese punto deja intacto al vigente.
"""
from __future__ import annotations

import logging
import os
import time

import mlflow
import pandas as pd

from . import registry
from .catalogs import Catalogs, catalogs_from_tables, read_published
from .db import PostgresRepo
from .features import sales_panel
from .model import VAL_DIAS, decide, evaluate_window, train_all

log = logging.getLogger("novamercado.pipeline")
MIN_DIAS_NUEVOS = int(os.getenv("MIN_DIAS_NUEVOS", "7"))


def repo_from_env() -> PostgresRepo:
    return PostgresRepo(os.environ["PRODUCT_DB_DSN"])


def retrain(run_id: str, repo=None) -> dict:
    repo = repo or repo_from_env()
    t0 = time.time()
    try:
        cat_d = repo.load_catalogs()
        if cat_d is None:
            raise RuntimeError("no hay catálogos cargados en la base de datos (¿corrió bootstrap?)")
        cat = Catalogs.from_dict(cat_d)
        hist, obs, promos = repo.training_data()
        panel = sales_panel(hist, obs, promos)

        # Ventana de comparación: solo días que el vigente NO vio al entrenarse (comparación justa).
        vig = registry.vigente_version()
        vig_model = registry.load_version(vig) if vig is not None else None
        val_dias = VAL_DIAS
        if vig_model is not None:
            fechas = pd.Series(pd.to_datetime(panel.dropna(subset=["ventas"])["fecha"].unique()))
            nuevos = int((fechas > pd.Timestamp(vig_model.entrenado_hasta)).sum())
            if nuevos < MIN_DIAS_NUEVOS:
                motivo = (f"Solo {nuevos} días con ventas nuevas desde que se entrenó el vigente "
                          f"(mínimo {MIN_DIAS_NUEVOS}); se conserva el vigente")
                repo.finish_reentreno(run_id, "exitoso", False, vig, motivo, {"dias_nuevos": nuevos})
                return {"estado": "exitoso", "promovido": False, "version": vig, "motivo": motivo}
            val_dias = min(VAL_DIAS, nuevos)
        ent = train_all(panel, cat, val_dias=val_dias)
        cand = ent.modelos[ent.mejor]
        costo_cand = ent.metricas[ent.mejor]["costo"]
        costo_vig = None
        if vig_model is not None:
            costo_vig = evaluate_window(vig_model, panel, cat, ent.val_inicio, ent.val_fin)["costo"]
        promovido, motivo = decide(costo_cand, costo_vig)

        mlflow.set_experiment(registry.EXPERIMENT)
        with mlflow.start_run(run_name=f"reentreno-{run_id}") as run:
            mlflow.set_tags({"reentreno_id": run_id, "modelo_elegido": ent.mejor,
                             "vigente_previo": vig or "ninguno", "promovido": promovido})
            mlflow.log_params({"val_inicio": ent.val_inicio, "val_fin": ent.val_fin, "corte": ent.corte,
                               "filas_historico": len(hist), "filas_observaciones": len(obs),
                               "fecha_max_datos": str(panel["fecha"].max().date()),
                               "criterio": "costo_validacion_candidato < costo_validacion_vigente"})
            for kind, m in ent.metricas.items():
                with mlflow.start_run(run_name=kind, nested=True):
                    mlflow.set_tag("tipo_modelo", kind)
                    mlflow.log_metrics({k: v for k, v in m.items()})
                mlflow.log_metrics({f"{kind}_{k}": v for k, v in m.items()})
            if costo_vig is not None:
                mlflow.log_metric("vigente_costo", costo_vig)
            version = registry.log_model(cand)
            registry.tag_version(version, {"reentreno_id": run_id, "tipo_modelo": ent.mejor,
                                           "costo_validacion": round(costo_cand, 1), "promovido": promovido,
                                           "motivo": motivo})
            if promovido:
                registry.set_vigente(version)  # único punto donde cambia el modelo vigente
            mlflow.log_metric("duracion_s", time.time() - t0)
            mlflow_run = run.info.run_id
        final = version if promovido else vig
        res = {"estado": "exitoso", "promovido": promovido, "version": final, "motivo": motivo,
               "metricas": {"candidato": ent.metricas, "costo_vigente": costo_vig, "version_candidata": version,
                            "mlflow_run_id": mlflow_run}}
        repo.finish_reentreno(run_id, "exitoso", promovido, final, motivo, res["metricas"])
        log.info("reentreno %s: %s", run_id, motivo)
        return res
    except Exception as e:  # el vigente no se tocó
        try:
            vig = registry.vigente_version()
        except Exception:
            vig = None
        repo.finish_reentreno(run_id, "fallido", False, vig, f"Falló el reentrenamiento: {e}")
        raise


def bootstrap(datos_dir: str, repo=None, wait_s: int = 300) -> None:
    """Primera vez: carga los datos publicados y entrena/registra el primer modelo.
    Si ya existe (volúmenes conservados), no repite nada."""
    repo = repo or repo_from_env()
    _wait(repo.ping, "base de datos", wait_s)
    repo.init_schema()
    if repo.historico_count() == 0:
        t = read_published(datos_dir)
        cat = catalogs_from_tables(t)
        repo.load_historico(t["historico_ventas"])
        repo.save_catalogs(cat.to_dict())
        log.info("datos publicados cargados: %d filas de histórico", len(t["historico_ventas"]))
    else:
        log.info("histórico ya cargado; se omite la carga")
    _wait(lambda: registry.client().search_registered_models(max_results=1), "MLflow", wait_s)
    if registry.vigente_version() is not None:
        log.info("ya existe modelo vigente (%s); no se reentrena", registry.vigente_version())
        return
    repo.create_reentreno("bootstrap")
    retrain("bootstrap", repo)


def _wait(fn, what: str, seconds: int) -> None:
    fin = time.time() + seconds
    while True:
        try:
            fn()
            return
        except Exception as e:
            if time.time() > fin:
                raise RuntimeError(f"{what} no disponible: {e}")
            log.info("esperando %s...", what)
            time.sleep(3)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bootstrap(os.getenv("DATOS_DIR", "/datos"))
