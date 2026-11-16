"""Datos sintéticos para las pruebas (los datos publicados no forman parte del repositorio)."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from novamercado.catalogs import catalogs_from_tables  # noqa: E402

FAMILIAS = [("abarrotes", "Abarrotes"), ("bebidas", "Bebidas"), ("lacteos", "Lácteos"), ("carnes", "Carnes"),
            ("panaderia", "Panadería"), ("frutas_verduras", "Frutas y verduras"), ("limpieza", "Limpieza"),
            ("cuidado_personal", "Cuidado personal"), ("licores", "Licores"), ("snacks", "Snacks y dulces"),
            ("bebe", "Bebé"), ("mascotas", "Mascotas")]


def synthetic_tables(dias: int = 150, seed: int = 0) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    tiendas = pd.DataFrame({
        "tienda_id": [1, 2, 3], "nombre": ["Prueba Centro", "Prueba Norte", "Prueba Express"],
        "ciudad": ["Guatemala", "Mixco", "Cobán"], "departamento": ["G", "G", "AV"],
        "tipo": ["hipermercado", "supermercado", "express"],
        "fecha_apertura": ["2024-01-01", "2024-01-01", "2024-03-01"]})
    familias = pd.DataFrame(FAMILIAS, columns=["familia_id", "nombre"])
    costos = pd.DataFrame({"familia_id": familias.familia_id,
                           "costo_faltante": [1.0] * 12, "costo_sobrante": [0.25] * 11 + [3.0]})
    fechas = pd.date_range("2024-01-01", periods=dias, freq="D")
    feriados = pd.DataFrame({"fecha": ["2024-01-01", "2024-03-15", "2024-12-25"],
                             "nombre": ["Año nuevo", "Local", "Navidad"], "tipo": ["feriado", "feriado", "feriado"],
                             "ambito": ["nacional", "Cobán", "nacional"]})
    rows = []
    for t in tiendas.itertuples():
        for i, (f, _) in enumerate(FAMILIAS):
            base = 50 + 20 * i + 30 * t.tienda_id
            for d in fechas:
                if d < pd.Timestamp(t.fecha_apertura):
                    continue
                promo = int(rng.integers(0, 5))
                mu = base * (1.3 if d.dayofweek >= 5 else 1.0) * (1 + 0.05 * promo)
                rows.append((d.date().isoformat(), t.tienda_id, f, int(rng.poisson(mu)), promo))
    hist = pd.DataFrame(rows, columns=["fecha", "tienda_id", "familia_id", "ventas", "en_promocion"])
    return {"tiendas": tiendas, "familias": familias, "costos": costos, "feriados": feriados,
            "historico_ventas": hist}


@pytest.fixture(scope="session")
def tables():
    return synthetic_tables()


@pytest.fixture(scope="session")
def cat(tables):
    return catalogs_from_tables(tables)


def valid_pred(tienda=1, fecha="2024-05-20"):
    return {"tienda_id": tienda, "fecha": fecha,
            "familias": [{"familia_id": f, "en_promocion": 0} for f, _ in FAMILIAS]}


def valid_obs(tienda=1, fecha="2024-05-20", valor=100):
    return {"tienda_id": tienda, "fecha": fecha,
            "ventas": [{"familia_id": f, "ventas": valor + i} for i, (f, _) in enumerate(FAMILIAS)]}
