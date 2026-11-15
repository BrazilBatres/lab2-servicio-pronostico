"""Integración con MLflow: registro de experimentos y modelos, alias del modelo vigente."""
from __future__ import annotations

import os

import mlflow
import mlflow.pyfunc
import pandas as pd
from mlflow.exceptions import MlflowException
from mlflow.tracking import MlflowClient

from .model import ForecastModel

MODEL_NAME = os.getenv("MODEL_NAME", "novamercado_ventas")
ALIAS = os.getenv("MODEL_ALIAS", "vigente")
EXPERIMENT = os.getenv("MLFLOW_EXPERIMENT", "novamercado_reentrenamiento")


class PyfuncWrapper(mlflow.pyfunc.PythonModel):
    """Envuelve ForecastModel para registrarlo como modelo pyfunc (recibe features, devuelve pronóstico)."""

    def __init__(self, model: ForecastModel):
        self.model = model

    def predict(self, context, model_input: pd.DataFrame, params=None):
        return self.model.predict_frame(model_input)


def client() -> MlflowClient:
    return MlflowClient()


def ping(timeout: float = 5.0) -> None:
    import urllib.request
    uri = mlflow.get_tracking_uri()
    if uri.startswith("http"):
        urllib.request.urlopen(uri.rstrip("/") + "/health", timeout=timeout).read()


def vigente_version() -> str | None:
    try:
        return str(client().get_model_version_by_alias(MODEL_NAME, ALIAS).version)
    except MlflowException as e:
        if "RESOURCE_DOES_NOT_EXIST" in str(e.error_code) or "not found" in str(e).lower():
            return None
        raise


def load_version(version: str) -> ForecastModel:
    pm = mlflow.pyfunc.load_model(f"models:/{MODEL_NAME}/{version}")
    return pm.unwrap_python_model().model


def log_model(model: ForecastModel, input_example: pd.DataFrame | None = None) -> str:
    """Registra el modelo en el run activo y devuelve la versión creada en el Model Registry."""
    info = mlflow.pyfunc.log_model(name="modelo", python_model=PyfuncWrapper(model),
                                   pip_requirements=["lightgbm==4.7.0", "pandas==3.1.1", "numpy==2.5.4"],
                                   registered_model_name=MODEL_NAME)
    return str(info.registered_model_version)


def set_vigente(version: str) -> None:
    client().set_registered_model_alias(MODEL_NAME, ALIAS, version)


def tag_version(version: str, tags: dict) -> None:
    c = client()
    for k, v in tags.items():
        c.set_model_version_tag(MODEL_NAME, version, k, str(v))
