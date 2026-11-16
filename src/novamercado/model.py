"""Modelos de pronóstico, cantidad sugerida (newsvendor) y métrica de negocio.

- Baseline: media de los últimos 4 mismos días de la semana disponibles (naive estacional).
- ML: LightGBM sobre la razón ventas / nivel reciente (ver features.py).
- Cantidad sugerida: venta_esperada · k_familia, con k_familia = cuantil q = c_f/(c_f+c_s) de la razón
  real/esperada en validación. Es la solución del problema del vendedor de periódicos (newsvendor):
  minimiza el costo esperado c_f·faltante + c_s·sobrante.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .catalogs import Catalogs
from .features import FEATURES, build_features

LGB_PARAMS = dict(objective="regression", learning_rate=0.05, num_leaves=31, min_data_in_leaf=40,
                  feature_fraction=0.9, bagging_fraction=0.9, bagging_freq=1, lambda_l2=1.0,
                  verbose=-1, seed=42, num_threads=2)
LGB_ROUNDS = 400
VAL_DIAS = 28


def costo(real, sugerido, familias, cat: Catalogs) -> np.ndarray:
    c = cat.costos.set_index("familia_id")
    cf = pd.Series(familias).map(c["costo_faltante"]).to_numpy(float)
    cs = pd.Series(familias).map(c["costo_sobrante"]).to_numpy(float)
    real = np.asarray(real, float)
    sug = np.asarray(sugerido, float)
    return cf * np.maximum(real - sug, 0) + cs * np.maximum(sug - real, 0)


def metricas(real, esperado, sugerido, familias, cat: Catalogs) -> dict:
    real = np.asarray(real, float)
    esp = np.asarray(esperado, float)
    err = esp - real
    return {
        "mae": float(np.mean(np.abs(err))),
        "rmse": float(np.sqrt(np.mean(err ** 2))),
        "wape": float(np.sum(np.abs(err)) / max(np.sum(real), 1.0)),
        "sesgo": float(np.sum(err) / max(np.sum(real), 1.0)),
        "costo": float(np.sum(costo(real, sugerido, familias, cat))),
        "n": int(len(real)),
    }


@dataclass
class ForecastModel:
    kind: str  # "baseline" | "lgbm"
    catalogs: dict
    multiplicadores: dict[str, float] = field(default_factory=dict)
    nivel_respaldo: dict[str, float] = field(default_factory=dict)
    booster_txt: str | None = None
    entrenado_hasta: str | None = None
    _booster: object = field(default=None, repr=False, compare=False)

    @property
    def cat(self) -> Catalogs:
        return Catalogs.from_dict(self.catalogs)

    def booster(self):
        if self._booster is None and self.booster_txt:
            import lightgbm as lgb
            self._booster = lgb.Booster(model_str=self.booster_txt)
        return self._booster

    def __getstate__(self):
        d = self.__dict__.copy()
        d["_booster"] = None
        return d

    # -- pronóstico puntual a partir de features ya construidas
    def expected(self, feats: pd.DataFrame) -> np.ndarray:
        nivel = feats["nivel"].copy()
        faltante = nivel.isna()
        if faltante.any():
            nivel[faltante] = feats.loc[faltante, "familia_id"].map(self.nivel_respaldo).fillna(0.0)
        if self.kind == "baseline":
            pred = feats["dow4"].fillna(feats["m7"]).fillna(nivel)
            return np.maximum(pred.to_numpy(float), 0.0)
        X = feats[FEATURES].copy()
        X["log_nivel"] = np.log1p(nivel)
        ratio = self.booster().predict(X.to_numpy(float))
        return np.maximum(ratio * (nivel.to_numpy(float) + 1.0), 0.0)

    def suggested(self, esperado: np.ndarray, familias) -> np.ndarray:
        k = pd.Series(familias).map(self.multiplicadores).fillna(1.0).to_numpy(float)
        return np.maximum(np.ceil(np.asarray(esperado, float) * k - 1e-9), 0).astype(int)

    def predict_frame(self, feats: pd.DataFrame) -> pd.DataFrame:
        esp = self.expected(feats)
        sug = self.suggested(esp, feats["familia_id"].to_numpy())
        return pd.DataFrame({"familia_id": feats["familia_id"].to_numpy(),
                             "venta_esperada": np.round(esp, 1), "cantidad_sugerida": sug},
                            index=feats.index)


def fit_multiplicadores(real, esperado, familias, cat: Catalogs) -> dict[str, float]:
    """k_f = cuantil crítico de real/esperado por familia (newsvendor empírico)."""
    df = pd.DataFrame({"real": np.asarray(real, float), "esp": np.asarray(esperado, float), "f": familias})
    df = df[df["esp"] > 0]
    out = {}
    for f in cat.familia_ids:
        r = (df.loc[df["f"] == f, "real"] / df.loc[df["f"] == f, "esp"]).to_numpy()
        out[f] = float(np.quantile(r, cat.cuantil(f))) if len(r) >= 10 else 1.0
    return out


def _train_lgbm(tr: pd.DataFrame, rounds: int = LGB_ROUNDS):
    import lightgbm as lgb
    base = tr["nivel"].to_numpy(float) + 1.0
    y = tr["ventas"].to_numpy(float) / base
    ds = lgb.Dataset(tr[FEATURES].to_numpy(float), y, weight=base, feature_name=FEATURES,
                     categorical_feature=["tienda_id", "familia_cod", "tipo_cod"], free_raw_data=False)
    return lgb.train(LGB_PARAMS, ds, num_boost_round=rounds)


@dataclass
class Entrenamiento:
    modelos: dict[str, ForecastModel]  # modelos finales (reentrenados con todo)
    metricas: dict[str, dict]  # métricas en validación de cada tipo
    mejor: str
    corte: str
    val_inicio: str
    val_fin: str


def train_all(panel: pd.DataFrame, cat: Catalogs, val_dias: int = VAL_DIAS) -> Entrenamiento:
    """Entrena baseline y LightGBM, los evalúa en los últimos `val_dias` y elige el de menor costo."""
    panel = panel.copy()
    panel["fecha"] = pd.to_datetime(panel["fecha"])
    obs = panel.dropna(subset=["ventas"])
    feats = build_features(panel, obs[["fecha", "tienda_id", "familia_id", "en_promocion", "ventas"]], cat)
    feats = feats[feats["nivel"].notna() & (feats["dias_desde_ultimo"] <= 28)]
    fin = feats["fecha"].max()
    corte = fin - pd.Timedelta(days=val_dias)
    tr, va = feats[feats["fecha"] <= corte], feats[feats["fecha"] > corte]
    respaldo = obs.groupby("familia_id")["ventas"].median().to_dict()
    cd = cat.to_dict()

    metr: dict[str, dict] = {}
    finales: dict[str, ForecastModel] = {}

    # baseline
    b = ForecastModel("baseline", cd, nivel_respaldo=respaldo, entrenado_hasta=str(fin.date()))
    esp = b.expected(va)
    b.multiplicadores = fit_multiplicadores(va["ventas"], esp, va["familia_id"].to_numpy(), cat)
    metr["baseline"] = metricas(va["ventas"], esp, b.suggested(esp, va["familia_id"]), va["familia_id"], cat)
    finales["baseline"] = b

    # LightGBM: evaluado con lo entrenado hasta el corte; el modelo final se reentrena con todo
    m = ForecastModel("lgbm", cd, nivel_respaldo=respaldo, entrenado_hasta=str(corte.date()))
    m._booster = _train_lgbm(tr)
    esp = m.expected(va)
    mult = fit_multiplicadores(va["ventas"], esp, va["familia_id"].to_numpy(), cat)
    m.multiplicadores = mult
    metr["lgbm"] = metricas(va["ventas"], esp, m.suggested(esp, va["familia_id"]), va["familia_id"], cat)
    full = _train_lgbm(feats)
    finales["lgbm"] = ForecastModel("lgbm", cd, multiplicadores=mult, nivel_respaldo=respaldo,
                                    booster_txt=full.model_to_string(), entrenado_hasta=str(fin.date()))

    mejor = min(metr, key=lambda k: metr[k]["costo"])
    return Entrenamiento(finales, metr, mejor, str(corte.date()), str(va["fecha"].min().date()),
                         str(fin.date()))


def evaluate_window(model: ForecastModel, panel: pd.DataFrame, cat: Catalogs, desde: str, hasta: str) -> dict:
    """Costo de un modelo en una ventana, construyendo features solo con el pasado de cada día."""
    panel = panel.copy()
    panel["fecha"] = pd.to_datetime(panel["fecha"])
    obs = panel.dropna(subset=["ventas"])
    tg = obs[(obs["fecha"] >= desde) & (obs["fecha"] <= hasta)]
    feats = build_features(panel, tg[["fecha", "tienda_id", "familia_id", "en_promocion", "ventas"]], cat)
    feats["en_promocion"] = feats["en_promocion"]
    out = model.predict_frame(feats)
    return metricas(feats["ventas"], out["venta_esperada"], out["cantidad_sugerida"], feats["familia_id"], cat)


def decide(costo_candidato: float, costo_vigente: float | None) -> tuple[bool, str]:
    """Criterio de reemplazo: el candidato debe tener menor costo de negocio que el vigente
    en la misma ventana de validación."""
    fmt = lambda x: f"{x:,.0f}".replace(",", " ")
    if costo_vigente is None or not math.isfinite(costo_vigente):
        return True, f"No hay modelo vigente evaluable; costo en validación del candidato {fmt(costo_candidato)}"
    if costo_candidato < costo_vigente:
        return True, f"Costo en validación {fmt(costo_candidato)} vs {fmt(costo_vigente)} del vigente"
    return False, (f"Costo en validación {fmt(costo_candidato)} no mejora {fmt(costo_vigente)} del vigente; "
                   "se conserva el vigente")
