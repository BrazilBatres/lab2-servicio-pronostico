# Evidencia de funcionamiento

Toda la evidencia proviene de una ejecución **desde cero** (`docker compose down -v` y luego `up -d`) del
código de este repositorio en GitHub Codespaces (4 núcleos, 16 GB), el 2026-11-15, con los datos
publicados en `datos/`. Los JSON se generaron con `scripts/simular_operacion.py` y
`scripts/exportar_evidencia.py`, a partir de las APIs de MLflow y Airflow y de la base del producto.

| Archivo | Qué muestra |
|---|---|
| `mlflow_modelo.json` | Modelo registrado `novamercado_ventas`, sus 5 versiones (etiquetas: costo, motivo, promovido) y el alias `vigente` |
| `mlflow_runs.json` | 15 runs: uno padre por reentreno (params de datos, ventana, criterio) con hijos `baseline` y `lgbm` y sus métricas (MAE, RMSE, WAPE, sesgo, costo) |
| `airflow_dag_runs.json` | 6 ejecuciones del DAG `reentrenamiento` disparadas por `POST /admin/reentrenar`, con el estado de cada tarea (una `failed`: MLflow detenido) |
| `base_de_datos.json` | Conteos de solicitudes, correcciones, predicciones por versión del modelo, observaciones y la tabla de reentrenamientos |
| `ejemplos_catalogo_ges.json` | Respuestas de la API a los 12 ejemplos publicados (E01–E10 y válidos); E09 se envía dos veces |
| `simulacion/ejemplos_respuestas.json` | Solicitudes válidas y defectuosas (incluidos errores no catalogados) con su respuesta |
| `simulacion/resumen.json`, `simulacion.log` | 28 días simulados: reentrenos, caída de MLflow, reinicio y tiempos |
| `simulacion_extra/` | 9 días adicionales con un reentreno más |

## Tiempos medidos frente a los límites del enunciado

| Requisito | Medido | Límite |
|---|---|---|
| Producto listo (`/health` = ok) desde cero, sin contar la construcción | 65 s | ≤ 10 min |
| Producto listo tras `down`/`up` sin borrar volúmenes (no reentrena) | 30 s; mismo modelo vigente | conservar lo que tiene |
| Duración de cada reentrenamiento (DAG completo, vista desde la API) | 17–27 s | ≤ 10 min |
| Uso del modelo promovido sin reinicio | 2–16 s | ≤ 2 min |
| `/health` pasa a `degradado` al detener MLflow | 4 s | < 1 min |
| `/health` vuelve a `ok` al regresar MLflow | 20 s | < 2 min |
| Predicción durante la caída de MLflow | HTTP 200 con el modelo cargado | seguir respondiendo |
| Reentreno durante la caída de MLflow | `fallido` en 10 s; vigente sin cambios | no afectar al vigente |
| Reenvío idéntico de observaciones | 23 de 23 con `registros_guardados = 0` | idempotente |

## Historial de reentrenamientos (tabla `reentrenamientos`)

| id | Resultado | Versión vigente al terminar | Motivo |
|---|---|---|---|
| bootstrap | exitoso, promovido | 1 | Sin vigente; costo del candidato 88 028 (baseline 105 548) |
| reentreno_1b569b… | exitoso, no promovido | 1 | Solo 6 días nuevos desde el entrenamiento del vigente (mínimo 7) |
| reentreno_e0c426… | **fallido** | 1 | MLflow detenido; el vigente no cambió |
| reentreno_af8dab… | exitoso, promovido | 2 | 49 502 vs 49 983 del vigente |
| reentreno_c6a150… | exitoso, promovido | 3 | 28 607 vs 38 489 del vigente |
| reentreno_2db464… | exitoso, promovido | 4 | 29 728 vs 30 742 del vigente |
| reentreno_0db03d… | exitoso, promovido | 5 | 36 721 vs 39 462 del vigente |

Un candidato **rechazado por el criterio** se observó en la primera ejecución del DAG (antes del
reinicio desde cero): 88 028 vs 81 232 del vigente. Eso motivó la corrección descrita en DECISIONES §4. El
rechazo también está cubierto por `tests/test_model.py::test_criterio_de_reemplazo`. En esta simulación,
la demanda sube 30 % a partir del día 10, así que cada modelo nuevo sí fue mejor que el anterior.

## Política aplicada a los ejemplos del GES (`ejemplos_catalogo_ges.json`)

| Ejemplo | HTTP | `calidad.estado` |
|---|---|---|
| prediccion_valida | 200 | ok |
| E01 tienda texto | 200 | corregido |
| E02 familia otra forma | 200 | corregido |
| E03 promoción texto | 200 | corregido |
| E04 fecha formato | 200 | corregido |
| E05 faltan familias | 200 | advertencia |
| E06 familia repetida | 200 | advertencia |
| E07 campo renombrado | 200 | corregido |
| E08 ventas negativas | 200 | advertencia (no se guarda el registro negativo) |
| E09 envío repetido | 200 / 200 | ok; el segundo envío da `registros_guardados = 0` |
| E10 número con formato | 200 | corregido |

**Nota**: las ventas reales de la simulación son sintéticas (mismo día de hace 52 semanas, con ruido y
un aumento de 30 % de la demanda desde el día 10). Los costos de la simulación verifican la operación;
no estiman el costo en el periodo real de evaluación.
