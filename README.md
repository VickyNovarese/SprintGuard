# SprintGuard — QA & Delivery Intelligence

SprintGuard 1.8.1 analiza un sprint de Jira y transforma sus historias, pruebas, ejecuciones, bugs y evidencias en información accionable para QA, Project y Delivery Management.

## Qué hace

- Lee los elementos de un sprint mediante Jira REST API.
- Agrupa historias, bugs y casos de prueba.
- Calcula puntos por tipo y distribución de estados.
- Detecta historias sin casos o con menos de tres casos.
- Comienza en modo seguro de solo lectura.
- Incluye un dashboard visual en la página principal.
- Calcula progreso de ejecución, pass rate, bugs por prioridad y GO/NO-GO.
- Descubre automáticamente los IDs de `Execution Status` y story points en cada sitio Jira.
- Cuenta vínculos entre historias y casos de prueba en ambas direcciones.
- Genera un informe ejecutivo del sprint con avance por puntos, bugs abiertos y acciones recomendadas.
- Permite imprimir el informe o guardarlo como PDF desde el navegador.
- Mantiene una interfaz enfocada en el análisis del sprint, sin botones ni dependencia de OpenAI.
- Interpreta To Do como pendiente, In Progress como desarrollo, Review como testing QA y Done + Passed como aprobado.
- Interpreta Failed como una columna explícita para casos rechazados por QA y pendientes de corrección.
- Solo recomienda GO cuando todos los Test Cases están Done + Passed y no quedan riesgos bloqueantes.
- No muestra una tarjeta de inconsistencias; cualquier Done sin Passed aparece únicamente como riesgo de workflow.
- Separa Ready for QA (desarrollo terminado y esperando QA) de Review (testing activo).
- Incorpora Ready for UAT como validación en preproducción antes de Done.
- Separa métricas de historias, Test Cases, bugs abiertos y bugs resueltos.
- Calcula el pass rate final como Done + Passed dividido por el total de Test Cases.
- Las tarjetas del workflow incluyen Bugs y Test Cases, con desglose por tipo.
- Reconoce Review e In Review como testing activo.
- Añade alertas operativas por antigüedad para Ready for QA, Failed y Ready for UAT.
- Muestra bugs Highest/Critical abiertos y cobertura insuficiente como alertas accionables.
- Incluye enlaces directos a cada elemento de Jira y hora de última sincronización.
- Calcula carga para 5 desarrolladores y 2 QA, además del reparto actual por responsable.
- Proyecta la probabilidad de terminar el sprint comparando avance por puntos y tiempo transcurrido.
- Detecta automáticamente el campo `QA Assigned` (también acepta `QA Assignee` o `QA Owner`).
- Mantiene `Assignee` como desarrollador y muestra por separado el QA asignado.
- Calcula la carga de trabajo individual de cada QA y alerta sobre elementos listos para probar que no tienen QA.
- Separa los elementos activos bajo responsabilidad de desarrollo de la cola que todavía requiere trabajo de desarrollo.
- Incluye Test Execution aunque no estén cargadas en el sprint y las relaciona con el Test Case por su referencia PROJ.
- Muestra resultados, ambiente, QA Assigned y cantidad de evidencias adjuntas por ejecución.
- Detecta Test Cases sin ejecución y ejecuciones Failed sin evidencia o sin Bug vinculado.
- Incorpora el último resultado de ejecución a la decisión GO/NO-GO.
- Añade accesos configurables al board de Jira y al repositorio de GitHub.
- Está preparada para ejecutarse localmente o publicarse como demo de solo lectura en Vercel.
- Muestra por separado el estado del workflow de cada Test Execution y su resultado de ejecución.
- En Test Execution utiliza `Assignee` como QA ejecutor; `QA Assigned` se conserva solo para Test Cases y Bugs.
- Lee y presenta el objetivo definido en el sprint de Jira.
- Calcula una puntuación de salud del sprint combinando pronóstico, alertas, bugs y cobertura.
- Ordena las cinco acciones más importantes del día según criticidad y antigüedad.
- Resume el tiempo promedio y el elemento más antiguo en cada etapa del workflow.
- Expone dependencias y bloqueos registrados mediante vínculos de Jira.
- Muestra carga individual por persona, rol, elementos activos, story points y distribución por estado.
- Enriquece el informe ejecutivo con objetivo, salud, riesgos y recomendaciones para stakeholders.
- El botón `Actualizar desde Jira` fuerza una consulta nueva sin recargar la página ni reutilizar datos en caché.
- La cobertura muestra por separado el estado real de la historia en Jira y su preparación calculada para el cierre.
- Separa estrictamente las ejecuciones `Failed`, `Blocked` y `Not Run`, sin doble conteo.
- Muestra las ejecuciones que requieren atención con clave, Test Case, estado Jira, QA y enlace directo.
- Incluye un organizador controlado para distribuir Sprint 2-10 con 3 historias y 3 Test Cases vinculados por historia.
- Las métricas de ejecución incluyen únicamente Test Executions vinculadas a Test Cases del sprint seleccionado; los resultados históricos de otros sprints quedan excluidos.

