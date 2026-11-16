"""Ciclo de reentrenamiento con MLflow en archivos locales (sin servidor) y repositorio en memoria."""
import pandas as pd
import pytest

mlflow = pytest.importorskip("mlflow")

from novamercado import pipeline, registry  # noqa: E402
from novamercado.db import MemoryRepo  # noqa: E402


@pytest.fixture
def repo(tmp_path, tables, cat, monkeypatch):
    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
    mlflow.set_tracking_uri(f"file:{tmp_path / 'mlruns'}")
    monkeypatch.setattr(registry, "MODEL_NAME", f"prueba_{tmp_path.name}")
    return MemoryRepo(tables["historico_ventas"], cat.to_dict())


def _add_obs(repo, tables, desde, dias, factor):
    h = tables["historico_ventas"]
    h = h[pd.to_datetime(h.fecha) >= pd.Timestamp(desde) - pd.Timedelta(days=28)].copy()
    for i in range(dias):
        d = pd.Timestamp(desde) + pd.Timedelta(days=i)
        ref = h[pd.to_datetime(h.fecha) == d - pd.Timedelta(days=28)]
        for t, g in ref.groupby("tienda_id"):
            repo.upsert_observaciones(None, int(t), d.date().isoformat(),
                                      [{"familia_id": r.familia_id, "ventas": int(r.ventas * factor)}
                                       for r in g.itertuples()])


def test_ciclo_completo(repo, tables):
    r1 = pipeline.retrain("r1", repo)
    assert r1["promovido"] is True and registry.vigente_version() == r1["version"]

    # sin datos nuevos: no hay evidencia para reemplazar; el vigente no cambia
    r2 = pipeline.retrain("r2", repo)
    assert r2["promovido"] is False and r2["version"] == r1["version"]
    assert "días con ventas nuevas" in r2["motivo"]
    assert repo.get_reentreno("r2")["estado"] == "exitoso"

    # con 14 días nuevos se compara en esos días (que el vigente no vio)
    _add_obs(repo, tables, "2024-05-30", 14, 1.4)
    r3 = pipeline.retrain("r3", repo)
    assert r3["estado"] == "exitoso" and "vigente" in r3["motivo"]
    assert registry.vigente_version() == r3["version"]


def test_fallo_no_afecta_al_vigente(repo, tables, monkeypatch):
    r1 = pipeline.retrain("r1", repo)
    monkeypatch.setattr(pipeline, "train_all", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    _add_obs(repo, tables, "2024-05-30", 8, 1.0)
    with pytest.raises(RuntimeError):
        pipeline.retrain("rx", repo)
    assert registry.vigente_version() == r1["version"]
    st = repo.get_reentreno("rx")
    assert st["estado"] == "fallido" and st["promovido"] is False and st["version"] == r1["version"]
