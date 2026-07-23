# Arquitectura — Oblivion Multi-Agent Build Automation

> Documento maestro. Si vas a tocar el código, léelo primero. Captura el **porqué**
> de cada decisión; el código captura el **cómo**. Cuando los dos discrepen, uno de
> los dos está mal — arréglalo, no lo ignores.

---

## 1. Qué estamos construyendo (el objetivo)

Convertir **una reunión con un cliente en cuatro entregables de proyecto** —
wireframe, plan de construcción, Gantt y presupuesto— de forma automática, sin
intervención manual salvo revisión y edición.

No es un solo programa. Es un **equipo de 7 agentes especializados**, cada uno con
un trabajo estrecho, coordinados por un orquestador y vigilados por un juez
compartido. La idea de fondo (del whiteboard original): un agente que hace una cosa
bien es más fácil de razonar, testear y reemplazar que un mono-agente que lo hace
todo regular.

**Volumen real esperado:** 10–25 proyectos al año (dato del propio reporte de
mercado de Oblivion). Esto condiciona TODA decisión de infraestructura: no
diseñamos para "miles de ejecuciones concurrentes", diseñamos para una carga
**bursty y de bajo volumen** (una reunión dispara una ráfaga de ~18 llamadas LLM,
luego silencio hasta la próxima reunión). Sobre-ingeniería aquí es un antipatrón,
no una virtud.

---

## 2. El elenco de agentes

| # | Agente | Trabajo en una línea | Disparado por | Luego dispara |
|---|--------|----------------------|---------------|---------------|
| 1 | **Meeting Notes** | Exporta la nota de Plaud, la clasifica (proyecto + clase) | Timer, 30 min post-reunión | 7 |
| 7 | **Orchestrator** | Decide quién corre y en qué modo | 1 | 2, 3, 4 o 5 |
| 2 | **Wireframe** | Construye un wireframe editable desde las notas | 7 (o inicio de cadena) | 3 |
| 3 | **Planner** | Lista necesidades (SW/HW/cloud) + plan ordenado | 2 o 7 | 4 |
| 4 | **Gantt** | Milestones mensuales + tareas + Gantt | 3 o 7 | 5 |
| 5 | **Budget** | Presupuesto justificado y priceado (.docx) | 4 o 7 | — / vuelve a 7 |
| 6 | **Judge** | Revisa y aprueba cualquier artefacto (compartido) | 2, 3, 4, 5 | vuelve al que lo llamó |

**La cadena de build es lineal y secuencial** — no hay paralelismo real que
explotar, porque cada agente depende del artefacto del anterior: el presupuesto
necesita el Gantt, el Gantt necesita el plan, el plan necesita el wireframe. Lo
constatamos explícitamente para no perseguir un paralelismo inexistente más
adelante.

```
   reunión ──30min──▶ [1 MEETING NOTES] ── export + clasifica (proyecto, clase)
                              │
                              ▼
                      [7 ORCHESTRATOR] ── enruta por clase; modo: create/follow-up/update
                       │    │    │    │
        onboarding →   ▼    ▼    ▼    ▼   ← follow-up/update entran directo al agente dueño
                    [2]──▶[3]──▶[4]──▶[5]
                    wire  plan  gantt budget
                     └─────┴──▶ [6 JUDGE] ◀──┴─────┘  (cada artefacto, máx. 2 rondas)

   edición manual de wireframe → re-corre 3   |   edición manual de Gantt → re-corre 5
```

---

## 3. De dónde sale la data y cómo se transforma (flujo de datos)

Este es el corazón del sistema. Seguir el dato de principio a fin:

### 3.1. Entrada
1. **Google Calendar** — un evento de reunión. De aquí salen: hora de fin
   (dispara el timer de 30 min), lista de asistentes (emails → usados para el
   match determinístico de proyecto), e idioma implícito.
