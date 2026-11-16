"""Simulador de la operación diaria (similar al programa de evaluación del enunciado).

Las ventas "reales" son SINTÉTICAS: las del mismo día de la semana de hace 52 semanas, con ruido
y un cambio de nivel de la demanda a partir de cierto día. Sirve para probar el servicio, no para
medir la calidad del modelo en el periodo real de evaluación.

Uso (con el producto levantado):
    python3 scripts/simular_operacion.py --datos datos --desde 2026-03-01 --dias 28
"""
from __future__ import annotations

import argparse
import copy
import io
import json
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, timedelta
from pathlib import Path

import csv

API = "http://localhost:8000"


def http(method, path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")
    except Exception as e:
        return 0, {"error": str(e)}


def leer_csv(datos: Path, nombre: str) -> list[dict]:
    for p in datos.rglob(nombre):
        return list(csv.DictReader(open(p, encoding="utf-8")))
    for z in datos.rglob("*.zip"):
        with zipfile.ZipFile(z) as zf:
            for n in zf.namelist():
                if n.endswith(nombre):
                    return list(csv.DictReader(io.TextIOWrapper(zf.open(n), encoding="utf-8")))
    raise FileNotFoundError(nombre)


# ------------------------------------------------------------------ errores (incluye no catalogados)
def m_tienda(r):
    r["tienda_id"] = random.choice([f"T{r['tienda_id']:02d}", f" {r['tienda_id']} ", f"tienda-{r['tienda_id']}",
                                    float(r["tienda_id"])])
    return "tienda con otro formato"


def m_familia(r):
    i = random.randrange(len(r["familias"]))
    f = r["familias"][i]["familia_id"]
    r["familias"][i]["familia_id"] = random.choice([f.upper(), f" {f.replace('_', ' ').title()} ", f.replace("_", "-")])
    return "familia con otra forma"


def m_promo(r):
    i = random.randrange(len(r["familias"]))
    p = r["familias"][i]["en_promocion"]
    r["familias"][i]["en_promocion"] = random.choice([str(p), float(p), f"{p}.0", None])
    return "promoción como texto/decimal/nula"


def m_fecha(r):
    d = date.fromisoformat(r["fecha"])
    r["fecha"] = random.choice([d.strftime("%d/%m/%Y"), d.isoformat() + "T08:00:00", d.strftime("%Y/%m/%d"),
                                d.strftime("%Y%m%d")])
    return "fecha en otro formato"


def m_faltan(r):
    r["familias"] = r["familias"][: random.randint(6, 11)]
    return "faltan familias"


def m_duplicada(r):
    r["familias"].append(copy.deepcopy(r["familias"][0]))
    r["familias"][-1]["en_promocion"] += random.choice([0, 3])
    return "familia repetida"


def m_campos(r):
    r["Tienda"] = r.pop("tienda_id")
    for f in r["familias"]:
        f["Familia"] = f.pop("familia_id")
    r["sistema_origen"] = "pos-v3"
    return "campos renombrados + campo extra"


def m_familia_desconocida(r):
    r["familias"].append({"familia_id": "electronica", "en_promocion": 0})
    return "familia desconocida"


def m_tienda_invalida(r):
    r["tienda_id"] = 99
    return "tienda inexistente (debe rechazarse)"


MUTADORES = [m_tienda, m_familia, m_promo, m_fecha, m_faltan, m_duplicada, m_campos, m_familia_desconocida,
             m_tienda_invalida]


def m_obs(o):
    i = random.randrange(len(o["ventas"]))
    v = o["ventas"][i]["ventas"]
    kind = random.choice(["miles", "coma", "negativa", "texto"])
    o["ventas"][i]["ventas"] = {"miles": f"{v:,}", "coma": f"{v},0", "negativa": -abs(v) - 1, "texto": f" {v} "}[kind]
    return f"observación con número '{kind}'"


# ------------------------------------------------------------------ utilidades de operación
def compose(*args):
    return subprocess.run(["docker", "compose", *args], capture_output=True, text=True)


def esperar(cond, timeout, paso=2):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return round(time.time() - t0, 1)
        time.sleep(paso)
    return None


def health_estado():
    s, b = http("GET", "/health", timeout=5)
    return s, (b or {}).get("estado")


def reentrenar(log):
    t0 = time.time()
    s, b = http("POST", "/admin/reentrenar")
    if s != 202:
        log.append({"evento": "reentreno", "http": s, "respuesta": b})
        return None
    rid = b["id"]
    while time.time() - t0 < 900:
        _, st = http("GET", f"/admin/reentrenar/{rid}")
        if st.get("estado") != "en_curso":
            break
        time.sleep(5)
    fin = round(time.time() - t0, 1)
    swap = None
    if st.get("promovido"):
        swap = esperar(lambda: http("GET", "/modelo")[1].get("version") == st.get("version"), 180)
    ev = {"evento": "reentreno", "id": rid, "duracion_s": fin, "estado": st, "segundos_hasta_usar_nuevo": swap}
    log.append(ev)
    print(json.dumps(ev, ensure_ascii=False))
    return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--datos", default="datos")
    ap.add_argument("--desde", default="2026-03-01")
    ap.add_argument("--dias", type=int, default=28)
    ap.add_argument("--reentreno-cada", type=int, default=7)
    ap.add_argument("--cambio-dia", type=int, default=10, help="día desde el que la demanda cambia de nivel")
    ap.add_argument("--factor", type=float, default=1.3)
    ap.add_argument("--mlflow-caida-dia", type=int, default=12)
    ap.add_argument("--reinicio", action="store_true", help="al final: docker compose down/up (sin -v)")
    ap.add_argument("--salida", default="evidencia/simulacion")
    ap.add_argument("--semilla", type=int, default=7)
    a = ap.parse_args()
    random.seed(a.semilla)
    out = Path(a.salida)
    out.mkdir(parents=True, exist_ok=True)

    hist = leer_csv(Path(a.datos), "historico_ventas.csv")
    costos = {r["familia_id"]: (float(r["costo_faltante"]), float(r["costo_sobrante"]))
              for r in leer_csv(Path(a.datos), "costos.csv")}
    tiendas = sorted({int(r["tienda_id"]) for r in hist})
    H = {(r["fecha"], int(r["tienda_id"]), r["familia_id"]): (int(r["ventas"]), int(r["en_promocion"])) for r in hist}
    familias = sorted({r["familia_id"] for r in hist})

    def real(d: date, t: int, f: str):
        v, p = H.get(((d - timedelta(days=364)).isoformat(), t, f), (0, 0))
        k = a.factor if (d - date.fromisoformat(a.desde)).days >= a.cambio_dia else 1.0
        return max(0, int(round(v * k * random.lognormvariate(0, 0.08)))), p

    log, ejemplos, costo_total, n_pred = [], [], 0.0, 0
    estados = {}
    inicio = date.fromisoformat(a.desde)
    reales_dia = {}
    for i in range(a.dias):
        d = inicio + timedelta(days=i)
        # 1) observaciones del día anterior (llegan antes que las solicitudes del día)
        if i > 0:
            ayer = d - timedelta(days=1)
            for t in tiendas:
                o = {"tienda_id": t, "fecha": ayer.isoformat(),
                     "ventas": [{"familia_id": f, "ventas": reales_dia[(t, f)]} for f in familias]}
                motivo = m_obs(o) if random.random() < 0.15 else "válida"
                s, b = http("POST", "/v1/observaciones", o)
                if random.random() < 0.1:  # reenvío idéntico (E09)
                    s2, b2 = http("POST", "/v1/observaciones", o)
                    log.append({"evento": "reenvio_observacion", "registros_guardados": b2.get("registros_guardados")})
                if motivo != "válida" and len(ejemplos) < 60:
                    ejemplos.append({"endpoint": "/v1/observaciones", "problema": motivo, "solicitud": o,
                                     "http": s, "respuesta": b})
        # 2) solicitudes de pronóstico del día
        reales_dia = {}
        for t in tiendas:
            fams = []
            for f in familias:
                v, p = real(d, t, f)
                reales_dia[(t, f)] = v
                fams.append({"familia_id": f, "en_promocion": p})
            req = {"tienda_id": t, "fecha": d.isoformat(), "familias": fams}
            motivo = "válida"
            if random.random() < 0.3:
                motivo = random.choice(MUTADORES)(req)
            s, b = http("POST", "/v1/prediccion", req)
            estados[str(s) + ":" + (b.get("calidad", {}).get("estado", "-") if s == 200 else "rechazo")] = \
                estados.get(str(s) + ":" + (b.get("calidad", {}).get("estado", "-") if s == 200 else "rechazo"), 0) + 1
            if s == 200:
                for p in b["predicciones"]:
                    cf, cs = costos[p["familia_id"]]
                    r_ = reales_dia[(t, p["familia_id"])]
                    costo_total += cf * max(r_ - p["cantidad_sugerida"], 0) + cs * max(p["cantidad_sugerida"] - r_, 0)
                    n_pred += 1
            if (motivo != "válida" or i == 0 and t == tiendas[0]) and len(ejemplos) < 60:
                ejemplos.append({"endpoint": "/v1/prediccion", "problema": motivo, "solicitud": req,
                                 "http": s, "respuesta": b})
        print(f"{d} ok  costo acumulado={costo_total:,.0f}  modelo={http('GET', '/modelo')[1].get('version')}")

        # 3) interrupción de MLflow
        if i == a.mlflow_caida_dia:
            compose("stop", "mlflow")
            t_deg = esperar(lambda: health_estado() == (200, "degradado"), 120)
            s_pred, _ = http("POST", "/v1/prediccion", {"tienda_id": tiendas[0], "fecha": d.isoformat(),
                                                         "familias": [{"familia_id": familias[0], "en_promocion": 0}]})
            st = reentrenar(log)  # debe fallar sin tocar el vigente
            compose("start", "mlflow")
            t_ok = esperar(lambda: health_estado() == (200, "ok"), 240)
            ev = {"evento": "caida_mlflow", "segundos_hasta_degradado": t_deg, "prediccion_durante_caida_http": s_pred,
                  "reentreno_durante_caida": st, "segundos_hasta_ok_al_volver": t_ok}
            log.append(ev)
            print(json.dumps(ev, ensure_ascii=False))
        # 4) reentrenamiento periódico
        if (i + 1) % a.reentreno_cada == 0:
            reentrenar(log)

    if a.reinicio:
        t0 = time.time()
        compose("down")
        compose("up", "-d")
        t_ok = esperar(lambda: health_estado() == (200, "ok"), 600, 5)
        ultimo = inicio + timedelta(days=a.dias - 2)
        o = {"tienda_id": tiendas[0], "fecha": ultimo.isoformat(),
             "ventas": [{"familia_id": f, "ventas": 1} for f in familias]}
        ev = {"evento": "reinicio_sin_borrar_volumenes", "segundos_hasta_ok": t_ok,
              "total_s": round(time.time() - t0, 1), "modelo_tras_reinicio": http("GET", "/modelo")[1],
              "bootstrap_log": compose("logs", "bootstrap").stdout.strip().splitlines()[-3:]}
        log.append(ev)
        print(json.dumps(ev, ensure_ascii=False))

    resumen = {"dias": a.dias, "desde": a.desde, "predicciones_familia": n_pred, "costo_total": round(costo_total, 1),
               "respuestas_por_estado": estados, "eventos": log}
    (out / "resumen.json").write_text(json.dumps(resumen, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "ejemplos_respuestas.json").write_text(json.dumps(ejemplos, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in resumen.items() if k != "eventos"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(main())
