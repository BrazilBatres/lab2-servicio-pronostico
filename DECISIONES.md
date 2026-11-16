# Decisiones técnicas y de producto

Cada decisión indica alternativas consideradas y su justificación. Las cuatro que pide el enunciado
están en las secciones 1 a 4.

## 1. Política ante cada tipo de problema en los datos

**Principio general** (se aplica también a problemas que no están en el catálogo):

| Acción | Cuándo | Efecto |
|---|---|---|
| **Reparar** | La corrección es inequívoca: el dato significa lo mismo, escrito de otra forma. | Se responde; `calidad.estado = corregido`. |
| **Advertir** | Hubo que suponer algo, elegir entre valores en conflicto, o descartar un dato (sin inventarlo). | Se responde; `calidad.estado = advertencia`. |
| **Rechazar (422)** | Falta información indispensable (tienda, fecha, lista) o no es interpretable sin inventar. | `{"detail": motivo}`. |

Cada acción queda en `calidad.observaciones` (campo con la ruta exacta, p. ej. `familias[2].familia_id`)
y en la tabla `correcciones`. Las reglas viven en `src/novamercado/cleaning.py` y están cubiertas por
`tests/test_cleaning.py`.

**Reglas generales** (no una por ejemplo):

- *Nombres de campo*: se normalizan (minúsculas, sin tildes ni símbolos) y se comparan con el nombre del
  contrato, con alias conocidos (`tienda`, `id_tienda`, `store`…) y por similitud alta (≥ 0.85). Si coincide
  → **reparar**. Campo desconocido → se ignora y **advertir**. Si un alias choca con el campo oficial, gana el oficial.
- *Identificadores de tienda*: número, texto numérico, prefijos (`T05`, `tienda-5`), `5.0` o el nombre de
  la tienda → **reparar**. Tienda inexistente o ininteligible → **rechazar** (no se puede pronosticar
  para una tienda que no se conoce).
- *Familias*: comparación normalizada contra id y nombre del catálogo, ignorando tildes, mayúsculas,
  separadores y palabras como "y"/"de" → **reparar**. Parecido no exacto (error de dedo) → se toma la
  familia más parecida y **advertir**. Familia desconocida → se ignora y **advertir**.
- *Números* (promociones y ventas): texto numérico y enteros escritos como decimales → **reparar**.
  Convención local: coma seguida de grupos de 3 dígitos = miles (`"1,234"`); otra coma = decimal (`"812,0"`).
  Decimal no entero → se redondea y **advertir**.
- *Fechas*: AAAA-MM-DD con hora, `/` o sin separadores; `dd/mm/aaaa` inequívoco (día > 12) o `mm/dd/aaaa`
  inequívoco → **reparar**. `dd/mm` ambiguo (ambos ≤ 12) → se asume día/mes (convención guatemalteca) y
  **advertir**. Fecha inexistente o ilegible → **rechazar**. Nunca se usa el reloj del sistema.
- *Rangos de fecha*: anterior al inicio del histórico o a la apertura de la tienda → **rechazar**.
  Posterior al calendario de feriados → se responde asumiendo día sin feriado y **advertir**.
- *Lista como objeto* `{"abarrotes": 3}` → se convierte a lista y **reparar**. Elemento que no es objeto o
  no trae familia → se ignora y **advertir**. Lista vacía o sin elementos válidos → **rechazar**.

**Catálogo del GES**:

| Código | Problema | Política |
|---|---|---|
| E01 | Tienda como texto o con prefijo | Reparar (inexistente → rechazar) |
| E02 | Familia escrita de otra forma | Reparar (parecida → advertir; desconocida → advertir y omitir) |
| E03 | Promoción como texto o decimal | Reparar `"3"`, `2.0`; no entero → advertir y redondear; ilegible/negativa/ausente → advertir y asumir 0 |
| E04 | Fecha en otro formato | Reparar si es inequívoca; ambigua → advertir; ilegible → rechazar |
| E05 | Faltan familias | Advertir: se pronostican solo las recibidas y se listan las faltantes |
| E06 | Familia repetida | Valores iguales → reparar (una vez); distintos → advertir y usar la primera aparición |
| E07 | Nombre de campo distinto | Reparar (alias/similitud); campo indispensable ausente → rechazar |
| E08 | Ventas negativas | Advertir y **no guardar** ese registro |
| E09 | Envío repetido | Idempotente: idéntico → `registros_guardados = 0`, estado ok; valor distinto → se reemplaza (corrección), cuenta como modificado y advertir |
| E10 | Separador de miles o coma decimal | Reparar |

Alternativas consideradas:
- *Rechazar todo lo que no cumpla el contrato*: es simple, pero contradice el objetivo ("un servicio que no se detenga").
- *E05: completar las familias faltantes con promoción 0*: daría más información para el inventario,
  pero inventa la promoción. Elegimos responder solo lo pedido y advertir.
- *E08: recortar a 0*: inventa una venta. Descartar el registro no inventa nada y el modelo tolera huecos (§2).
- *E06: usar el máximo o el último valor*: no hay razón para preferirlos. La primera aparición es
  determinista, y la advertencia deja constancia.

