"""Contrato del servicio (Anexo del enunciado) como modelos de Pydantic.

Las solicitudes crudas pasan primero por `cleaning` y el resultado se valida estrictamente aquí.
Las respuestas se construyen con estos modelos, de modo que nunca se omite ni renombra un campo.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


# ---------------------------------------------------------------- entradas ya limpias
class FamiliaPromo(_Strict):
    familia_id: str
    en_promocion: NonNegativeInt


class PrediccionRequest(_Strict):
    tienda_id: int
    fecha: date = Field(json_schema_extra={"format": "date"})
    familias: list[FamiliaPromo] = Field(min_length=1)

    model_config = ConfigDict(extra="forbid", strict=False)  # fecha llega como texto ISO


class VentaFamilia(_Strict):
    familia_id: str
    ventas: NonNegativeInt


class ObservacionRequest(_Strict):
    tienda_id: int
    fecha: date
    ventas: list[VentaFamilia]

    model_config = ConfigDict(extra="forbid", strict=False)


# ---------------------------------------------------------------- respuestas
class ObservacionCalidad(BaseModel):
    campo: str
    accion: Literal["reparar", "advertir"]
    detalle: str


class Calidad(BaseModel):
    estado: Literal["ok", "corregido", "advertencia"]
    observaciones: list[ObservacionCalidad] = []


class PrediccionFamilia(BaseModel):
    familia_id: str
    venta_esperada: float
    cantidad_sugerida: NonNegativeInt


class PrediccionResponse(BaseModel):
    tienda_id: int
    fecha: str
    version_modelo: str
    predicciones: list[PrediccionFamilia]
    calidad: Calidad


class ObservacionResponse(BaseModel):
    registros_guardados: NonNegativeInt
    calidad: Calidad


class ReentrenarResponse(BaseModel):
    id: str


class EstadoReentreno(BaseModel):
    estado: Literal["en_curso", "exitoso", "fallido"]
    promovido: bool | None = None
    version: str | None = None
    motivo: str | None = None


class ModeloResponse(BaseModel):
    nombre: str
    alias: str
    version: str


class HealthResponse(BaseModel):
    estado: Literal["ok", "degradado"]
    motivo: str | None = None
    dependencias: dict[str, str] = {}
    version_modelo: str | None = None
