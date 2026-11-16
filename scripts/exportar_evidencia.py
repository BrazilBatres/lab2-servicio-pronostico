"""Exporta evidencia verificable desde los servicios en ejecución a evidencia/ (JSON).

- MLflow: versiones del modelo (etiquetas, alias) y runs del experimento con sus métricas.
- Airflow: ejecuciones del DAG `reentrenamiento` y sus tareas.
- Base del producto: conteos de cada tabla y la tabla de reentrenamientos.

Uso (en el host, con el producto levantado): python3 scripts/exportar_evidencia.py
Las credenciales se leen de .env y no se imprimen.
"""
import json
import subprocess
import urllib.request
from pathlib import Path

OUT = Path("evidencia")
OUT.mkdir(exist_ok=True)
ENV = dict(l.split("=", 1) for l in Path(".env").read_text().splitlines() if "=" in l and not l.startswith("#"))


def get(url, headers=None, body=None, method=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})}, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def dump(name, obj):
    (OUT / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("escrito", OUT / name)


# ---------------------------------------------------------------- MLflow
M = "http://localhost:5000/api/2.0/mlflow"
versiones = get(f"{M}/model-versions/search?filter=name%3D%27novamercado_ventas%27&max_results=200")
modelo = get(f"{M}/registered-models/get?name=novamercado_ventas")
exp = get(f"{M}/experiments/get-by-name?experiment_name=novamercado_reentrenamiento")["experiment"]
runs = get(f"{M}/runs/search", body={"experiment_ids": [exp["experiment_id"]], "max_results": 500}, method="POST")
dump("mlflow_modelo.json", {"modelo_registrado": modelo, "versiones": versiones.get("model_versions", [])})
dump("mlflow_runs.json", [{"run_id": r["info"]["run_id"], "nombre": r["info"].get("run_name"),
                           "estado": r["info"]["status"],
                           "metricas": {m["key"]: m["value"] for m in r["data"].get("metrics", [])},
                           "params": {p["key"]: p["value"] for p in r["data"].get("params", [])},
                           "tags": {t["key"]: t["value"] for t in r["data"].get("tags", [])
                                    if not t["key"].startswith("mlflow.") or t["key"] == "mlflow.parentRunId"}}
                          for r in runs.get("runs", [])])

# ---------------------------------------------------------------- Airflow
A = "http://localhost:8080"
tok = get(f"{A}/auth/token", body={"username": ENV["AIRFLOW_USER"], "password": ENV["AIRFLOW_PASSWORD"]},
          method="POST")["access_token"]
H = {"Authorization": f"Bearer {tok}"}
dag_runs = get(f"{A}/api/v2/dags/reentrenamiento/dagRuns?limit=100&order_by=start_date", H)["dag_runs"]
for d in dag_runs:
    d["tareas"] = [{k: t.get(k) for k in ("task_id", "state", "start_date", "end_date", "duration", "try_number")}
                   for t in get(f"{A}/api/v2/dags/reentrenamiento/dagRuns/{d['dag_run_id']}/taskInstances", H)
                   ["task_instances"]]
dump("airflow_dag_runs.json", [{k: d.get(k) for k in ("dag_run_id", "state", "start_date", "end_date", "run_type",
                                                     "tareas")} for d in dag_runs])

# ---------------------------------------------------------------- base del producto
sql = """select json_build_object(
 'solicitudes', (select count(*) from solicitudes),
 'solicitudes_por_estado', (select json_object_agg(coalesce(estado, 'rechazo_'||http_status), n) from
     (select estado, http_status, count(*) n from solicitudes group by 1,2) s),
 'correcciones_por_accion', (select json_object_agg(accion, n) from (select accion, count(*) n from correcciones group by 1) c),
 'predicciones', (select count(*) from predicciones),
 'predicciones_por_version', (select json_object_agg(version_modelo, n) from
     (select version_modelo, count(*) n from predicciones group by 1) p),
 'observaciones', (select count(*) from observaciones),
 'ventas_historicas', (select count(*) from ventas_historicas),
 'reentrenamientos', (select json_agg(r order by creado_en) from (select id, estado, promovido, version, motivo,
     creado_en, terminado_en from reentrenamientos) r))"""
res = subprocess.run(["docker", "compose", "exec", "-T", "db", "psql", "-U", ENV.get("PRODUCT_DB_USER", "novamercado"),
                      "-d", ENV.get("PRODUCT_DB_NAME", "novamercado"), "-At", "-c", sql],
                     capture_output=True, text=True, check=True)
dump("base_de_datos.json", json.loads(res.stdout))
