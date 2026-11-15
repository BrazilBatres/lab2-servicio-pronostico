"""Catálogos del negocio (tiendas, familias, costos, feriados) y lectura de los datos publicados."""
from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

ARCHIVOS = ("tiendas.csv", "familias.csv", "historico_ventas.csv", "feriados.csv", "costos.csv")


@dataclass
class Catalogs:
    tiendas: pd.DataFrame  # tienda_id, nombre, ciudad, departamento, tipo, fecha_apertura
    familias: pd.DataFrame  # familia_id, nombre
    costos: pd.DataFrame  # familia_id, costo_faltante, costo_sobrante
    feriados: pd.DataFrame  # fecha, nombre, tipo, ambito
    historia_inicio: date | None = None
    historia_fin: date | None = None
    _cache: dict = field(default_factory=dict, repr=False)

    @property
    def tienda_ids(self) -> list[int]:
        return sorted(int(t) for t in self.tiendas["tienda_id"])

    @property
    def familia_ids(self) -> list[str]:
        return list(self.familias["familia_id"])

    def ciudad(self, tienda_id: int) -> str:
        return str(self.tiendas.set_index("tienda_id").loc[tienda_id, "ciudad"])

    def cuantil(self, familia_id: str) -> float:
        """Cuantil crítico del newsvendor: c_f / (c_f + c_s)."""
        row = self.costos.set_index("familia_id").loc[familia_id]
        return float(row.costo_faltante / (row.costo_faltante + row.costo_sobrante))

    @property
    def feriados_fin(self) -> date | None:
        if self.feriados.empty:
            return None
        return pd.to_datetime(self.feriados["fecha"]).max().date()

    def to_dict(self) -> dict:
        return {
            "tiendas": self.tiendas.to_dict("list"),
            "familias": self.familias.to_dict("list"),
            "costos": self.costos.to_dict("list"),
            "feriados": self.feriados.to_dict("list"),
            "historia_inicio": self.historia_inicio.isoformat() if self.historia_inicio else None,
            "historia_fin": self.historia_fin.isoformat() if self.historia_fin else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Catalogs":
        return cls(
            tiendas=pd.DataFrame(d["tiendas"]),
            familias=pd.DataFrame(d["familias"]),
            costos=pd.DataFrame(d["costos"]),
            feriados=pd.DataFrame(d["feriados"]),
            historia_inicio=date.fromisoformat(d["historia_inicio"]) if d.get("historia_inicio") else None,
            historia_fin=date.fromisoformat(d["historia_fin"]) if d.get("historia_fin") else None,
        )


def _find_dir(root: Path) -> Path | None:
    for p in [root, *sorted(root.rglob("*"))]:
        if p.is_dir() and all((p / f).exists() for f in ARCHIVOS):
            return p
    return None


def read_published(root: str | Path) -> dict[str, pd.DataFrame]:
    """Lee los CSV publicados desde una carpeta (sueltos, en una subcarpeta o dentro de un .zip)."""
    root = Path(root)
    d = _find_dir(root)
    if d is not None:
        return {f[:-4]: pd.read_csv(d / f) for f in ARCHIVOS}
    for z in sorted(root.rglob("*.zip")):
        with zipfile.ZipFile(z) as zf:
            names = {Path(n).name: n for n in zf.namelist()}
            if all(f in names for f in ARCHIVOS):
                return {f[:-4]: pd.read_csv(io.BytesIO(zf.read(names[f]))) for f in ARCHIVOS}
    raise FileNotFoundError(f"No se encontraron {', '.join(ARCHIVOS)} en {root} (ni en un .zip)")


def catalogs_from_tables(t: dict[str, pd.DataFrame]) -> Catalogs:
    h = t.get("historico_ventas")
    ini = fin = None
    if h is not None and len(h):
        fechas = pd.to_datetime(h["fecha"])
        ini, fin = fechas.min().date(), fechas.max().date()
    tiendas = t["tiendas"].copy()
    tiendas["tienda_id"] = tiendas["tienda_id"].astype(int)
    return Catalogs(
        tiendas=tiendas,
        familias=t["familias"].copy(),
        costos=t["costos"].copy(),
        feriados=t["feriados"].copy(),
        historia_inicio=ini,
        historia_fin=fin,
    )