2. **Plaud** — la transcripción de la reunión. **Hoy es export manual** ("Developer
   Platform JSON later", dice el doc original). No hay integración API con Plaud
   todavía: `ingestion.export_plaud_note` es un stub que recibe el transcript ya
   exportado. Cuando exista la API, se cambia solo ese stub sin tocar nada aguas
   abajo.

### 3.2. Transformación (Agente 1)
El transcript crudo + los metadatos del calendar se convierten en un **registro de
reunión clasificado**: `project_id`, `class`, `sub_type`, `language`, y el texto
guardado en `raw_notes`. Ver §4 para el detalle de la clasificación.

### 3.3. Producción de artefactos (Agentes 2–5)
Cada agente lee de Supabase lo que necesita, genera su artefacto, lo pasa por el
Judge, y escribe una **nueva versión** en `artifacts` con `source='agent'`. El
siguiente agente de la cadena lee esa versión y repite. Fuentes por agente:

| Agente | Lee de | Produce (en `artifacts`) |
|--------|--------|--------------------------|
| 2 Wireframe | `raw_notes` + librería de wireframes pasados (few-shot) + foto de pizarra si la hay | `type='wireframe'` (JSON) |
| 3 Planner | `raw_notes` + última versión del wireframe | `type='plan'` (JSON) |
| 4 Gantt | última versión del plan | `type='gantt'` (JSON) + filas en `milestones`/`tasks` |
| 5 Budget | último Gantt + librería de presupuestos pasados + `rate_config` | `type='budget'` (.docx en Storage, link en `file_url`) |

### 3.4. Persistencia
Todo vive en **Supabase (Postgres)**. Ver `supabase/migrations/0001_init_schema.sql`
para el schema completo, comentado tabla por tabla. Dos conexiones distintas a la
misma base:
- **Datos de negocio** → `app/db/client.py` (API PostgREST, service role key).
- **Estado del grafo** (checkpoints de LangGraph) → `app/db/checkpointer.py`
  (conexión directa a Postgres, schema `langgraph` separado).

---

## 4. Clasificación: cómo y con qué criterios (Agente 1)

La clasificación responde **dos preguntas** sobre cada reunión:

### 4.1. ¿A qué proyecto pertenece?
Resolución de entidad en dos etapas, **lo barato primero** (`app/services/classification.py`):

1. **Match determinístico — sin LLM.** Se cruzan los emails de los asistentes del
   calendar contra `projects.attendee_emails`, y el título/notas contra
   `projects.aliases` (+ el nombre del proyecto). Si **exactamente un** proyecto
   matchea → resuelto, confianza 1.0, coste cero, cero no-determinismo.
2. **Fallback LLM — solo si el paso 1 da 0 o >1 candidatos.** GLM-4.7-Flash recibe
   la lista de proyectos activos + el excerpt de la reunión y devuelve una
   clasificación estructurada.

> **Criterio de diseño clave:** nunca auto-creamos un proyecto nuevo desde este
> flujo. Un nombre de cliente mal transcrito creando un proyecto duplicado es peor
> que un item extra en la cola de revisión. Si nada matchea, se sugiere nombre y va
> a revisión humana.

### 4.2. ¿Qué clase de reunión es?
Taxonomía de 4 clases (mutuamente excluyentes en el modelo actual). Estos son los
criterios que el Orchestrator usa después para enrutar:

| Clase | Criterio | Qué provoca |
|-------|----------|-------------|
| **onboarding** | Proyecto nuevo | Cadena completa desde Agente 2 (modo create): 2→3→4→5 |
| **follow_up** | Un artefacto existente necesita revisión (`sub_type` dice cuál) | Salta al agente dueño en modo follow-up, luego re-fluye aguas abajo |
| **update** | Avance de progreso sobre un build vivo | Orchestrator inspecciona el repo, compara vs. plan, y dispara lo que haga falta (normalmente 4) |
| **final_qa** | Etapa de aceptación | ⚠️ Sin agente dueño todavía — ver §9 Preguntas abiertas |

### 4.3. Umbral de confianza
Un resultado solo se auto-aplica si `confidence >= classification_confidence_threshold`
(default 0.70, en `app/config.py`). Por debajo, la reunión queda
`status='pending_review'` — esta es la cola de revisión que el whiteboard original
asumía pero nunca especificó.

---

## 5. Registro de modelos (todos open-weight, vía AWS Bedrock)

**Fuente de verdad autoritativa: `app/config.py` → `MODEL_REGISTRY`.** No dupliques
la tabla completa aquí para que no derive; esta sección explica la *estrategia*, el
código tiene los IDs exactos y el porqué de cada uno.

- **Hosting:** AWS Bedrock **on-demand** (pago por token, serverless, capa Project
  Mantle). Elegido sobre GPU propia (EC2/SageMaker) porque la carga es bursty: una
  GPU reservada estaría ociosa >95% del tiempo. Bedrock cumple la garantía de
  privacidad aceptada (AWS no entrena con tus datos, todo en tu cuenta/región).
- **Estrategia:** especializar. Modelo capaz donde un error se propaga (Planner,
  Judge); modelo barato donde la tarea es mecánica (clasificación, transformación).

| Agente | Modelo | Por qué (resumen) |
|--------|--------|-------------------|
| 1 Meeting Notes | `zai.glm-4.7-flash` | Clasificación trivial; el más barato del catálogo |
| 7 Orchestrator (update) | `minimax.minimax-m2.1` | Solo el resumen de progreso; el routing es código, no LLM |
| 2 Wireframe | `moonshotai.kimi-k2.5` | Visión nativa (fotos de pizarra) + JSON + tool-calling; más barato Y mejor que Qwen3-VL |
| 3 Planner | `deepseek.v3.2` | Razonamiento multi-paso; sus errores cascadean |
| 4 Gantt | `qwen.qwen3-next-80b-a3b-instruct` | Transformación estructurada; barato |
| 5 Budget | `qwen.qwen3-next-80b-a3b-instruct` | Contexto largo para few-shot; aritmética en código, no LLM |
| 6 Judge | `moonshotai.kimi-k2-thinking` | Familia distinta a los builders para no compartir puntos ciegos |

### 5.1. El routing NO es un LLM
Decisión deliberada: mapear clase→agente es una **tabla**, no un juicio. El
Orchestrator lo hace en código determinístico (testeable, gratis, reproducible). El
LLM del Orchestrator solo se usa para el **resumen de progreso en modo update**, que
sí requiere razonamiento sobre código real vs. plan.

### 5.2. Aritmética del presupuesto en código, no en el LLM
El Agente 5 genera las **líneas** (horas, tarifa, justificación) pero las sumas,
contingencia y conversión de moneda se calculan en Python. Un LLM equivocándose en
una multiplicación de un documento que llega al cliente es un riesgo real y
evitable.

### 5.3. Solapamiento Judge/Wireframe (tradeoff aceptado)
El Judge (Kimi K2 Thinking) comparte laboratorio con el Wireframe (Kimi K2.5). La
regla general es "Judge de familia distinta al builder" para no compartir puntos
ciegos. Se acepta el solapamiento **solo aquí** porque el Judge del wireframe evalúa
únicamente **estructura JSON vs. notas**, nunca el render visual — así que el linaje
de visión compartido no se ejercita en esa revisión. Decisión tomada
explícitamente; documentada por si algún día el rol del Judge cambia.

---

## 6. Dos patrones que comparten los agentes builder

### 6.1. El Judge loop
Agentes 2–5 nunca escriben directo al CRM. Cada uno: genera borrador → lo somete al
Judge con las notas fuente y ejemplos → recibe `APPROVE` o feedback accionable →
revisa → reenvía, **máximo 2 rondas**. Tras la ronda 2:
- Si aprobó en algún momento → se escribe la versión aprobada.
- Si nunca aprobó → el artefacto se marca `status='needs_human_review'`, **no** se
  acepta silenciosamente (decisión de ADR — ver §8). El grafo hace `interrupt()` y
  espera intervención humana.

Construido **una sola vez** como helper compartido que llaman los 4 builders — Lean:
no repetir el `while` con contador en cada agente.

Regla clave: **no sobre-alimentar feedback**. Si el borrador ya está bien, aprobar
limpio en vez de inventar cambios.

### 6.2. Modos create / follow-up / update
Cada builder tiene un modo:
- **create** — construye fresco desde las notas (onboarding).
- **follow-up** — carga la última versión guardada, diffea contra lo que piden las
  notas, cambia solo eso.
- **update** — como follow-up, pero el Orchestrator ya inspeccionó el repo y pasa un
  resumen de progreso (build real vs. planeado), para actualizar contra la realidad.

---

## 7. Re-disparo por edición manual

Los artefactos llegan al CRM como editables. Cuando un humano edita uno, el
artefacto aguas abajo queda obsoleto y la cadena debe re-fluir:
- Humano edita wireframe → re-dispara Agente 3 → que re-dispara 4 → 5.
- Humano edita Gantt/tareas → re-dispara Agente 5.

Requiere dos cosas del modelo de datos, ambas ya en el schema:
1. Cada artefacto **versionado**.
2. Cada versión registra `source` (`agent`/`human`). **Solo** una versión
   `source='human'` dispara el re-trigger — un write de agente nunca se dispara a sí
   mismo en loop.

**Transporte del disparo:** Supabase **Database Webhooks** (trigger de Postgres →
POST HTTP a FastAPI). Es push, no polling: no gastamos ciclos preguntando "¿hay algo
nuevo?" ni necesitamos un worker 24/7 vigilando la tabla. Elegido sobre cola
dedicada (Redis/RabbitMQ) porque a este volumen la cola es infraestructura sin
beneficio.

---

## 8. Decisiones de arquitectura (ADR resumido)

| Decisión | Elegido | Alternativas descartadas | Motivo |
|----------|---------|--------------------------|--------|
| Hosting de modelos | Bedrock on-demand | GPU propia (EC2/SageMaker); local | Carga bursty: GPU reservada ociosa >95% |
| Transporte entre agentes | Supabase DB Webhooks | Cola dedicada; polling; llamada directa | Push sin infra nueva, proporcional al volumen |
| Orchestrator routing | Reglas deterministas | Todo-LLM | Reproducible, testeable, gratis |
| Judge tras 2 rondas sin aprobar | `needs_human_review` | Aceptar "mejor versión" en silencio | No mandar al cliente algo que el sistema calificó de insuficiente |
| Aritmética de presupuesto | Código | LLM | Evitar errores de cálculo en doc de cliente |
| Framework de orquestación | LangGraph | Orquestación a mano; Temporal | Judge loop = grafo cíclico nativo; checkpoints + interrupt() gratis |
| Backend | Python (FastAPI) | Node/TS; Go | boto3/langchain-aws maduros; ecosistema LLM |

---

## 9. Preguntas abiertas (bloqueantes marcadas ⚠️)

1. **Herramienta del wireframe** — ¿qué produce técnicamente el Agente 2? Decidido:
   se renderiza en el CRM propio como JSON estructurado; el agente lo persiste vía
   su API. El JSON exacto (schema de pantallas/componentes) aún hay que fijarlo.
2. ⚠️ **Final QA** — Agente 1 puede clasificar "final_qa" pero no hay agente dueño.
   ¿Agente 8 (QA/aceptación) o solo actualizar estado + generar handover docs?
3. **Inspección de progreso (modo update)** — ¿commits/PRs de GitHub, lista de
   issues estilo Axo #54–#82, o un status file del equipo? Stub en
   `app/services/github_progress.py` hasta decidirlo.
4. **Librerías de ejemplos** — ¿dónde viven wireframes/presupuestos pasados y cómo
   se etiquetan los "mejores" para que el few-shot use buenos, no solo recientes?
5. **Verificación de modelos en región** — confirmar que los 7 model IDs están
   habilitados en `AWS_REGION` con `aws bedrock list-foundation-models` y que
   `langchain-aws` habla con ellos vía Converse API. Primer paso técnico real.
6. ~~**Cascada de re-disparo** — ¿total o acotable?~~ **DECIDIDO:** cascada
   completa y automática (editar wireframe regenera plan→Gantt→presupuesto sin
   intervención). Riesgo asumido: puede cambiar en silencio un presupuesto ya
   compartido con el cliente — mitigar operativamente (no editar wireframes de
   proyectos con presupuesto ya enviado sin saberlo). Ver `circuit breaker` como
   mejora futura: un flag por proyecto para pausar la automatización.

---

## 10. Principios de código (Lean Coding)

Cómo se escribe aquí, no negociable:

- **Fuente de verdad única.** Los model IDs viven solo en `MODEL_REGISTRY`. El
  schema vive solo en la migración SQL. No dupliques ninguno en prosa que derive.
- **Bajo acoplamiento.** Cada agente es un nodo que lee/escribe Supabase y no conoce
  la implementación de los demás. Las integraciones externas (Plaud, GitHub) son
  stubs con interfaz estable: se cambia el stub, no el llamador.
- **Falla ruidoso.** `model_id_for` lanza KeyError ante un typo en vez de caer a un
  modelo default en silencio. Idempotencia por constraint de BD, no por chequeos
  dispersos.
- **Sin complejidad especulativa.** Nada de Kafka/K8s/multi-tenant/CQRS a 10–25
  proyectos/año. Se añade cuando el volumen lo justifique, no antes.
- **El porqué se documenta.** Cada decisión no obvia lleva un comentario que remite
  a este doc. Si algo parece raro, probablemente hay una razón — está escrita.
