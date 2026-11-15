# Guía de lectura y defensa

## Orden de lectura recomendado (≈ 45 min)

1. **`README.md`** (5 min): qué servicios hay y cómo se levanta.
2. **`DECISIONES.md` §1–4** (15 min): son las cuatro decisiones que el enunciado pide justificar.
3. **`src/novamercado/cleaning.py`** (10 min): leer `clean_prediccion` de arriba a abajo. Cada `Nota`
   es una línea de `calidad.observaciones`.
4. **`src/novamercado/pipeline.py` → `retrain`** (5 min): el ciclo completo. Ubicar la única línea que
   mueve el alias (`registry.set_vigente`).
5. **`src/novamercado/service.py` → `ModelManager.refresh`** (5 min): cómo cambia el modelo sin reinicio
   y cómo sobrevive a MLflow caído.
6. **`src/novamercado/features.py` → `_series_stats`** (5 min): todo usa `.shift(1)`; por eso no hay fuga.

## Recorrido de una solicitud (para explicarlo en la pizarra)

`POST /v1/prediccion` → `api.prediccion` lee el JSON crudo → `cleaning.clean_prediccion` repara, advierte
o lanza `Rechazo` (422) → `PrediccionRequest` (Pydantic) valida el resultado limpio → `service.forecast`
pide a Postgres las ventas **anteriores** a la fecha → `features.build_features` → `ForecastModel.predict_frame`
calcula venta esperada × k(familia) → se guardan la solicitud, las correcciones y las predicciones con la
versión del modelo → la respuesta se construye con `PrediccionResponse`.

`POST /admin/reentrenar` → se crea la fila en `reentrenamientos` → la API dispara el DAG por la API REST
de Airflow con `dag_run_id = id` → el DAG llama `pipeline.retrain(id)` → MLflow registra los runs y la
versión, y mueve el alias si corresponde → se actualiza la fila → la API detecta el alias nuevo en ≤ 20 s.

## Preguntas probables y respuestas

**1. ¿Cómo decide el servicio entre reparar, advertir y rechazar?**
Reparar si la corrección es inequívoca (el mismo dato escrito de otra forma: `"T05"`, `"LÁCTEOS"`, `"1,234"`).
Advertir si hubo que suponer o descartar algo (una fecha ambigua, una familia parecida o desconocida,
familias faltantes, ventas negativas que no se guardan). Rechazar si falta la tienda, la fecha o la lista,
o si no se pueden interpretar sin inventar.

**2. ¿Cómo maneja errores que no están en el catálogo?**
Las reglas son por *tipo de dato*, no por ejemplo. Los nombres de campo se normalizan y comparan por
alias y similitud; las familias, contra el id y el nombre del catálogo; los números, con una convención
de separadores; las fechas, con formatos inequívocos. Las pruebas incluyen casos inventados, como
`tienda_idd`, `FRUTAS-VERDURAS`, `{"abarrotes": 3}` o campos extra, y el simulador envía errores no
catalogados.

**3. ¿Por qué no rellenan las familias faltantes (E05)?**
Para hacerlo habría que inventar su promoción del día. El enunciado pide no inventar datos, así que se
pronostica lo pedido y se advierte qué faltó.

**4. ¿Qué pasa con una venta negativa (E08)?**
No se guarda ese registro, y se advierte. Recortarla a 0 inventaría una venta de 0. El modelo tolera el
hueco, porque sus ventanas usan solo lo disponible.

**5. ¿Cómo garantizan la idempotencia de observaciones?**
Hay una llave primaria (tienda, familia, fecha) y se usa `INSERT … ON CONFLICT DO UPDATE … WHERE ventas
IS DISTINCT FROM EXCLUDED.ventas RETURNING 1`. Solo devuelve fila si insertó o cambió algo, así que
`registros_guardados` cuenta solo lo nuevo o modificado: un reenvío idéntico da 0.

**6. ¿Cómo evitan usar información del futuro?**
Todas las ventanas están desplazadas al menos un día (`shift(1)`), y la consulta a Postgres filtra
`fecha < F`. Hay una prueba que cambia las ventas futuras y verifica que las features no cambian. Las
promociones del día salen de la solicitud, que el enunciado permite.

**7. ¿Por qué el modelo predice una razón y no unidades?**
Porque el periodo de evaluación puede traer cambios de demanda. Al predecir la razón ventas / nivel, el
nivel se actualiza con cada observación diaria y el pronóstico se ajusta solo, sin reentrenar. Un árbol
que predice unidades no puede extrapolar fuera de los niveles que vio.

