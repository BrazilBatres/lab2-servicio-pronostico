"""Lógica del servicio, independiente de FastAPI: modelo vigente en memoria, salud de dependencias,
pronóstico y registro de observaciones."""
from __future__ import annotations

import logging
import os
import pickle
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx
import pandas as pd

from .catalogs import Catalogs
from .cleaning import ADVERTIR, Nota
from .features import build_features
from .model import ForecastModel

log = logging.getLogger("novamercado.service")


# --------------------------------------------------------------------------- modelo vigente
@dataclass
class Loaded:
    model: ForecastModel
    version: str
    cat: Catalogs


class ModelManager:
    """Mantiene el modelo vigente. Consulta el alias periódicamente y cambia de versión en caliente.
    Guarda una copia local para seguir sirviendo si MLflow no está disponible (incluso al reiniciar)."""

    def __init__(self, resolve_version: Callable[[], str | None], load: Callable[[str], ForecastModel],
                 cache_dir: str | None = None, name: str = "novamercado_ventas", alias: str = "vigente"):
        self.resolve_version = resolve_version
        self.load = load
        self.cache = Path(cache_dir) if cache_dir else None
        self.name, self.alias = name, alias
        self.current: Loaded | None = None
        self.lock = threading.Lock()
        self.last_error: str | None = None

    def load_cache(self) -> bool:
        if not self.cache or not (self.cache / "modelo.pkl").exists():
            return False
        try:
            with open(self.cache / "modelo.pkl", "rb") as fh:
                version, model = pickle.load(fh)
            self._set(model, version)
            log.info("modelo %s cargado desde la copia local", version)
            return True
        except Exception as e:
            log.warning("no se pudo leer la copia local: %s", e)
            return False

    def _set(self, model: ForecastModel, version: str) -> None:
        with self.lock:
            self.current = Loaded(model, str(version), model.cat)

    def refresh(self) -> bool:
        """Devuelve True si se pudo consultar MLflow. Cambia de modelo si el alias apunta a otra versión."""
        try:
            v = self.resolve_version()
        except Exception as e:
            self.last_error = f"MLflow no disponible: {type(e).__name__}"
            return False
        self.last_error = None
        if v is None or (self.current and self.current.version == str(v)):
            return True
        try:
            model = self.load(str(v))
        except Exception as e:
            self.last_error = f"no se pudo cargar la versión {v}: {type(e).__name__}"
            return False
        self._set(model, str(v))
        log.info("modelo vigente ahora es la versión %s", v)
        if self.cache:
            try:
                self.cache.mkdir(parents=True, exist_ok=True)
                tmp = self.cache / "modelo.pkl.tmp"
                with open(tmp, "wb") as fh:
                    pickle.dump((str(v), model), fh)
                os.replace(tmp, self.cache / "modelo.pkl")
            except Exception as e:
                log.warning("no se pudo guardar la copia local: %s", e)
        return True


# --------------------------------------------------------------------------- salud
@dataclass
class Health:
    checks: dict[str, Callable[[], None]]
    estado: dict[str, str] = field(default_factory=dict)

    def run_once(self) -> None:
        for name, fn in self.checks.items():
            try:
                fn()
                self.estado[name] = "ok"
            except Exception as e:
                self.estado[name] = f"no disponible ({type(e).__name__})"


def http_check(url: str, timeout: float = 3.0) -> Callable[[], None]:
    def _f():
        r = httpx.get(url, timeout=timeout)
        r.raise_for_status()
    return _f


class Background:
    def __init__(self):
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []

    def every(self, seconds: float, fn: Callable[[], object]) -> None:
        def loop():
            while not self.stop.is_set():
                try:
                    fn()
                except Exception as e:
                    log.warning("tarea periódica falló: %s", e)
                self.stop.wait(seconds)
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        self.threads.append(t)


# --------------------------------------------------------------------------- operaciones
def forecast(loaded: Loaded, repo, limpio: dict) -> tuple[list[dict], list[Nota]]:
    """Pronóstico de una tienda y fecha. Solo usa ventas de días anteriores a la fecha."""
    notas: list[Nota] = []
    tid, fecha = limpio["tienda_id"], limpio["fecha"]
    hist = repo.history_for(tid, fecha)
    tg = pd.DataFrame([{"fecha": fecha, "tienda_id": tid, **f} for f in limpio["familias"]])
    feats = build_features(hist, tg, loaded.cat)
    sin = feats.loc[feats["nivel"].isna(), "familia_id"].tolist()
    if sin:
        notas.append(Nota("familias", ADVERTIR, f"sin ventas previas para {', '.join(sin)}; "
                                                 "se usó el nivel típico de la familia"))
    dias = feats["dias_desde_ultimo"].dropna()
    if len(dias) and dias.max() > 7:
        notas.append(Nota("fecha", ADVERTIR, f"la última venta conocida es de hace {int(dias.max())} días; "
                                              "el pronóstico usa esa información"))
    out = loaded.model.predict_frame(feats)
    rows = []
    for (_, r), promo in zip(out.iterrows(), feats["en_promocion"]):
        rows.append({"familia_id": r.familia_id, "venta_esperada": float(r.venta_esperada),
                     "cantidad_sugerida": int(r.cantidad_sugerida), "en_promocion": int(promo)})
    return rows, notas


def replaced_notes(repo, limpio: dict) -> list[Nota]:
    """Avisa cuando una observación ya registrada llega con otro valor (se reemplaza: corrección)."""
    notas = []
    previas = repo.existing_observaciones(limpio["tienda_id"], limpio["fecha"])
    for i, r in enumerate(limpio["ventas"]):
        p = previas.get(r["familia_id"])
        if p is not None and p != r["ventas"]:
            notas.append(Nota(f"ventas[{r['familia_id']}]", ADVERTIR,
                              f"ya existía la observación ({p}); se reemplazó por {r['ventas']}"))
    return notas
