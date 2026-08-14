# OrangeHRM QA Manager Agent — MVP 1.3.1

Primer MVP para analizar un sprint de Jira y detectar riesgos de cobertura.

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

## Configuración

1. Copia `.env.example` como `.env`.
2. Configura `JIRA_EMAIL` y `JIRA_API_TOKEN`. No guardes el token en Git.
3. Deja `AGENT_WRITE_MODE=false` durante las primeras pruebas.
4. Instala y ejecuta:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload
```

## Uso

```bash
curl -X POST http://127.0.0.1:8000/analyze-sprint \
  -H 'Content-Type: application/json' \
  -d '{"sprint_name":"PROJ Sprint 1"}'
```

Dashboard: `http://127.0.0.1:8000/`

Documentación interactiva: `http://127.0.0.1:8000/docs`.

## Seguridad

- Los secretos se cargan por variables de entorno.
- El MVP no modifica Jira.
- La siguiente versión añadirá propuestas de acciones y aprobación humana antes de cualquier escritura.