## 2. Información que usa el modelo al predecir

Para la tienda T y la fecha F, **solo ventas de fechas < F**: el histórico más las observaciones
registradas (si una observación coincide con un día del histórico, gana la observación). Las features son:

- **Nivel reciente**: media de las últimas 28 observaciones disponibles de la serie tienda×familia.
- Relativas al nivel: venta de ayer, de hace 7 días, media de 7 días y media de los últimos 4 mismos días
  de la semana. Además, los días transcurridos desde la última venta conocida.
- **Promoción del día** (de la solicitud) y su relación con la promoción media reciente. Las observaciones
  no traen promociones; para los días observados se usa la promoción enviada en la solicitud de pronóstico
  de ese día (queda registrada en `predicciones`).
- Calendario: día de la semana, día del mes, fin de mes, quincena, mes.
- Feriados de `feriados.csv` por tipo (feriado, comercial, pago, medio día), aplicables si su `ambito`
  es `nacional` o la ciudad de la tienda; también si mañana o ayer es feriado.
- Tienda, familia y formato de la tienda (categóricas).

El modelo predice la **razón ventas / nivel**, no las unidades. *Por qué*: el periodo de evaluación puede
traer cambios de demanda. Con la razón, el pronóstico sube o baja junto con las observaciones que llegan
cada día, sin esperar un reentrenamiento, y un árbol no queda limitado a los niveles que vio al entrenar.
*Alternativa*: predecir unidades directamente; con un cambio de nivel, el árbol tiende a quedarse en los
valores del histórico.

Si faltan días, las ventanas usan lo disponible y la respuesta advierte cuando la última venta conocida
tiene más de 7 días. Sin ninguna venta previa, se usa la mediana de la familia y se advierte.
Una prueba (`test_features_no_usan_el_futuro`) altera las ventas futuras y verifica que las features no cambian.

## 3. Elección de la cantidad sugerida

Es el **problema del vendedor de periódicos (newsvendor)**: si faltar cuesta `c_f` y sobrar `c_s` por
unidad, la cantidad que minimiza el costo esperado es el cuantil `q = c_f / (c_f + c_s)` de la demanda.

`cantidad_sugerida = ⌈venta_esperada × k_familia⌉`, donde `k_familia` es el cuantil `q` de la razón
real/esperado en la validación del modelo. Por ejemplo:

| Familia | c_f | c_s | q | k (modelo inicial) |
|---|---|---|---|---|
| licores | 2.0 | 0.1 | 0.95 | ≈ 1.49 |
| limpieza | 1.0 | 0.1 | 0.91 | ≈ 1.31 |
| abarrotes | 1.0 | 0.2 | 0.83 | ≈ 1.21 |
| lacteos | 1.0 | 1.2 | 0.45 | ≈ 0.96 |
| panaderia | 1.0 | 2.5 | 0.29 | ≈ 0.83 |

Así la sugerida queda por encima de lo esperado cuando faltar es caro, y por debajo cuando sobrar es caro.
*Alternativas*: (a) `venta_esperada` sin ajuste, que ignora la asimetría de costos; (b) un modelo de
regresión cuantílica por familia, más fino pero 12 veces más entrenamiento; (c) suponer una distribución
normal o de Poisson. El cuantil empírico no supone ninguna forma y es fácil de explicar.
Se usa una razón multiplicativa porque el error crece con el volumen.

**Métricas** (en MLflow): técnicas (MAE, RMSE, WAPE, sesgo) y la de negocio, `costo = Σ c_f·max(real−sug,0) + c_s·max(sug−real,0)`.

## 4. Criterio de reemplazo del modelo

En cada reentrenamiento:
1. Se toman los días con ventas **posteriores a la última fecha con la que se entrenó el vigente**
   (`entrenado_hasta`), hasta 28. Si hay menos de 7 (`MIN_DIAS_NUEVOS`), no hay evidencia nueva: se
   conserva el vigente sin entrenar ("exitoso", `promovido: false`).
2. Se entrenan el baseline y LightGBM con los datos anteriores a esa ventana y se evalúan en ella. El
   candidato es el de menor costo.
3. El vigente se evalúa en **la misma ventana**, con las mismas features (solo pasado de cada día).
4. **Se promueve si `costo_candidato < costo_vigente`.** El motivo queda en la respuesta, por ejemplo
   `"Costo en validación 18 210 vs 19 940 del vigente"`.
5. El candidato se reentrena con todos los datos y se registra en el Model Registry siempre, con etiquetas
   de costo, motivo y si fue promovido. Solo si se promueve se mueve el alias `vigente`.

*Por qué solo días que el vigente no vio*: la primera versión comparaba en los últimos 28 días, que el
vigente ya había visto al entrenarse. La comparación favorecía al vigente: en la prueba quedó 81 232
frente a 88 028 del candidato, y con reentrenos periódicos casi nunca se habría promovido un modelo.
Lo detectamos en la primera ejecución del DAG y lo corregimos (ver historial de commits).
*Alternativas*: exigir un margen mínimo de mejora (más estable, pero tarda en reaccionar a cambios de
demanda); comparar con MAE (no es lo que importa al negocio).

