"""DAG de reentrenamiento. Se dispara desde POST /admin/reentrenar (no tiene calendario).

Usa el histórico y las observaciones registradas en la base de datos del producto, entrena el
baseline y LightGBM, compara el candidato contra el modelo vigente en la misma ventana y solo
mueve el alias `vigente` si el candidato tiene menor costo. La lógica vive en novamercado.pipeline.
"""
from datetime import datetime, timedelta

from airflow.sdk import dag, task


@dag(
    dag_id="reentrenamiento",
    schedule=None,
    start_date=datetime(2024, 1, 1),
    catchup=False,
    is_paused_upon_creation=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(minutes=10),
    tags=["novamercado", "mlflow"],
)
def reentrenamiento():
    @task(retries=0)
    def verificar_datos():
        from novamercado.pipeline import repo_from_env

        h, o, p = repo_from_env().training_data()
        if len(h) == 0:
            raise RuntimeError("no hay histórico en la base de datos del producto")
        return {"historico": len(h), "observaciones": len(o), "promociones_registradas": len(p)}

    @task(retries=0, execution_timeout=timedelta(minutes=9))
    def entrenar_evaluar_promover(conteos: dict, **context):
        from novamercado.pipeline import retrain

        res = retrain(context["dag_run"].run_id)
        return {**conteos, **{k: res[k] for k in ("estado", "promovido", "version", "motivo")}}

    entrenar_evaluar_promover(verificar_datos())


reentrenamiento()
