"""Features sin fuga, cantidad sugerida coherente con los costos y criterio de reemplazo."""
import numpy as np
import pandas as pd

from novamercado.features import build_features, sales_panel
from novamercado.model import costo, decide, fit_multiplicadores, train_all


def test_features_no_usan_el_futuro(tables, cat):
    panel = sales_panel(tables["historico_ventas"], None)
    tg = pd.DataFrame({"fecha": ["2024-04-10"], "tienda_id": [1], "familia_id": ["abarrotes"], "en_promocion": [2]})
    a = build_features(panel, tg, cat)
    alterado = panel.copy()
    alterado.loc[alterado["fecha"] >= "2024-04-10", "ventas"] = 10 ** 6
    b = build_features(alterado, tg, cat)
    cols = ["nivel", "lag1", "lag7", "m7", "dow4", "r_lag1"]
    pd.testing.assert_frame_equal(a[cols], b[cols])


def test_features_toleran_huecos(tables, cat):
    panel = sales_panel(tables["historico_ventas"], None)
    panel = panel[panel["fecha"] < "2024-04-01"]  # 10 días sin datos antes del objetivo
    tg = pd.DataFrame({"fecha": ["2024-04-10"], "tienda_id": [1], "familia_id": ["abarrotes"], "en_promocion": [0]})
    f = build_features(panel, tg, cat)
    assert f["nivel"].notna().all() and int(f["dias_desde_ultimo"].iloc[0]) == 10


def test_multiplicador_es_el_cuantil_newsvendor(cat):
    rng = np.random.default_rng(1)
    n = 2000
    fam = np.array(["abarrotes"] * n + ["mascotas"] * n)
    esp = np.full(2 * n, 100.0)
    real = rng.normal(100, 20, 2 * n)
    k = fit_multiplicadores(real, esp, fam, cat)
    # abarrotes: c_f=1, c_s=0.25 -> q=0.8 (pedir más); mascotas: c_f=1, c_s=3 -> q=0.25 (pedir menos)
    assert k["abarrotes"] > 1.1 and k["mascotas"] < 0.95
    assert abs(k["abarrotes"] - np.quantile(real[:n] / 100, 0.8)) < 1e-9


def test_costo_asimetrico(cat):
    c = costo([10, 10], [8, 12], ["mascotas", "mascotas"], cat)
    assert list(c) == [2.0, 6.0]


def test_entrenamiento_supera_baseline(tables, cat):
    e = train_all(sales_panel(tables["historico_ventas"], None), cat, val_dias=21)
    assert set(e.metricas) == {"baseline", "lgbm"}
    assert e.metricas["lgbm"]["costo"] < e.metricas["baseline"]["costo"]
    assert e.mejor == "lgbm"


def test_criterio_de_reemplazo():
    assert decide(100.0, None)[0] is True
    ok, motivo = decide(18210, 19940)
    assert ok and motivo == "Costo en validación 18 210 vs 19 940 del vigente"
    assert decide(20000, 19940)[0] is False
