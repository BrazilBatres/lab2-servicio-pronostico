"""Persistencia del producto (PostgreSQL propia, separada de la de Airflow).

`PostgresRepo` es la implementación real; `MemoryRepo` implementa la misma interfaz en memoria
para las pruebas (sin servidor).
"""
from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS catalogos (
    nombre TEXT PRIMARY KEY, contenido JSONB NOT NULL, cargado_en TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS ventas_historicas (
    fecha DATE NOT NULL, tienda_id INT NOT NULL, familia_id TEXT NOT NULL,
    ventas DOUBLE PRECISION NOT NULL, en_promocion DOUBLE PRECISION,
    PRIMARY KEY (tienda_id, familia_id, fecha));
CREATE TABLE IF NOT EXISTS solicitudes (
    id BIGSERIAL PRIMARY KEY, recibido_en TIMESTAMPTZ DEFAULT now(), endpoint TEXT NOT NULL,
    payload JSONB, http_status INT, estado TEXT, detalle TEXT, tienda_id INT, fecha DATE);
CREATE TABLE IF NOT EXISTS correcciones (
    id BIGSERIAL PRIMARY KEY, solicitud_id BIGINT REFERENCES solicitudes(id),
    campo TEXT, accion TEXT, detalle TEXT);
CREATE TABLE IF NOT EXISTS predicciones (
    id BIGSERIAL PRIMARY KEY, solicitud_id BIGINT REFERENCES solicitudes(id),
    creado_en TIMESTAMPTZ DEFAULT now(), tienda_id INT NOT NULL, fecha DATE NOT NULL,
    familia_id TEXT NOT NULL, en_promocion INT, venta_esperada DOUBLE PRECISION,
    cantidad_sugerida INT, version_modelo TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS predicciones_llave ON predicciones (tienda_id, familia_id, fecha);
CREATE TABLE IF NOT EXISTS observaciones (
    tienda_id INT NOT NULL, fecha DATE NOT NULL, familia_id TEXT NOT NULL, ventas INT NOT NULL,
    solicitud_id BIGINT REFERENCES solicitudes(id), recibido_en TIMESTAMPTZ DEFAULT now(),
    actualizado_en TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (tienda_id, familia_id, fecha));
CREATE TABLE IF NOT EXISTS reentrenamientos (
    id TEXT PRIMARY KEY, creado_en TIMESTAMPTZ DEFAULT now(), terminado_en TIMESTAMPTZ,
    estado TEXT NOT NULL, promovido BOOLEAN, version TEXT, motivo TEXT, metricas JSONB);
"""

HIST_COLS = ["fecha", "tienda_id", "familia_id", "ventas", "en_promocion"]


class PostgresRepo:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def _conn(self):
        import psycopg
        return psycopg.connect(self.dsn, connect_timeout=3, autocommit=False)

    def ping(self) -> None:
        with self._conn() as c:
            c.execute("SELECT 1")

    def init_schema(self) -> None:
        with self._conn() as c:
            c.execute("SELECT pg_advisory_xact_lock(4242)")
            c.execute(SCHEMA)

    # ---------------------------------------------------------------- carga inicial
    def historico_count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT count(*) FROM ventas_historicas").fetchone()[0]

    def load_historico(self, df: pd.DataFrame) -> int:
        with self._conn() as c, c.cursor() as cur:
            with cur.copy("COPY ventas_historicas (fecha, tienda_id, familia_id, ventas, en_promocion) "
                          "FROM STDIN") as cp:
                for r in df[HIST_COLS].itertuples(index=False):
                    cp.write_row((r.fecha, int(r.tienda_id), r.familia_id, float(r.ventas),
                                  None if pd.isna(r.en_promocion) else float(r.en_promocion)))
        return len(df)

    def save_catalogs(self, cat: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO catalogos (nombre, contenido) VALUES ('catalogos', %s) "
                      "ON CONFLICT (nombre) DO UPDATE SET contenido = EXCLUDED.contenido, cargado_en = now()",
                      (json.dumps(cat, default=str),))

    def load_catalogs(self) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT contenido FROM catalogos WHERE nombre = 'catalogos'").fetchone()
            return r[0] if r else None

    # ---------------------------------------------------------------- operación
    def log_solicitud(self, endpoint: str, payload: Any, http_status: int, estado: str | None,
                      detalle: str | None, tienda_id: int | None = None, fecha: str | None = None,
                      notas: list | None = None) -> int:
        with self._conn() as c:
            sid = c.execute(
                "INSERT INTO solicitudes (endpoint, payload, http_status, estado, detalle, tienda_id, fecha) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (endpoint, json.dumps(payload, default=str), http_status, estado, detalle, tienda_id, fecha),
            ).fetchone()[0]
            if notas:
                with c.cursor() as cur:
                    cur.executemany("INSERT INTO correcciones (solicitud_id, campo, accion, detalle) "
                                    "VALUES (%s, %s, %s, %s)",
                                    [(sid, n.campo, n.accion, n.detalle) for n in notas])
            return sid

    def save_predicciones(self, sid: int, tienda_id: int, fecha: str, rows: list[dict], version: str) -> None:
        with self._conn() as c, c.cursor() as cur:
            cur.executemany(
                "INSERT INTO predicciones (solicitud_id, tienda_id, fecha, familia_id, en_promocion, "
                "venta_esperada, cantidad_sugerida, version_modelo) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                [(sid, tienda_id, fecha, r["familia_id"], r.get("en_promocion"), r["venta_esperada"],
                  r["cantidad_sugerida"], version) for r in rows])

    def upsert_observaciones(self, sid: int | None, tienda_id: int, fecha: str, rows: list[dict]) -> int:
        """Idempotente: una observación idéntica no cambia nada y no cuenta."""
        n = 0
        with self._conn() as c:
            for r in rows:
                res = c.execute(
                    "INSERT INTO observaciones (tienda_id, fecha, familia_id, ventas, solicitud_id) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (tienda_id, familia_id, fecha) DO UPDATE "
                    "SET ventas = EXCLUDED.ventas, solicitud_id = EXCLUDED.solicitud_id, actualizado_en = now() "
                    "WHERE observaciones.ventas IS DISTINCT FROM EXCLUDED.ventas RETURNING 1",
                    (tienda_id, fecha, r["familia_id"], int(r["ventas"]), sid)).fetchone()
                n += 1 if res else 0
        return n

    def existing_observaciones(self, tienda_id: int, fecha: str) -> dict[str, int]:
        with self._conn() as c:
            rows = c.execute("SELECT familia_id, ventas FROM observaciones WHERE tienda_id=%s AND fecha=%s",
                             (tienda_id, fecha)).fetchall()
        return {f: v for f, v in rows}

    def history_for(self, tienda_id: int, fecha: str, dias: int = 120) -> pd.DataFrame:
        """Ventas de la tienda con fecha < `fecha` (histórico ∪ observaciones; la observación gana)."""
        desde = (date.fromisoformat(fecha) - timedelta(days=dias)).isoformat()
        sql = """
        WITH obs AS (
          SELECT o.fecha, o.tienda_id, o.familia_id, o.ventas::float AS ventas,
                 (SELECT p.en_promocion::float FROM predicciones p
                   WHERE p.tienda_id=o.tienda_id AND p.familia_id=o.familia_id AND p.fecha=o.fecha
                   ORDER BY p.id DESC LIMIT 1) AS en_promocion
          FROM observaciones o WHERE o.tienda_id=%(t)s AND o.fecha < %(f)s AND o.fecha >= %(d)s)
        SELECT * FROM obs
        UNION ALL
        SELECT h.fecha, h.tienda_id, h.familia_id, h.ventas, h.en_promocion FROM ventas_historicas h
        WHERE h.tienda_id=%(t)s AND h.fecha < %(f)s AND h.fecha >= %(d)s
          AND NOT EXISTS (SELECT 1 FROM obs WHERE obs.fecha=h.fecha AND obs.familia_id=h.familia_id)
        """
        with self._conn() as c:
            rows = c.execute(sql, {"t": tienda_id, "f": fecha, "d": desde}).fetchall()
        return pd.DataFrame(rows, columns=HIST_COLS)

    def training_data(self) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        with self._conn() as c:
            h = pd.DataFrame(c.execute(f"SELECT {', '.join(HIST_COLS)} FROM ventas_historicas").fetchall(),
                             columns=HIST_COLS)
            o = pd.DataFrame(c.execute("SELECT fecha, tienda_id, familia_id, ventas FROM observaciones").fetchall(),
                             columns=["fecha", "tienda_id", "familia_id", "ventas"])
            p = pd.DataFrame(c.execute(
                "SELECT DISTINCT ON (tienda_id, familia_id, fecha) fecha, tienda_id, familia_id, en_promocion "
                "FROM predicciones ORDER BY tienda_id, familia_id, fecha, id DESC").fetchall(),
                columns=["fecha", "tienda_id", "familia_id", "en_promocion"])
        return h, o, p

    # ---------------------------------------------------------------- reentrenamientos
    def create_reentreno(self, rid: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO reentrenamientos (id, estado) VALUES (%s, 'en_curso') "
                      "ON CONFLICT (id) DO NOTHING", (rid,))

    def delete_reentreno(self, rid: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM reentrenamientos WHERE id=%s", (rid,))

    def finish_reentreno(self, rid: str, estado: str, promovido: bool, version: str | None, motivo: str,
                         metricas: dict | None = None) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO reentrenamientos (id, estado, promovido, version, motivo, metricas, terminado_en) "
                "VALUES (%s,%s,%s,%s,%s,%s, now()) ON CONFLICT (id) DO UPDATE SET estado=EXCLUDED.estado, "
                "promovido=EXCLUDED.promovido, version=EXCLUDED.version, motivo=EXCLUDED.motivo, "
                "metricas=EXCLUDED.metricas, terminado_en=now()",
                (rid, estado, promovido, version, motivo, json.dumps(metricas or {}, default=str)))

    def get_reentreno(self, rid: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT estado, promovido, version, motivo FROM reentrenamientos WHERE id=%s",
                          (rid,)).fetchone()
        return dict(zip(["estado", "promovido", "version", "motivo"], r)) if r else None


class MemoryRepo:
    """Misma interfaz que PostgresRepo, en memoria (para pruebas)."""

    def __init__(self, historico: pd.DataFrame | None = None, catalogs: dict | None = None):
        self.lock = threading.Lock()
        self.hist = historico.copy() if historico is not None else pd.DataFrame(columns=HIST_COLS)
        self.catalogs = catalogs
        self.solicitudes: list[dict] = []
        self.correcciones: list[dict] = []
        self.predicciones: list[dict] = []
        self.obs: dict[tuple, int] = {}
        self.reentrenos: dict[str, dict] = {}
        self.down = False

    def ping(self):
        if self.down:
            raise ConnectionError("db caída")

    def init_schema(self):
        pass

    def historico_count(self):
        return len(self.hist)

    def load_historico(self, df):
        self.hist = df[HIST_COLS].copy()
        return len(df)

    def save_catalogs(self, cat):
        self.catalogs = cat

    def load_catalogs(self):
        return self.catalogs

    def log_solicitud(self, endpoint, payload, http_status, estado, detalle, tienda_id=None, fecha=None, notas=None):
        self.ping()
        sid = len(self.solicitudes) + 1
        self.solicitudes.append(dict(id=sid, endpoint=endpoint, payload=payload, http_status=http_status,
                                     estado=estado, detalle=detalle))
        for n in notas or []:
            self.correcciones.append(dict(solicitud_id=sid, **n.dict()))
        return sid

    def save_predicciones(self, sid, tienda_id, fecha, rows, version):
        for r in rows:
            self.predicciones.append(dict(solicitud_id=sid, tienda_id=tienda_id, fecha=fecha,
                                          version_modelo=version, **r))

    def upsert_observaciones(self, sid, tienda_id, fecha, rows):
        self.ping()
        n = 0
        with self.lock:
            for r in rows:
                k = (tienda_id, r["familia_id"], fecha)
                if self.obs.get(k) != int(r["ventas"]):
                    self.obs[k] = int(r["ventas"])
                    n += 1
        return n

    def existing_observaciones(self, tienda_id, fecha):
        return {k[1]: v for k, v in self.obs.items() if k[0] == tienda_id and k[2] == fecha}

    def _obs_df(self):
        return pd.DataFrame([dict(fecha=k[2], tienda_id=k[0], familia_id=k[1], ventas=float(v))
                             for k, v in self.obs.items()], columns=["fecha", "tienda_id", "familia_id", "ventas"])

    def history_for(self, tienda_id, fecha, dias=120):
        self.ping()
        f = pd.Timestamp(fecha)
        h = self.hist.copy()
        h["fecha"] = pd.to_datetime(h["fecha"])
        o = self._obs_df()
        o["fecha"] = pd.to_datetime(o["fecha"])
        o["en_promocion"] = float("nan")
        df = pd.concat([h, o]).drop_duplicates(["fecha", "tienda_id", "familia_id"], keep="last")
        df = df[(df.tienda_id == tienda_id) & (df.fecha < f) & (df.fecha >= f - pd.Timedelta(days=dias))]
        return df[HIST_COLS].reset_index(drop=True)

    def training_data(self):
        p = pd.DataFrame(self.predicciones)
        if len(p):
            p = p.drop_duplicates(["tienda_id", "familia_id", "fecha"], keep="last")[
                ["fecha", "tienda_id", "familia_id", "en_promocion"]]
        return self.hist.copy(), self._obs_df(), p

    def create_reentreno(self, rid):
        self.reentrenos.setdefault(rid, {"estado": "en_curso", "promovido": None, "version": None, "motivo": None})

    def delete_reentreno(self, rid):
        self.reentrenos.pop(rid, None)

    def finish_reentreno(self, rid, estado, promovido, version, motivo, metricas=None):
        self.reentrenos[rid] = {"estado": estado, "promovido": promovido, "version": version, "motivo": motivo}

    def get_reentreno(self, rid):
        return self.reentrenos.get(rid)


def utcnow() -> datetime:
    """Solo para marcas de auditoría; nunca para validar fechas de negocio."""
    return datetime.now(timezone.utc)