**8. ¿Por qué la cantidad sugerida no es igual a la venta esperada?**
Porque los costos son asimétricos. Es el problema del *newsvendor*: el óptimo es el cuantil
`c_f/(c_f+c_s)` de la demanda. En licores faltar cuesta 20 veces más que sobrar (q = 0.95, se pide
alrededor de 49 % más); en panadería sobrar cuesta más (q = 0.29, se pide alrededor de 17 % menos).
Usamos el cuantil empírico de real/esperado en validación, sin suponer una distribución.

**9. ¿Cuál es el baseline y cuánto mejora el modelo?**
El baseline es la media de los últimos 4 mismos días de la semana. LightGBM, en la validación de
febrero de 2026, baja el costo de negocio de 105 548 a 88 028 (−17 %) y el WAPE de 21.5 % a 17.8 %.
Ambos quedan registrados en MLflow como runs hijos de cada reentreno.

**10. ¿Cuál es el criterio de reemplazo y por qué es justo?**
El candidato reemplaza al vigente si tiene menor costo en la misma ventana de días que el vigente no
vio al entrenarse (como mínimo 7 y como máximo 28). La primera versión comparaba en días que el vigente
ya conocía, y eso lo favorecía (81 232 frente a 88 028). Lo detectamos en la primera ejecución real del
DAG y lo corregimos.

**11. ¿Qué pasa si el reentrenamiento falla?**
El alias se mueve en un solo punto, al final. Cualquier error antes de eso (incluido MLflow caído, que
se verifica al inicio para fallar rápido) deja el vigente intacto y registra `fallido` con el motivo.
Hay una prueba automatizada y lo verificamos con MLflow detenido.

**12. ¿Cómo empieza a usarse un modelo nuevo sin reiniciar?**
Un hilo de la API consulta el alias cada 20 s. Si apunta a otra versión, descarga el modelo y lo
reemplaza en memoria de forma atómica. Medimos el tiempo hasta usarlo en la simulación; ver `evidencia/`.

**13. ¿Qué pasa si MLflow se cae?**
La API sigue respondiendo con el modelo en memoria y con una copia local en un volumen, que le sirve
incluso si se reinicia mientras MLflow está caído. `/health` pasa a `degradado` en ≤ 10 s, porque se
revisa cada 10 s, y vuelve a `ok` cuando MLflow regresa.

**14. ¿Por qué no usan el reloj del sistema?**
Porque el evaluador simula fechas. La validez de una fecha depende solo del histórico, la apertura de
la tienda y el calendario de feriados.

**15. ¿Qué hace el servicio `bootstrap`?**
Corre una sola vez por arranque. Crea el esquema, carga los CSV si el histórico está vacío y entrena
solo si no existe el alias `vigente`. Por eso desde cero entrena y, tras un reinicio, no repite nada.

**16. ¿Por qué Airflow y no un hilo en la API?**
Porque separa servir de entrenar, deja historial y estado de cada ejecución, aplica timeouts y permite
consultar el estado. Además, el enunciado lo pide. El DAG solo orquesta; la lógica está en el paquete
y tiene pruebas.

**17. ¿Cómo se relacionan el `id` de la API y la ejecución de Airflow?**
Son el mismo: la API usa su `id` como `dag_run_id`. El estado se lee de Airflow mientras corre, y el
resultado de la tabla `reentrenamientos` al terminar.

**18. ¿Qué limitaciones tiene?**
Están en DECISIONES §6. Las principales: la simulación usa ventas sintéticas, no se pronostican familias
desconocidas y una fecha ambigua se resuelve con la convención día/mes.

**19. ¿Qué hizo la IA y qué hiciste tú?**
Claude Code propuso y escribió el código bajo mi dirección: interpretación del enunciado, decisiones de
política, revisión y ejecución de pruebas y simulaciones. Las decisiones de producto las aprobé yo
(política de datos, criterio de reemplazo, entorno). La coautoría está en cada commit y PR.

## Comandos útiles para la demo

```bash
docker compose ps
curl -s localhost:8000/health | python3 -m json.tool
curl -s localhost:8000/modelo
docker compose stop mlflow; sleep 15; curl -s localhost:8000/health; docker compose start mlflow
docker compose exec db psql -U novamercado -d novamercado -c "select * from reentrenamientos"
```
