# NovaMercado — Servicio de pronóstico diario de ventas (Laboratorio 2)

Servicio de ML en producción: recibe solicitudes de pronóstico por tienda y fecha, se defiende de
datos defectuosos, registra predicciones y ventas reales en PostgreSQL y se reentrena con Airflow,
registrando experimentos y modelos en MLflow. Todo se levanta con `docker compose up`.

| Componente | Servicio Compose | Puerto | Papel |
|---|---|---|---|
| API (FastAPI) | `api` | 8000 | Contrato del Anexo: `/v1/prediccion`, `/v1/observaciones`, `/admin/reentrenar`, `/modelo`, `/health` |
| Base del producto | `db` (PostgreSQL 16) | — | Solicitudes, correcciones, predicciones, observaciones, reentrenamientos, histórico |
| MLflow | `mlflow` | 5000 | Experimentos, Model Registry, alias `vigente` |
| Airflow 3 | `airflow-apiserver`, `-scheduler`, `-dag-processor`, `-init`, `airflow-db` | 8080 | DAG `reentrenamiento` (con su propia base PostgreSQL) |
| Arranque | `bootstrap` | — | Carga los datos publicados y entrena el primer modelo (solo la primera vez) |

## Levantar el producto desde cero

Requisitos: Docker con Compose v2 (≥ 8 GB de RAM para Docker; probado en GitHub Codespaces de 4 núcleos / 16 GB).

1. Clonar el tag de entrega:
   ```bash
   git clone --branch entrega-lab2 https://github.com/BrazilBatres/lab2-servicio-pronostico.git
   cd lab2-servicio-pronostico
   ```
2. **Colocar los archivos publicados en la carpeta `datos/`**. Sirve el `.zip` del GES tal cual
   (`datos/datos_lab2.zip`) o su contenido descomprimido (`datos/tiendas.csv`, `datos/historico_ventas.csv`, …,
   también dentro de una subcarpeta). Esta carpeta no se versiona.
3. Crear el archivo de credenciales `.env` (no se versiona). Opción A, con claves aleatorias:
   ```bash
   bash scripts/generar_env.sh
   ```
   Opción B: `cp .env.example .env` y editar los valores.
4. Levantar:
   ```bash
   docker compose up -d --build
   ```
5. Esperar a que `GET /health` responda `"estado": "ok"` (≈ 1.5 min después de levantar, sin contar
   la construcción de imágenes):
   ```bash
   curl -s localhost:8000/health
   ```

Al levantarse desde cero no existe ningún modelo: el servicio `bootstrap` carga los CSV en la base del
producto, entrena el baseline y LightGBM, registra el mejor en MLflow y le asigna el alias `vigente`.
Mientras tanto `/health` responde 503. Al volver a levantar (`docker compose down` / `up`, sin `-v`), el
bootstrap detecta que ya hay histórico y modelo vigente y no repite nada.

Interfaces: API y documentación interactiva en http://localhost:8000/docs · MLflow en http://localhost:5000 ·
Airflow en http://localhost:8080 (usuario y clave en `.env`: `AIRFLOW_USER` / `AIRFLOW_PASSWORD`).

## Uso rápido

```bash
curl -s -X POST localhost:8000/v1/prediccion -H 'Content-Type: application/json' \
  -d '{"tienda_id": "T05", "fecha": "17/11/2025", "familias": [{"familia_id": "LÁCTEOS", "en_promocion": "3"}]}'
curl -s -X POST localhost:8000/v1/observaciones -H 'Content-Type: application/json' \
  -d '{"tienda_id": 5, "fecha": "2025-11-17", "ventas": [{"familia_id": "abarrotes", "ventas": "1,234"}]}'
curl -s -X POST localhost:8000/admin/reentrenar          # -> 202 {"id": "reentreno_..."}
curl -s localhost:8000/admin/reentrenar/<id>              # en_curso | exitoso | fallido
curl -s localhost:8000/modelo
```

## Estructura

```
src/novamercado/
  cleaning.py        defensa del contrato: reparar / advertir / rechazar (funciones puras)
  schemas.py         contrato del Anexo en modelos Pydantic
  features.py        features sin fuga (solo días anteriores a la fecha pronosticada)
  model.py           baseline, LightGBM, cantidad sugerida (newsvendor), métrica de costo, criterio
  registry.py        MLflow: registro de modelos y alias `vigente`
  pipeline.py        reentrenamiento (lo llama el DAG) y bootstrap
  db.py              PostgreSQL del producto (y repositorio en memoria para pruebas)
  service.py         modelo vigente en caliente + copia local, salud de dependencias, pronóstico
  api.py             endpoints FastAPI (solo orquestan)
  airflow_client.py  dispara/consulta el DAG por la API REST de Airflow 3
dags/reentrenamiento.py  DAG: verificar_datos >> entrenar_evaluar_promover
docker/                  Dockerfiles (API/bootstrap, Airflow, MLflow) con versiones fijas
tests/                   pytest (no requiere MLflow ni Postgres en ejecución)
scripts/                 generar_env.sh, simular_operacion.py
evidencia/               resultados de la verificación (ver evidencia/README.md)
```

## Pruebas

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
python -m pytest -q
```

Las pruebas usan datos sintéticos, un repositorio en memoria y MLflow en archivos locales: no
dependen de servidores. La CI (GitHub Actions) corre pytest y valida el `docker-compose.yaml` en cada PR.

## Simulación de la operación

`scripts/simular_operacion.py` imita al programa de evaluación: cada día envía las observaciones del día
anterior y las solicitudes del día (30 % con errores, incluidos tipos no catalogados), reentrena cada
7 días, interrumpe MLflow y, con `--reinicio`, baja y sube el producto sin borrar volúmenes.
**Las ventas "reales" que usa son sintéticas** (mismo día de hace 52 semanas, con ruido y un cambio de
nivel de la demanda), así que sirve para verificar la operación, no para estimar el costo real.

```bash
python3 -u scripts/simular_operacion.py --datos datos --desde 2026-03-01 --dias 28 --reinicio
```

## Decisiones y evidencia

- [DECISIONES.md](DECISIONES.md): políticas ante datos defectuosos, información que usa el modelo,
  cantidad sugerida, criterio de reemplazo y decisiones de arquitectura.
- [evidencia/README.md](evidencia/README.md): experimentos y versiones en MLflow, ejecuciones del DAG,
  ejemplos de respuestas válidas y defectuosas, tiempos medidos.
- [GUIA_DEFENSA.md](GUIA_DEFENSA.md): guía de lectura y preguntas probables.

## Uso de herramientas de IA

Este proyecto se desarrolló con apoyo de **Claude Code** (Anthropic), como permite y recomienda el
enunciado. La IA aparece como coautora en los commits (`Co-Authored-By`) y en los PR. El autor revisó,
ejecutó y verificó el código y las decisiones, y es responsable de poder justificarlos.
