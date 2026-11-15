# Reglas del proyecto (contexto para agentes de programación)

Producto: servicio de pronóstico diario de ventas por tienda y familia (NovaMercado) con FastAPI,
PostgreSQL, MLflow y Airflow 3, todo en Docker Compose. Leer `README.md` y `DECISIONES.md` antes de
cambiar comportamiento. El contrato HTTP del Anexo del enunciado es inmutable: se pueden añadir
campos, nunca quitar ni renombrar.

## Principios no negociables
1. **Sin fuga**: al pronosticar la fecha F solo se usan ventas con fecha < F (`features.build_features`
   desplaza todas las ventanas al menos un día; prueba `test_features_no_usan_el_futuro`).
2. **Nunca el reloj del sistema** para validar fechas de negocio (las fechas son simuladas).
3. **Defensa del contrato en `cleaning.py`** (funciones puras con pruebas), no en los endpoints.
   Toda regla nueva debe ser general y quedar documentada en DECISIONES.md §1.
   Reparar = inequívoco; advertir = hubo supuesto o se descartó un dato; rechazar (422) = falta lo indispensable.
4. **No inventar datos**: una observación inválida no se guarda; no se imputan ventas.
5. **Observaciones idempotentes** (upsert por tienda, familia, fecha; un reenvío idéntico cuenta 0).
6. **El alias `vigente` solo lo mueve `pipeline.retrain`**, al final y si el candidato supera el criterio.
7. **Los DAGs solo orquestan**; la lógica vive en `src/novamercado/`.
8. Datos publicados y credenciales nunca en Git ni en las imágenes (`datos/` y `.env` ignorados).

## Convenciones
- Código y documentación en español; nombres de funciones en inglés corto.
- Antes de un PR: `python -m pytest -q` (no requiere MLflow ni Postgres). Trabajar en ramas.
- Commits y PRs con coautoría del asistente de IA (`Co-Authored-By`).