## Configuración

1. Descarga o clona el repositorio.
2. Copia `.env.example` como `.env`.
3. Configura la URL, email, API token, proyecto, board y repositorio.
4. Mantén `AGENT_WRITE_MODE=false` para utilizar la aplicación en modo de solo lectura.
5. Instala y ejecuta:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload
```

En Windows CMD:

```cmd
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app:app --reload
```

Ejemplo de configuración:

```env
JIRA_BASE_URL=https://your-site.atlassian.net
JIRA_EMAIL=your-email@example.com
JIRA_API_TOKEN=your-token
JIRA_PROJECT_KEY=PROJ
JIRA_BOARD_URL=https://your-site.atlassian.net/jira/software/projects/PROJ/boards/1
GITHUB_REPOSITORY_URL=https://github.com/your-user/sprintguard
AGENT_WRITE_MODE=false
```

## Uso

```bash
curl -X POST http://127.0.0.1:8000/analyze-sprint \
  -H 'Content-Type: application/json' \
  -d '{"sprint_name":"PROJ Sprint 1"}'
```

Dashboard: `http://127.0.0.1:8000/`

Documentación interactiva: `http://127.0.0.1:8000/docs`.

## Organizar los próximos sprints

El comando utiliza las mismas credenciales de `.env`. Primero presenta el plan y no escribe nada hasta que confirmes con la palabra `ORGANIZAR`:

```powershell
python organize_sprints.py
```

El organizador:

- Conserva intacto el Sprint 1 finalizado.
- Distribuye 27 historias entre Sprint 2 y Sprint 10, tres por sprint.
- Reutiliza los Test Cases ya vinculados y crea únicamente los faltantes hasta llegar a tres por historia.
- Mueve los bugs que estén vinculados a la misma historia.
- Crea cualquier sprint faltante y vincula los nuevos casos automáticamente.
- Deja sin cambios los elementos que no formen parte del plan.

Para automatizaciones previamente revisadas puede omitirse la confirmación interactiva:

```powershell
python organize_sprints.py --apply
```

## Publicar la demo en Vercel

1. Sube el proyecto a un repositorio de GitHub sin incluir `.env`.
2. En Vercel, selecciona **Add New → Project** e importa el repositorio.
3. En **Settings → Environment Variables**, agrega todas las variables de `.env.example` con los valores de la cuenta demo.
4. Confirma que `AGENT_WRITE_MODE=false`.
5. Despliega el proyecto. Vercel detectará automáticamente `app.py` como aplicación FastAPI.

Utiliza exclusivamente un Jira de demostración con datos ficticios. Cualquier visitante de la URL pública podrá ver la información que devuelva el dashboard.

## Seguridad

- Los secretos se cargan por variables de entorno.
- `.env`, `.venv` y la configuración local de Vercel están excluidos de Git.
- El dashboard no muestra el email ni el API token.
- Con `AGENT_WRITE_MODE=false`, la aplicación no modifica Jira.
- La demo pública debe utilizar un usuario Jira dedicado y con los permisos mínimos necesarios.
