"""Features del pronóstico. Regla de oro: para la fecha F solo se usan ventas de fechas < F.

Todas las features de ventas son relativas al *nivel* reciente de la serie (media de las últimas
28 observaciones disponibles), y el objetivo del modelo es la razón ventas / nivel. Así el modelo
se adapta a cambios de nivel en la demanda (el periodo de evaluación puede tenerlos) sin necesitar
reentrenarse, y tolera huecos: si falta un día, las ventanas usan lo que sí llegó.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .catalogs import Catalogs

FERIADO_TIPOS = ("feriado", "comercial", "pago", "medio_dia")
TIPOS_TIENDA = ("hipermercado", "supermercado", "express")

FEATURES = [
    "tienda_id", "familia_cod", "tipo_cod",
    "dow", "dom", "mes", "fin_mes", "quincena",
    *[f"fer_{t}" for t in FERIADO_TIPOS], "fer_manana", "fer_ayer",
    "log_nivel", "r_lag1", "r_lag7", "r_m7", "r_dow4", "dias_desde_ultimo",
    "promo", "promo_ratio",
]


def _feriado_flags(fechas: pd.Series, ciudades: pd.Series, cat: Catalogs) -> pd.DataFrame:
    fer = cat.feriados.copy()
    fer["fecha"] = pd.to_datetime(fer["fecha"])
    out = pd.DataFrame(index=fechas.index)
    keys = pd.DataFrame({"fecha": fechas.values, "ciudad": ciudades.values}, index=fechas.index)

    def flag(shift_days: int, tipo: str | None) -> np.ndarray:
        f = fer if tipo is None else fer[fer["tipo"] == tipo]
        nac = set(f.loc[f["ambito"] == "nacional", "fecha"])
        loc = set(zip(f.loc[f["ambito"] != "nacional", "fecha"], f.loc[f["ambito"] != "nacional", "ambito"]))
        d = keys["fecha"] + pd.Timedelta(days=shift_days)
        return np.array([(x in nac) or ((x, c) in loc) for x, c in zip(d, keys["ciudad"])], dtype=int)

    for t in FERIADO_TIPOS:
        out[f"fer_{t}"] = flag(0, t)
    out["fer_manana"] = flag(1, "feriado")
    out["fer_ayer"] = flag(-1, "feriado")
    return out


def _series_stats(s: pd.Series) -> pd.DataFrame:
    """s: ventas diarias indexadas por fecha (calendario completo, NaN = sin dato)."""
    valid = s.dropna()
    nivel = valid.rolling(28, min_periods=1).mean().reindex(s.index).ffill().shift(1)
    ultimo = pd.Series(np.where(s.notna(), np.arange(len(s)), np.nan), index=s.index).ffill().shift(1)
    dias = np.arange(len(s)) - ultimo
    m7 = s.shift(1).rolling(7, min_periods=1).mean()
    dow4 = pd.Series(np.nan, index=s.index)
    for wd in range(7):
        sub = s[s.index.day_of_week == wd]
        v = sub.dropna().rolling(4, min_periods=1).mean().reindex(sub.index).ffill().shift(1)
        dow4.loc[sub.index] = v.values
    return pd.DataFrame({"nivel": nivel, "lag1": s.shift(1), "lag7": s.shift(7), "m7": m7,
                         "dow4": dow4, "dias_desde_ultimo": dias})


def build_features(panel: pd.DataFrame, targets: pd.DataFrame, cat: Catalogs) -> pd.DataFrame:
    """panel: fecha, tienda_id, familia_id, ventas[, en_promocion]  (historia conocida)
    targets: fecha, tienda_id, familia_id, en_promocion  (filas a pronosticar)
    Devuelve targets + columnas de FEATURES + 'nivel'. Las ventas de `panel` con fecha >= a la del
    target nunca entran a sus features (todas las ventanas están desplazadas al menos un día)."""
    panel = panel.copy()
    targets = targets.copy()
    panel["fecha"] = pd.to_datetime(panel["fecha"])
    targets["fecha"] = pd.to_datetime(targets["fecha"])
    if "en_promocion" not in panel:
        panel["en_promocion"] = np.nan
    fin = max(panel["fecha"].max() if len(panel) else targets["fecha"].min(), targets["fecha"].max())
    partes = []
    for (tid, fid), tg in targets.groupby(["tienda_id", "familia_id"], sort=False):
        p = panel[(panel["tienda_id"] == tid) & (panel["familia_id"] == fid)]
        p = p.groupby("fecha")[["ventas", "en_promocion"]].last()
        ini = min(p.index.min() if len(p) else tg["fecha"].min(), tg["fecha"].min())
        idx = pd.date_range(ini, fin, freq="D")
        s = p["ventas"].reindex(idx).astype(float)
        st = _series_stats(s)
        pr = p["en_promocion"].reindex(idx).astype(float)
        st["promo_media"] = pr.shift(1).rolling(28, min_periods=1).mean()
        st = st.reindex(tg["fecha"].values)
        st.index = tg.index
        partes.append(pd.concat([tg, st], axis=1))
    df = pd.concat(partes) if partes else targets.assign(nivel=np.nan)
    df = df.loc[targets.index]

    base = df["nivel"] + 1.0
    df["log_nivel"] = np.log1p(df["nivel"])
    for c in ("lag1", "lag7", "m7", "dow4"):
        df[f"r_{c}"] = df[c] / base
    df["promo"] = df["en_promocion"].astype(float)
    df["promo_ratio"] = df["promo"] / (df["promo_media"].fillna(df["promo"]) + 1.0)

    tiendas = cat.tiendas.set_index("tienda_id")
    df["tipo_cod"] = df["tienda_id"].map(tiendas["tipo"]).map({t: i for i, t in enumerate(TIPOS_TIENDA)})
    df["familia_cod"] = df["familia_id"].map({f: i for i, f in enumerate(cat.familia_ids)})
    f = df["fecha"]
    df["dow"] = f.dt.day_of_week
    df["dom"] = f.dt.day
    df["mes"] = f.dt.month
    df["fin_mes"] = (f.dt.days_in_month - f.dt.day <= 1).astype(int)
    df["quincena"] = f.dt.day.isin([14, 15, 16]).astype(int)
    ciudades = df["tienda_id"].map(tiendas["ciudad"])
    df = pd.concat([df, _feriado_flags(f, ciudades, cat)], axis=1)
    return df


def sales_panel(historico: pd.DataFrame, observaciones: pd.DataFrame | None,
                promos: pd.DataFrame | None = None) -> pd.DataFrame:
    """Une histórico + observaciones (la observación gana si se repite la llave).
    `promos` (de las solicitudes de pronóstico registradas) completa en_promocion de días observados."""
    h = historico[["fecha", "tienda_id", "familia_id", "ventas", "en_promocion"]].copy()
    h["fecha"] = pd.to_datetime(h["fecha"])
    if observaciones is not None and len(observaciones):
        o = observaciones[["fecha", "tienda_id", "familia_id", "ventas"]].copy()
        o["fecha"] = pd.to_datetime(o["fecha"])
        if promos is not None and len(promos):
            pr = promos[["fecha", "tienda_id", "familia_id", "en_promocion"]].copy()
            pr["fecha"] = pd.to_datetime(pr["fecha"])
            o = o.merge(pr, on=["fecha", "tienda_id", "familia_id"], how="left")
        else:
            o["en_promocion"] = np.nan
        h = pd.concat([h, o], ignore_index=True)
        h = h.drop_duplicates(["fecha", "tienda_id", "familia_id"], keep="last")
    h["ventas"] = h["ventas"].astype(float)
    return h.sort_values(["tienda_id", "familia_id", "fecha"]).reset_index(drop=True)