**Garantías**: el alias se mueve en un solo punto, al final de `pipeline.retrain`. Cualquier excepción
anterior (MLflow caído, datos, entrenamiento) deja el vigente intacto y registra `fallido` con el motivo
(prueba `test_fallo_no_afecta_al_vigente`; también se verificó con MLflow detenido).

## 5. Arquitectura y operación

**D1. El reentrenamiento corre dentro de Airflow** (`PythonOperator` vía TaskFlow) con el código del
paquete instalado en la imagen. El DAG solo orquesta: `verificar_datos >> entrenar_evaluar_promover`.
- La API dispara el DAG por la API REST de Airflow 3 (token JWT con usuario y clave de `.env`), usando
  como `dag_run_id` el mismo `id` que devuelve `POST /admin/reentrenar`. Así el estado se consulta en
  Airflow y el resultado (promovido, versión, motivo) en la tabla `reentrenamientos`.
- `max_active_runs=1`: dos reentrenos simultáneos no compiten por el alias. `dagrun_timeout=10 min`.
- *Alternativa*: entrenar en la API con un hilo. Mezclaría servir con entrenar y no usaría Airflow como pide el enunciado.

**D2. Cambio de modelo sin reinicio.** La API consulta el alias en MLflow cada 20 s
(`MODEL_POLL_SECONDS`). Si cambió, descarga la nueva versión y la reemplaza en memoria de forma atómica.
El tiempo hasta usar un modelo promovido es ≤ 20 s más la descarga, dentro del límite de 2 minutos.
*Alternativa*: que el DAG avise a la API con un webhook. Es más rápido, pero añade acoplamiento y falla
si la API no está disponible en ese momento.

**D3. Resiliencia a MLflow.** La API guarda una copia local del modelo vigente (`pickle` en el volumen
`api-cache`) y, al arrancar, carga primero esa copia. Si MLflow no responde, sigue sirviendo, incluso
después de un reinicio. Un hilo revisa la base de datos, MLflow, Airflow y el modelo cada 10 s, y
`/health` refleja `degradado` con su `motivo` en ≤ 10 s y vuelve a `ok` en ≤ 30 s.

**D4. Persistencia.** PostgreSQL propia (`db`), separada de `airflow-db`. Las tablas son `solicitudes`
(incluye rechazos), `correcciones`, `predicciones` (con `version_modelo`), `observaciones` (llave primaria
tienda+familia+fecha; `INSERT … ON CONFLICT DO UPDATE … WHERE valor distinto RETURNING`, que cuenta solo
los registros nuevos o modificados), `reentrenamientos`, `ventas_historicas` y `catalogos`. Todo vive en
volúmenes con nombre, que sobreviven a `down`/`up`.

**D5. Bootstrap idempotente.** Es un servicio `bootstrap` de una sola ejecución: crea el esquema, carga
los CSV si la tabla del histórico está vacía y entrena solo si no existe el alias `vigente`. Acepta el
`.zip` o los CSV sueltos en `datos/`.

**D6. MLflow** 3.18 con backend SQLite y artefactos en el volumen `mlflow-data`, servidos por el propio
servidor (`--serve-artifacts`). Los clientes no necesitan acceso al disco. El modelo se registra como
`pyfunc` (envoltorio de `ForecastModel`) con su linaje: run padre del reentreno con params de datos,
runs hijos `baseline` y `lgbm` con sus métricas, y la versión etiquetada con costo, motivo y promoción.
*Alternativa*: PostgreSQL como backend de MLflow; sería una tercera base que no hace falta para un nodo.

**D7. Imágenes con versión fija** (`python:3.12.8-slim`, `apache/airflow:3.1.0-python3.12`,
`ghcr.io/mlflow/mlflow:v3.18.0`, `postgres:16.6-alpine`) y dependencias fijadas con `==`. Los datos se
montan en tiempo de ejecución (`./datos:/datos:ro`) y las credenciales vienen de `.env`; ninguno de los
dos entra a las imágenes (`.dockerignore`) ni al repositorio (`.gitignore`).

**D8. Airflow 3 con LocalExecutor** y su propia PostgreSQL, sin Celery ni Redis. Basta para un nodo y
reduce la memoria y el tiempo de arranque.

**D9. Sin DVC.** Los datos los publica el equipo docente y se colocan en `datos/`. No producimos datos
derivados que haya que versionar: el estado operativo vive en PostgreSQL y MLflow.

## 6. Limitaciones conocidas

- El simulador usa ventas reales **sintéticas**, así que no estima el costo en el periodo real de evaluación.
- Parte del criterio de reemplazo depende de la ventana de días nuevos. Con muy pocos días entre
  reentrenos (< 7) no se promueve nada, lo cual es intencional.
- Si una solicitud trae una familia nueva para el catálogo, se ignora (con advertencia): el servicio no
  pronostica familias que no conoce.
- La fecha `dd/mm` ambigua se resuelve con la convención local y queda advertida; si una tienda usa
  `mm/dd`, solo se detecta cuando el día es mayor que 12.
