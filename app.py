from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import re
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    jira_base_url: str
    jira_email: str
    jira_api_token: str
    jira_project_key: str = "PROJ"
    jira_board_url: str = ""
    github_repository_url: str = ""
    agent_write_mode: bool = False


class JiraClient:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self.client = httpx.Client(
            base_url=cfg.jira_base_url.rstrip("/"),
            auth=(cfg.jira_email, cfg.jira_api_token),
            headers={"Accept": "application/json"},
            timeout=30,
        )

    def field_ids(self) -> dict[str, str]:
        response = self.client.get("/rest/api/3/field")
        response.raise_for_status()
        return {
            str(field.get("name", "")).strip().casefold(): field["id"]
            for field in response.json()
            if field.get("id") and field.get("name")
        }

    @staticmethod
    def _find_field(field_ids: dict[str, str], *names: str) -> str | None:
        for name in names:
            if field_id := field_ids.get(name.casefold()):
                return field_id
        return None

    def search(self, jql: str) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
        available_fields = self.field_ids()
        resolved_fields = {
            "execution_status": self._find_field(
                available_fields,
                "Execution Status",
                "Test Execution Status",
                "Test Result",
            ),
            "story_points": self._find_field(
                available_fields,
                "Story point estimate",
                "Story Points",
            ),
            "qa_assigned": self._find_field(
                available_fields,
                "QA Assigned",
                "QA Assignee",
                "QA Owner",
            ),
            "execution_date": self._find_field(
                available_fields,
                "Execution Date",
            ),
        }
        requested_fields = [
            "summary", "description", "issuetype", "status", "priority", "labels", "issuelinks",
            "assignee", "created", "updated", "statuscategorychangedate", "attachment"
        ]
        requested_fields.extend(
            field_id for field_id in resolved_fields.values() if field_id
        )
        response = self.client.get(
            "/rest/api/3/search/jql",
            params={
                "jql": jql,
                "maxResults": 100,
                "fields": ",".join(dict.fromkeys(requested_fields)),
            },
        )
        response.raise_for_status()
        return response.json().get("issues", []), resolved_fields

    def sprint_info(self, sprint_name: str) -> dict[str, Any] | None:
        for sprint in self.list_sprints():
            if sprint.get("name", "").casefold() == sprint_name.casefold():
                return sprint
        return None

    def list_sprints(self) -> list[dict[str, Any]]:
        boards = self.client.get(
            "/rest/agile/1.0/board",
            params={"projectKeyOrId": self.cfg.jira_project_key, "maxResults": 50},
        )
        boards.raise_for_status()
        sprints_by_id: dict[int, dict[str, Any]] = {}
        for board in boards.json().get("values", []):
            response = self.client.get(
                f"/rest/agile/1.0/board/{board['id']}/sprint",
                params={"state": "active,future,closed", "maxResults": 100},
            )
            response.raise_for_status()
            for sprint in response.json().get("values", []):
                if sprint.get("id") is not None:
                    sprints_by_id[int(sprint["id"])] = sprint
        state_order = {"active": 0, "future": 1, "closed": 2}
        return sorted(
            sprints_by_id.values(),
            key=lambda sprint: (
                state_order.get(sprint.get("state", ""), 3),
                sprint.get("startDate") or sprint.get("endDate") or "",
            ),
        )


class AnalyzeRequest(BaseModel):
    sprint_name: str


def build_sprint_jql(project_key: str, sprint_name: str, sprint: dict[str, Any] | None) -> str:
    """Build an exact sprint query, preferring Jira's unique numeric sprint ID."""
    sprint_id = (sprint or {}).get("id")
    if sprint_id is not None:
        sprint_filter = f"sprint = {int(sprint_id)}"
    else:
        escaped_name = sprint_name.replace('\\', '\\\\').replace('"', '\\"')
        sprint_filter = f'sprint = "{escaped_name}"'
    return (
        f"project = {project_key} AND "
        f"({sprint_filter} OR issuetype = \"Test Execution\") "
        "ORDER BY issuetype, key"
    )


def parse_jira_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def age_hours(value: str | None, now: datetime | None = None) -> int:
    changed = parse_jira_date(value)
    if not changed:
        return 0
    current = now or datetime.now(timezone.utc)
    return max(0, int((current - changed.astimezone(timezone.utc)).total_seconds() // 3600))


def sprint_forecast(
    sprint: dict[str, Any] | None,
    completed_points: float,
    total_points: float,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    start = parse_jira_date((sprint or {}).get("startDate"))
    end = parse_jira_date((sprint or {}).get("endDate"))
    progress = (completed_points / total_points) if total_points else 0.0
    if not start or not end or end <= start:
        return {"available": False, "probability": None, "pace": "Sin fechas del sprint", "remaining_days": None}
    elapsed = min(1.0, max(0.0, (current - start).total_seconds() / (end - start).total_seconds()))
    remaining_days = max(0, (end - current).total_seconds() / 86400)
    expected = max(elapsed, 0.05)
    pace_ratio = progress / expected
    probability = round(max(0, min(100, pace_ratio * 85))) if total_points else 0
    pace = "En ritmo" if pace_ratio >= 0.9 else "En riesgo" if pace_ratio >= 0.65 else "Atrasado"
    return {
        "available": True,
        "probability": probability,
        "pace": pace,
        "remaining_days": round(remaining_days, 1),
        "elapsed_percentage": round(elapsed * 100, 1),
        "completed_percentage": round(progress * 100, 1),
        "start": start.date().isoformat(),
        "end": end.date().isoformat(),
    }


def adf_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := adf_text(item)))
    if isinstance(value, dict):
        own_text = str(value.get("text", "")).strip()
        child_text = adf_text(value.get("content", []))
        return "\n".join(part for part in (own_text, child_text) if part)
    return ""


TEAM_RULES = [
    (("logout", "session", "login", "authentication"), "Diego Rojas", "Tomás Herrera"),
    (("authorization", "access", "permission", "role", "admin", "ess"), "Camila Torres", "Tomás Herrera"),
    (("api", "backend", "database", "server"), "Martín Acosta", "Tomás Herrera"),
    (("integration", "import", "export", "sync"), "Nicolás Vega", "Valentina Silva"),
    (("ui", "visual", "button", "layout", "form", "responsive"), "Lucía Fernández", "Valentina Silva"),
]


def propose_owners(summary: str, description: str = "") -> tuple[str, str]:
    text = f"{summary} {description}".casefold()
    for keywords, developer, qa_owner in TEAM_RULES:
        if any(keyword in text for keyword in keywords):
            return developer, qa_owner
    return "Diego Rojas", "Valentina Silva"


def _field_value(raw_value: Any) -> str | None:
    if raw_value is None:
        return None
    if isinstance(raw_value, dict):
        for key in ("value", "name", "displayName"):
            if raw_value.get(key) is not None:
                return str(raw_value[key]).strip()
        return None
    if isinstance(raw_value, list):
        values = [value for item in raw_value if (value := _field_value(item))]
        return ", ".join(values) or None
    return str(raw_value).strip() or None


def analyze_coverage(
    issues: list[dict[str, Any]],
    execution_field_id: str | None = "customfield_10097",
    story_points_field_id: str | None = "customfield_10016",
    qa_assigned_field_id: str | None = None,
    execution_date_field_id: str | None = None,
    jira_base_url: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    by_type: Counter[str] = Counter()
    status: Counter[str] = Counter()
    points: defaultdict[str, float] = defaultdict(float)
    links: defaultdict[str, set[str]] = defaultdict(set)
    execution: Counter[str] = Counter()
    bugs_by_priority: Counter[str] = Counter()
    open_bugs_by_priority: Counter[str] = Counter()
    tc_workflow: Counter[str] = Counter()
    delivery_workflow: defaultdict[str, Counter[str]] = defaultdict(Counter)
    tc_details: list[dict[str, str]] = []
    operational_items: list[dict[str, Any]] = []
    owner_load: defaultdict[str, Counter[str]] = defaultdict(Counter)
    qa_load: defaultdict[str, Counter[str]] = defaultdict(Counter)
    person_points: defaultdict[str, float] = defaultdict(float)
    dependencies: list[dict[str, Any]] = []
    total_points = 0.0
    completed_points = 0.0

    for issue in issues:
        fields = issue["fields"]
        issue_type = fields["issuetype"]["name"]
        by_type[issue_type] += 1
        status_name = fields["status"]["name"]
        status[status_name] += 1
        status_age = age_hours(fields.get("statuscategorychangedate") or fields.get("updated"), now)
        assignee = (fields.get("assignee") or {}).get("displayName") or "Sin asignar"
        qa_assigned = _field_value(fields.get(qa_assigned_field_id)) if qa_assigned_field_id else None
        qa_assigned = qa_assigned or "Sin QA asignado"
        issue_points = float(fields.get(story_points_field_id) or 0) if story_points_field_id else 0
        points[issue_type] += issue_points
        total_points += issue_points
        if fields["status"]["name"].casefold() in {"done", "closed", "resolved", "passed"}:
            completed_points += issue_points
        item_stage = None
        if issue_type == "Test Case":
            raw_execution = _field_value(fields.get(execution_field_id)) if execution_field_id else None
            execution_result = raw_execution or "Not Run"
            execution[execution_result] += 1
            workflow_status = fields["status"]["name"].casefold()
            result_status = execution_result.casefold()
            if workflow_status in {"review", "in review"}:
                stage = "testing"
            elif workflow_status in {"ready for qa", "ready for testing", "qa ready"}:
                stage = "ready_for_qa"
            elif workflow_status in {"ready for uat", "uat", "pre-production", "preproduction"}:
                stage = "ready_for_uat"
            elif workflow_status == "failed":
                stage = "failed"
            elif workflow_status == "in progress":
                stage = "development"
            elif workflow_status == "to do":
                stage = "pending_development"
            elif workflow_status in {"done", "closed", "resolved"} and result_status == "passed":
                stage = "approved"
            elif workflow_status in {"done", "closed", "resolved"}:
                stage = "workflow_anomaly"
            else:
                stage = "other"
            tc_workflow[stage] += 1
            delivery_workflow[stage]["test_cases"] += 1
            item_stage = stage
            tc_details.append({
                "key": issue["key"],
                "summary": fields["summary"],
                "workflow_status": fields["status"]["name"],
                "execution_status": execution_result,
                "stage": stage,
            })
        if issue_type == "Bug":
            bug_priority = (fields.get("priority") or {}).get("name", "None")
            bugs_by_priority[bug_priority] += 1
            if fields["status"]["name"].casefold() not in {"done", "closed", "resolved"}:
                open_bugs_by_priority[bug_priority] += 1
            bug_status = fields["status"]["name"].casefold()
            bug_stage = {
                "to do": "pending_development",
                "in progress": "development",
                "ready for qa": "ready_for_qa",
                "ready for testing": "ready_for_qa",
                "qa ready": "ready_for_qa",
                "review": "testing",
                "in review": "testing",
                "ready for uat": "ready_for_uat",
                "uat": "ready_for_uat",
                "failed": "failed",
                "done": "resolved",
                "closed": "resolved",
                "resolved": "resolved",
            }.get(bug_status, "other")
            delivery_workflow[bug_stage]["bugs"] += 1
            item_stage = bug_stage
        if item_stage:
            owner_load[assignee][item_stage] += 1
            qa_load[qa_assigned][item_stage] += 1
            person_points[assignee] += issue_points
            operational_items.append({
                "key": issue["key"],
                "summary": fields["summary"],
                "type": issue_type,
                "status": status_name,
                "stage": item_stage,
                "age_hours": status_age,
                "assignee": f"Dev: {assignee} · QA: {qa_assigned}",
                "developer_assigned": assignee,
                "qa_assigned": qa_assigned,
                "priority": (fields.get("priority") or {}).get("name", "None"),
                "story_points": issue_points,
                "blocked": "block" in status_name.casefold() or any(
                    str(label).casefold() in {"blocked", "impediment"} for label in (fields.get("labels") or [])
                ),
                "url": f"{jira_base_url.rstrip('/')}/browse/{issue['key']}" if jira_base_url else "",
            })
        for link in fields.get("issuelinks") or []:
            other = link.get("outwardIssue") or link.get("inwardIssue")
            if other:
                links[issue["key"]].add(other["key"])
                links[other["key"]].add(issue["key"])
                link_type = link.get("type") or {}
                direction = "outward" if link.get("outwardIssue") else "inward"
                relationship = link_type.get(direction) or link_type.get("name") or "relacionado con"
                dependencies.append({
                    "source": issue["key"], "target": other["key"],
                    "relationship": relationship,
                    "is_blocking": "block" in str(relationship).casefold(),
                    "source_url": f"{jira_base_url.rstrip('/')}/browse/{issue['key']}" if jira_base_url else "",
                    "target_url": f"{jira_base_url.rstrip('/')}/browse/{other['key']}" if jira_base_url else "",
                })

    stories = [x for x in issues if x["fields"]["issuetype"]["name"] == "Story"]
    test_cases = [x for x in issues if x["fields"]["issuetype"]["name"] == "Test Case"]
    tc_keys = {x["key"] for x in test_cases}
    execution_issues = [x for x in issues if x["fields"]["issuetype"]["name"] == "Test Execution"]
    execution_details: list[dict[str, Any]] = []
    execution_by_result: Counter[str] = Counter()
    execution_by_workflow: Counter[str] = Counter()
    execution_by_qa: Counter[str] = Counter()
    execution_by_environment: Counter[str] = Counter()
    for item in execution_issues:
        fields = item["fields"]
        description = adf_text(fields.get("description"))
        reference_match = re.search(r"[A-Z][A-Z0-9]+-\d+", f"{fields.get('summary', '')} {description}", re.IGNORECASE)
        test_case_key = reference_match.group(0).upper() if reference_match else None
        linked_test_cases = sorted(links[item["key"]] & tc_keys)
        if test_case_key not in tc_keys:
            test_case_key = linked_test_cases[0] if linked_test_cases else None
        if test_case_key not in tc_keys:
            continue
        result = _field_value(fields.get(execution_field_id)) if execution_field_id else None
        result = result or "Not Run"
        qa_name = _field_value(fields.get("assignee"))
        if not qa_name and qa_assigned_field_id:
            qa_name = _field_value(fields.get(qa_assigned_field_id))
        qa_name = qa_name or "Sin QA asignado"
        environment_match = re.search(r"Ambiente\s*:\s*([^\n]+)", description, re.IGNORECASE)
        environment = environment_match.group(1).strip() if environment_match else "Sin ambiente"
        evidence_count = len(fields.get("attachment") or [])
        has_bug = any(
            ((link.get("outwardIssue") or link.get("inwardIssue") or {}).get("fields") or {})
            .get("issuetype", {}).get("name") == "Bug"
            for link in (fields.get("issuelinks") or [])
        )
        execution_date = _field_value(fields.get(execution_date_field_id)) if execution_date_field_id else None
        execution_date = execution_date or fields.get("created") or ""
        detail = {
            "key": item["key"], "summary": fields["summary"], "test_case_key": test_case_key,
            "result": result, "qa_executor": qa_name, "qa_assigned": qa_name, "environment": environment,
            "execution_date": execution_date, "evidence_count": evidence_count, "has_bug": has_bug,
            "status": fields["status"]["name"],
            "url": f"{jira_base_url.rstrip('/')}/browse/{item['key']}" if jira_base_url else "",
        }
        execution_details.append(detail)
        execution_by_result[result] += 1
        execution_by_workflow[fields["status"]["name"]] += 1
        execution_by_qa[qa_name] += 1
        execution_by_environment[environment] += 1

    latest_by_test_case: dict[str, dict[str, Any]] = {}
    for item in sorted(execution_details, key=lambda x: x["execution_date"]):
        if item["test_case_key"] in tc_keys:
            latest_by_test_case[item["test_case_key"]] = item
    test_cases_without_execution = sorted(tc_keys - set(latest_by_test_case))
    failed_without_evidence = [x["key"] for x in execution_details if x["result"].casefold() == "failed" and x["evidence_count"] == 0]
    failed_without_bug = [x["key"] for x in execution_details if x["result"].casefold() == "failed" and not x["has_bug"]]
    failed_executions = [x["key"] for x in execution_details if x["result"].casefold() == "failed"]
    blocked_executions = [x["key"] for x in execution_details if x["result"].casefold() == "blocked"]
    pending_executions = [x["key"] for x in execution_details if x["result"].casefold() == "not run"]
    incomplete_execution_workflow = [
        x["key"] for x in execution_details
        if x["status"].casefold() not in {"done", "closed", "resolved"}
    ]
    tc_stage_by_key = {item["key"]: item["stage"] for item in tc_details}
    coverage = {}
    for story in stories:
        related = sorted(links[story["key"]] & tc_keys)
        ready_to_close = bool(related) and all(tc_stage_by_key.get(key) == "approved" for key in related)
        coverage[story["key"]] = {
            "summary": story["fields"]["summary"],
            "jira_status": story["fields"]["status"]["name"],
            "test_cases": related,
            "count": len(related),
            "coverage_risk": "high" if len(related) == 0 else "medium" if len(related) < 3 else "low",
            "ready_to_close": ready_to_close,
            "delivery_state": "Lista para cerrar" if ready_to_close else "En curso",
        }

    approved = tc_workflow["approved"]
    pass_rate = round((approved / len(test_cases)) * 100, 1) if test_cases else 0.0
    total_bugs = len([issue for issue in issues if issue["fields"]["issuetype"]["name"] == "Bug"])
    open_bugs = sum(open_bugs_by_priority.values())
    resolved_bugs = total_bugs - open_bugs
    all_tests_approved = bool(test_cases) and approved == len(test_cases)
    high_risk = bool(
        [k for k, v in coverage.items() if v["count"] < 3]
        or open_bugs_by_priority.get("Highest", 0)
        or not all_tests_approved
        or tc_workflow.get("testing", 0)
        or tc_workflow.get("failed", 0)
        or tc_workflow.get("workflow_anomaly", 0)
        or pending_executions
        or failed_executions
        or blocked_executions
        or incomplete_execution_workflow
        or test_cases_without_execution
    )
    points_progress = round((completed_points / total_points) * 100, 1) if total_points else 0.0
    actions = []
    if any(v["count"] < 3 for v in coverage.values()):
        actions.append("Completar al menos 3 casos de prueba para cada historia con cobertura insuficiente.")
    if tc_workflow.get("pending_development", 0):
        actions.append("Iniciar el desarrollo de los Test Cases pendientes en To Do.")
    if tc_workflow.get("development", 0):
        actions.append("Completar los Test Cases en desarrollo y moverlos a Ready for QA.")
    if tc_workflow.get("ready_for_qa", 0):
        actions.append("Asignar capacidad QA a los Test Cases en Ready for QA y moverlos a Review al iniciar la validación.")
    if tc_workflow.get("testing", 0):
        actions.append("Completar la validación QA de los Test Cases en Review.")
    if tc_workflow.get("ready_for_uat", 0):
        actions.append("Ejecutar en preproducción los Test Cases en Ready for UAT y moverlos a Done solo si pasan.")
    if tc_workflow.get("failed", 0):
        actions.append("Tomar los Test Cases en Failed, iniciar la corrección en In Progress y enviarlos nuevamente a Review.")
    if tc_workflow.get("workflow_anomaly", 0):
        actions.append("Revisar los Test Cases en Done sin Passed y moverlos a Failed o Review según el último resultado.")
    if open_bugs_by_priority.get("Highest", 0):
        actions.append("Resolver o aceptar formalmente el riesgo de los bugs Highest abiertos.")
    if test_cases_without_execution:
        actions.append("Crear una Test Execution para cada Test Case sin ejecución registrada.")
    if pending_executions:
        actions.append("Ejecutar las Test Execution con resultado Not Run.")
    if blocked_executions:
        actions.append("Resolver el impedimento de las Test Execution con resultado Blocked.")
    if failed_executions:
        actions.append("Corregir y volver a ejecutar las Test Execution con resultado Failed.")
    if failed_without_evidence:
        actions.append("Adjuntar evidencia en todas las Test Execution con resultado Failed.")
    if failed_without_bug:
        actions.append("Vincular un Bug a todas las Test Execution con resultado Failed.")
    if not actions:
        actions.append("Mantener la regresión y preparar la evidencia para el cierre del sprint.")

    alerts: list[dict[str, Any]] = []
    for item in operational_items:
        message = None
        level = "warning"
        if item["blocked"]:
            message = f"{item['key']} está bloqueado"
            level = "critical"
        elif item["type"] == "Bug" and item["priority"].casefold() in {"highest", "critical"} and item["stage"] != "resolved":
            message = f"{item['key']} es un bug {item['priority']} abierto"
            level = "critical"
        elif item["stage"] == "ready_for_qa" and item["age_hours"] >= 24:
            message = f"{item['key']} espera QA hace {item['age_hours']} h"
        elif item["stage"] == "failed" and item["age_hours"] >= 8:
            message = f"{item['key']} sigue en Failed y requiere volver a desarrollo"
            level = "critical"
        elif item["stage"] == "ready_for_uat" and item["age_hours"] >= 48:
            message = f"{item['key']} espera UAT hace {item['age_hours']} h"
        if message:
            alerts.append({**item, "message": message, "level": level})
        if item["stage"] in {"ready_for_qa", "testing", "ready_for_uat"} and item["qa_assigned"] == "Sin QA asignado":
            alerts.append({**item, "message": f"{item['key']} está en {item['status']} sin QA Assigned", "level": "critical"})
    for key, value in coverage.items():
        if value["count"] < 3:
            alerts.append({
                "key": key, "summary": value["summary"], "type": "Story", "status": "Cobertura",
                "stage": "coverage", "age_hours": 0, "assignee": "Equipo QA", "priority": "High",
                "url": f"{jira_base_url.rstrip('/')}/browse/{key}" if jira_base_url else "",
                "message": f"{key} tiene {value['count']}/3 casos vinculados", "level": "critical" if value["count"] == 0 else "warning",
            })
    for item in execution_details:
        if item["result"].casefold() == "failed" and item["evidence_count"] == 0:
            alerts.append({**item, "type": "Test Execution", "age_hours": 0, "message": f"{item['key']} falló sin evidencia adjunta", "level": "critical"})
        if item["result"].casefold() == "failed" and not item["has_bug"]:
            alerts.append({**item, "type": "Test Execution", "age_hours": 0, "message": f"{item['key']} falló sin bug vinculado", "level": "critical"})
        if item["result"].casefold() == "blocked":
            alerts.append({**item, "type": "Test Execution", "age_hours": 0, "message": f"{item['key']} está bloqueada", "level": "critical"})
        if item["qa_executor"] == "Sin QA asignado":
            alerts.append({**item, "type": "Test Execution", "age_hours": 0, "message": f"{item['key']} no tiene QA asignado en Assignee", "level": "warning"})
        if item["status"].casefold() in {"done", "closed", "resolved"} and item["result"].casefold() != "passed":
            alerts.append({**item, "type": "Test Execution", "age_hours": 0, "message": f"{item['key']} está Done sin resultado Passed", "level": "critical"})
        if item["result"].casefold() == "passed" and item["status"].casefold() not in {"done", "closed", "resolved"}:
            alerts.append({**item, "type": "Test Execution", "age_hours": 0, "message": f"{item['key']} tiene Passed pero todavía está en {item['status']}", "level": "warning"})

    developer_queue = sum(
        counts["test_cases"] + counts["bugs"] for stage, counts in delivery_workflow.items()
        if stage in {"pending_development", "development", "failed"}
    )
    qa_queue = sum(
        counts["test_cases"] + counts["bugs"] for stage, counts in delivery_workflow.items()
        if stage in {"ready_for_qa", "testing", "ready_for_uat"}
    )
    active_stages = {
        "pending_development", "development", "failed",
        "ready_for_qa", "testing", "ready_for_uat", "workflow_anomaly", "other",
    }
    active_items = [item for item in operational_items if item["stage"] in active_stages]
    development_responsibility = len(active_items)
    development_unassigned = sum(
        1 for item in active_items if item["developer_assigned"] == "Sin asignar"
    )
    team_capacity = {
        "developers": {
            "people": 5,
            "active_total": development_responsibility,
            "unassigned": development_unassigned,
            "queue": developer_queue,
            "items_per_person": round(development_responsibility / 5, 1),
            "queue_per_person": round(developer_queue / 5, 1),
        },
        "qa": {"people": 2, "queue": qa_queue, "items_per_person": round(qa_queue / 2, 1)},
        "owner_load": {
            **{f"Desarrollo · {owner}": dict(counts) for owner, counts in sorted(owner_load.items())},
            **{f"QA · {owner}": dict(counts) for owner, counts in sorted(qa_load.items())},
        },
        "qa_load": {owner: dict(counts) for owner, counts in sorted(qa_load.items())},
        "people": [
            {
                "name": owner,
                "role": "QA" if owner.startswith("QA · ") else "Desarrollo",
                "active_items": sum(counts.values()),
                "story_points": round(person_points.get(owner.removeprefix("Desarrollo · "), 0), 1),
                "stages": dict(counts),
            }
            for owner, counts in sorted({
                **{f"Desarrollo · {name}": values for name, values in owner_load.items()},
                **{f"QA · {name}": values for name, values in qa_load.items()},
            }.items())
            if not owner.endswith("Sin asignar") and not owner.endswith("Sin QA asignado")
        ],
    }

    stage_aging: dict[str, dict[str, Any]] = {}
    for stage in sorted({item["stage"] for item in operational_items}):
        stage_items = [item for item in operational_items if item["stage"] == stage]
        stage_aging[stage] = {
            "count": len(stage_items),
            "average_hours": round(sum(item["age_hours"] for item in stage_items) / len(stage_items), 1),
            "oldest_hours": max(item["age_hours"] for item in stage_items),
            "oldest_key": max(stage_items, key=lambda item: item["age_hours"])["key"],
        }

    sorted_alerts = sorted(alerts, key=lambda x: (x["level"] != "critical", -x["age_hours"]))
    today_priorities = [
        {"rank": index, "key": item["key"], "message": item["message"], "level": item["level"], "url": item.get("url", "")}
        for index, item in enumerate(sorted_alerts[:5], 1)
    ]

    return {
        "total_items": len(issues),
        "items_by_type": dict(by_type),
        "points_by_type": dict(points),
        "status_distribution": dict(status),
        "execution_distribution": dict(execution),
        "execution_progress": {"approved": approved, "total": len(test_cases), "pass_rate": pass_rate},
        "test_case_workflow": dict(tc_workflow),
        "delivery_workflow": {
            stage: {
                "test_cases": counts["test_cases"],
                "bugs": counts["bugs"],
                "total": counts["test_cases"] + counts["bugs"],
            }
            for stage, counts in delivery_workflow.items()
        },
        "test_case_details": tc_details,
        "operational_items": sorted(operational_items, key=lambda x: (-x["age_hours"], x["key"])),
        "alerts": sorted_alerts,
        "today_priorities": today_priorities,
        "stage_aging": stage_aging,
        "dependencies": dependencies,
        "team_capacity": team_capacity,
        "test_executions": {
            "total": len(execution_details),
            "by_result": dict(execution_by_result),
            "by_workflow": dict(execution_by_workflow),
            "by_qa": dict(execution_by_qa),
            "by_environment": dict(execution_by_environment),
            "details": sorted(execution_details, key=lambda x: x["execution_date"], reverse=True),
            "latest_by_test_case": latest_by_test_case,
            "test_cases_without_execution": test_cases_without_execution,
            "failed_without_evidence": failed_without_evidence,
            "failed_without_bug": failed_without_bug,
            "pending": pending_executions,
            "failed": failed_executions,
            "blocked": blocked_executions,
            "attention_required": [
                item for item in execution_details
                if item["result"].casefold() in {"failed", "blocked", "not run"}
            ],
            "incomplete_workflow": incomplete_execution_workflow,
        },
        "bugs_by_priority": dict(bugs_by_priority),
        "open_bugs_by_priority": dict(open_bugs_by_priority),
        "bug_progress": {"open": open_bugs, "resolved": resolved_bugs, "total": total_bugs},
        "sprint_progress": {
            "completed_points": completed_points,
            "total_points": total_points,
            "percentage": points_progress,
        },
        "story_coverage": coverage,
        "stories_without_tests": [k for k, v in coverage.items() if v["count"] == 0],
        "stories_below_target": [k for k, v in coverage.items() if v["count"] < 3],
        "stories_ready_to_close": [k for k, v in coverage.items() if v["ready_to_close"]],
        "release_recommendation": "NO-GO" if high_risk else "GO",
        "executive_summary": (
            f"Se analizaron {len(issues)} elementos, {len(stories)} historias y {len(test_cases)} casos de prueba. "
            f"Hay {tc_workflow['testing']} casos en testing QA, {tc_workflow['failed']} en Failed, "
            f"{tc_workflow['ready_for_qa']} esperando QA, {tc_workflow['ready_for_uat']} en preproducción "
            f"y {approved} aprobados definitivamente (Done + Passed) "
            f"de un total de {len(test_cases)}. "
            f"Se registraron {len(execution_details)} Test Execution: "
            f"{execution_by_result.get('Passed', 0)} Passed, {execution_by_result.get('Failed', 0)} Failed "
            f"y {execution_by_result.get('Not Run', 0)} Not Run. "
            f"El avance por story points es {completed_points:g}/{total_points:g} ({points_progress}%). "
            f"La recomendación actual es {'NO-GO' if high_risk else 'GO'}."
        ),
        "action_items": actions,
        "risk_reasons": [
            reason for condition, reason in [
                (any(v["count"] < 3 for v in coverage.values()), "Historias con cobertura inferior a 3 casos"),
                (open_bugs_by_priority.get("Highest", 0) > 0, "Existen bugs Highest abiertos"),
                (tc_workflow.get("testing", 0) > 0, "Existen Test Cases en testing (Review)"),
                (tc_workflow.get("ready_for_qa", 0) > 0, "Existen Test Cases desarrollados esperando QA"),
                (tc_workflow.get("ready_for_uat", 0) > 0, "Existen Test Cases pendientes de validación UAT en preproducción"),
                (tc_workflow.get("failed", 0) > 0, "Existen Test Cases en Failed esperando corrección"),
                (tc_workflow.get("development", 0) > 0, "Existen Test Cases en desarrollo"),
                (tc_workflow.get("pending_development", 0) > 0, "Existen Test Cases pendientes de desarrollo"),
                (tc_workflow.get("workflow_anomaly", 0) > 0, "Existen Test Cases en Done sin Passed; revisar el workflow"),
                (len(pending_executions) > 0, f"Test Execution Not Run: {', '.join(pending_executions)}"),
                (len(blocked_executions) > 0, f"Test Execution Blocked: {', '.join(blocked_executions)}"),
                (len(failed_executions) > 0, f"Test Execution Failed: {', '.join(failed_executions)}"),
                (len(incomplete_execution_workflow) > 0, "Existen Test Execution cuyo workflow todavía no está Done"),
                (len(test_cases_without_execution) > 0, "Existen Test Cases sin Test Execution registrada"),
                (len(failed_without_evidence) > 0, "Existen ejecuciones Failed sin evidencia"),
                (len(failed_without_bug) > 0, "Existen ejecuciones Failed sin bug vinculado"),
            ] if condition
        ],
        "field_mapping": {
            "execution_status": execution_field_id,
            "story_points": story_points_field_id,
            "qa_assigned": qa_assigned_field_id,
            "execution_date": execution_date_field_id,
        },
        "data_warnings": [
            warning for condition, warning in [
                (execution_field_id is None, "No se encontró el campo Execution Status en Jira"),
                (story_points_field_id is None, "No se encontró el campo Story point estimate en Jira"),
                (qa_assigned_field_id is None, "No se encontró el campo QA Assigned en Jira"),
            ] if condition
        ],
    }


app = FastAPI(title="SprintGuard API", version="1.9.0")

DASHBOARD_HTML = """<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SprintGuard — QA &amp; Delivery Intelligence</title><style>
:root{--navy:#14213d;--blue:#2563eb;--bg:#f4f7fb;--red:#dc2626;--green:#16a34a;--amber:#d97706}
*{box-sizing:border-box}body{margin:0;font-family:Inter,Segoe UI,Arial;background:var(--bg);color:#172033}
header{background:linear-gradient(120deg,var(--navy),#253f70);color:white;padding:26px 7vw}header h1{margin:0 0 5px;font-size:26px}header p{margin:0;opacity:.8}
main{max-width:1180px;margin:24px auto;padding:0 18px}.controls,.card{background:white;border-radius:14px;box-shadow:0 5px 20px #1f2b4410}
.controls{display:flex;gap:10px;padding:16px;margin-bottom:18px;align-items:center;flex-wrap:wrap}select{flex:1;min-width:260px;padding:11px;border:1px solid #ccd5e2;border-radius:8px;background:white;color:#172033}button,.button-link{background:var(--blue);color:white;border:0;padding:11px 18px;border-radius:8px;font-weight:600;cursor:pointer;text-decoration:none;white-space:nowrap}.button-link:hover{color:white;text-decoration:none}.secondary{background:#475569}.demo{max-width:1180px;margin:18px auto 0;padding:12px 18px;background:#e0ecff;color:#1e3a8a;border-radius:10px}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.card{padding:18px}.metric{font-size:30px;font-weight:750;margin-top:6px}.muted{color:#6b7280;font-size:13px}.breakdown{color:#64748b;font-size:12px;margin-top:5px}
.wide{grid-column:span 2}table{width:100%;border-collapse:collapse;margin-top:8px}th,td{text-align:left;padding:10px;border-bottom:1px solid #edf0f5;font-size:14px}
.pill{padding:5px 9px;border-radius:999px;font-weight:700;font-size:12px}.go{background:#dcfce7;color:#166534}.nogo{background:#fee2e2;color:#991b1b}.warn{background:#fef3c7;color:#92400e}.low{color:var(--green)}.medium{color:var(--amber)}.high{color:var(--red)}
#error{color:var(--red);padding:10px 0}.hidden{display:none}.full{grid-column:1/-1}.report-title{display:flex;justify-content:space-between;align-items:center}.bar{height:10px;background:#e5e7eb;border-radius:999px;overflow:hidden}.bar span{display:block;height:100%;background:var(--blue)}@media(max-width:800px){.grid{grid-template-columns:1fr}.wide{grid-column:span 1}}@media print{header,.controls,#error,#print{display:none!important}body{background:white}.card{box-shadow:none;border:1px solid #ddd}.grid{display:block}.card{margin-bottom:12px}}
a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}.alert{border-left:4px solid var(--amber);padding:9px 11px;margin:8px 0;background:#fffbeb;border-radius:6px}.alert.critical{border-color:var(--red);background:#fef2f2}.item-table{max-height:420px;overflow:auto}.compact-card{align-self:start}.dependency-scroll{max-height:180px;overflow-y:auto;padding-right:8px;scrollbar-width:thin;scrollbar-color:#94a3b8 #eef2f7}.dependency-scroll p{margin:0;padding:9px 3px;border-bottom:1px solid #edf0f5}.dependency-scroll p:last-child{border-bottom:0}.dependency-scroll::-webkit-scrollbar{width:8px}.dependency-scroll::-webkit-scrollbar-track{background:#eef2f7;border-radius:8px}.dependency-scroll::-webkit-scrollbar-thumb{background:#94a3b8;border-radius:8px}.sync{font-size:12px;color:#64748b;margin-left:auto;align-self:center}.nowrap{white-space:nowrap}
</style></head><body><header><h1>SprintGuard</h1><p>QA &amp; Delivery Intelligence</p></header><div class="demo"><b>Demo de solo lectura.</b> Cobertura, ejecución, evidencias, riesgos y decisión de release desde Jira, sin modificar elementos.</div>
<main><section class="controls"><select id="sprint" aria-label="Seleccionar sprint"><option value="">Cargando sprints desde Jira…</option></select><button id="refresh" onclick="loadReport()">Actualizar desde Jira</button><a id="jiraLink" class="button-link secondary hidden" target="_blank" rel="noopener">Ver board en Jira</a><a id="githubLink" class="button-link secondary hidden" target="_blank" rel="noopener">Ver código en GitHub</a><span id="sync" class="sync">Sin sincronizar</span></section><div id="error"></div>
<section id="results" class="grid hidden"><article class="card wide"><h3>Objetivo del sprint</h3><p id="sprintgoal">—</p></article><article class="card"><div class="muted">Salud del sprint</div><div id="health" class="metric">—</div><div id="healthdetail" class="breakdown"></div></article><article class="card"><div class="muted">Prioridades para hoy</div><div id="prioritycount" class="metric">—</div><div class="breakdown">acciones ordenadas por impacto</div></article><article class="card full"><h2>Qué atender hoy</h2><div id="today"></div></article><article class="card"><div class="muted">Historias</div><div id="stories" class="metric">—</div></article><article class="card"><div class="muted">Test Cases</div><div id="testcases" class="metric">—</div></article><article class="card"><div class="muted">Bugs</div><div id="bugcount" class="metric">—</div></article><article class="card"><div class="muted">Test Executions</div><div id="tecount" class="metric">—</div></article><article class="card"><div class="muted">Ejecuciones Passed</div><div id="tepassed" class="metric">—</div></article><article class="card"><div class="muted">Ejecuciones Failed</div><div id="tefailed" class="metric">—</div></article><article class="card"><div class="muted">Ejecuciones Not Run</div><div id="tenotrun" class="metric">—</div></article><article class="card"><div class="muted">Sin QA asignado</div><div id="teunassigned" class="metric">—</div></article><article class="card"><div class="muted">Pendientes</div><div id="pending" class="metric">—</div><div id="pendingbreak" class="breakdown"></div></article><article class="card"><div class="muted">En desarrollo</div><div id="development" class="metric">—</div><div id="developmentbreak" class="breakdown"></div></article><article class="card"><div class="muted">Ready for QA</div><div id="readyqa" class="metric">—</div><div id="readyqabreak" class="breakdown"></div></article><article class="card"><div class="muted">Testing (Review)</div><div id="testing" class="metric">—</div><div id="testingbreak" class="breakdown"></div></article><article class="card"><div class="muted">Ready for UAT</div><div id="readyuat" class="metric">—</div><div id="readyuatbreak" class="breakdown"></div></article><article class="card"><div class="muted">Failed</div><div id="failed" class="metric">—</div><div id="failedbreak" class="breakdown"></div></article><article class="card"><div class="muted">TC aprobados</div><div id="approved" class="metric">—</div></article><article class="card"><div class="muted">Bugs resueltos</div><div id="resolvedbugs" class="metric">—</div></article><article class="card"><div class="muted">Bugs abiertos</div><div id="openbugs" class="metric">—</div></article><article class="card"><div class="muted">Pass rate final</div><div id="pass" class="metric">—</div></article><article class="card"><div class="muted">Recomendación</div><div id="decision" class="metric">—</div></article>
<article class="card"><div class="muted">Ejecuciones Blocked</div><div id="teblocked" class="metric">—</div></article>
<article class="card wide"><h3>Cobertura por historia</h3><table><thead><tr><th>Historia</th><th>Estado Jira</th><th>Casos</th><th>Riesgo</th><th>Preparación</th></tr></thead><tbody id="coverage"></tbody></table></article>
<article class="card"><h3>Último resultado</h3><div id="execution"></div></article><article class="card"><h3>Riesgos</h3><ul id="risks"></ul></article>
<article class="card wide"><h3>Alertas operativas</h3><div id="alerts"></div></article><article class="card"><h3>Capacidad del equipo</h3><div id="capacity"></div></article><article class="card"><h3>Proyección del sprint</h3><div id="forecast"></div></article>
<article class="card full"><h3>Workflow de Test Execution</h3><div id="teworkflow"></div></article>
<article class="card full"><h3>Ejecuciones que requieren atención</h3><div id="executionattention"></div></article>
<article class="card wide"><h3>Tiempo por estado</h3><div id="aging"></div></article><article class="card wide compact-card"><h3>Dependencias y bloqueos</h3><div id="dependencies" class="dependency-scroll"></div></article>
<article class="card full"><h3>Carga individual</h3><div class="item-table"><table><thead><tr><th>Persona</th><th>Rol</th><th>Elementos activos</th><th>Story points</th><th>Distribución</th></tr></thead><tbody id="people"></tbody></table></div></article>
<article class="card full"><h3>Seguimiento diario por antigüedad</h3><div class="item-table"><table><thead><tr><th>Elemento</th><th>Tipo</th><th>Estado</th><th>Responsables (Dev / QA)</th><th>Tiempo en estado</th></tr></thead><tbody id="operational"></tbody></table></div></article>
<article class="card full"><h3>Ejecuciones y evidencias</h3><div class="item-table"><table><thead><tr><th>Test Execution</th><th>Test Case</th><th>Estado Jira</th><th>Ambiente</th><th>QA ejecutor (Assignee)</th><th>Resultado</th><th>Evidencias</th></tr></thead><tbody id="executionrows"></tbody></table></div></article>
<article class="card full"><div class="report-title"><h2>Informe automático del sprint</h2><button id="print" class="secondary" onclick="window.print()">Imprimir / Guardar PDF</button></div><p id="summary"></p><h3>Avance por story points</h3><p id="points"></p><div class="bar"><span id="pointsbar"></span></div><h3>Bugs abiertos por prioridad</h3><div id="bugs"></div><h3>Acciones recomendadas</h3><ol id="actions"></ol></article>
</section></main>
<script>function esc(s){return String(s).replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]))}
function showStage(dw,stage,id){const x=dw[stage]||{total:0,test_cases:0,bugs:0};document.getElementById(id).textContent=x.total;document.getElementById(id+'break').textContent=x.test_cases+' TC · '+x.bugs+' Bugs'}
function issueLink(x,label){return x.url?'<a href="'+esc(x.url)+'" target="_blank" rel="noopener"><b>'+esc(label||x.key)+'</b></a>':'<b>'+esc(label||x.key)+'</b>'}
async function loadSprints(){const select=document.getElementById('sprint'),error=document.getElementById('error');try{const r=await fetch('/sprints?refresh='+Date.now(),{cache:'no-store'}),d=await r.json();if(!r.ok)throw new Error(d.detail||'No se pudieron cargar los sprints');select.innerHTML=d.sprints.map(s=>'<option value="'+esc(s.name)+'" '+(s.selected?'selected':'')+'>'+esc(s.label)+'</option>').join('');if(!d.sprints.length)throw new Error('Jira no devolvió sprints disponibles');await loadReport()}catch(e){select.innerHTML='<option value="PROJ Sprint 1">PROJ Sprint 1 (selección de respaldo)</option>';error.textContent=e.message}}
async function loadReport(){const error=document.getElementById('error'),btn=document.getElementById('refresh');error.textContent='Sincronizando con Jira…';btn.disabled=true;try{const r=await fetch('/analyze-sprint',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sprint_name:document.getElementById('sprint').value})});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Error');const a=d.analysis,dw=a.delivery_workflow,b=a.bug_progress,te=a.test_executions;error.textContent=a.data_warnings.join(' · ');document.getElementById('sync').textContent='Última sincronización: '+new Date(a.last_synced_at).toLocaleString();const jl=document.getElementById('jiraLink'),gl=document.getElementById('githubLink');if(d.jira_board_url){jl.href=d.jira_board_url;jl.classList.remove('hidden')}if(d.github_repository_url){gl.href=d.github_repository_url;gl.classList.remove('hidden')}document.getElementById('stories').textContent=a.items_by_type.Story||0;document.getElementById('testcases').textContent=a.items_by_type['Test Case']||0;document.getElementById('bugcount').textContent=a.items_by_type.Bug||0;document.getElementById('tecount').textContent=te.total;document.getElementById('tepassed').textContent=te.by_result.Passed||0;document.getElementById('tefailed').textContent=(te.by_result.Failed||0)+(te.by_result.Blocked||0);document.getElementById('tenotrun').textContent=te.by_result['Not Run']||0;document.getElementById('teunassigned').textContent=te.by_qa['Sin QA asignado']||0;document.getElementById('teworkflow').innerHTML=Object.entries(te.by_workflow).map(([k,v])=>'<span class="pill '+(k==='Done'?'go':k==='Failed'?'nogo':'warn')+'>'+esc(k)+': '+v+'</span> ').join('')||'<span class="muted">Sin estados</span>';showStage(dw,'pending_development','pending');showStage(dw,'development','development');showStage(dw,'ready_for_qa','readyqa');showStage(dw,'testing','testing');showStage(dw,'ready_for_uat','readyuat');showStage(dw,'failed','failed');document.getElementById('approved').textContent=a.execution_progress.approved+'/'+a.execution_progress.total;document.getElementById('resolvedbugs').textContent=b.resolved+'/'+b.total;document.getElementById('openbugs').textContent=b.open+'/'+b.total;document.getElementById('pass').textContent=a.execution_progress.pass_rate+'%';const dec=document.getElementById('decision');dec.innerHTML='<span class="pill '+(a.release_recommendation==='GO'?'go':'nogo')+'">'+a.release_recommendation+'</span>';document.getElementById('coverage').innerHTML=Object.entries(a.story_coverage).map(([k,v])=>'<tr><td><a href="'+esc(d.jira_base_url||'')+'/browse/'+esc(k)+'" target="_blank"><b>'+esc(k)+'</b></a><br><span class="muted">'+esc(v.summary)+'</span></td><td>'+v.count+'</td><td class="'+v.coverage_risk+'">'+v.coverage_risk+'</td><td><span class="pill '+(v.ready_to_close?'go':'nogo')+'">'+esc(v.delivery_state)+'</span></td></tr>').join('');document.getElementById('execution').innerHTML=Object.entries(a.execution_distribution).map(([k,v])=>'<p>'+esc(k)+': <b>'+v+'</b></p>').join('')||'<p class="muted">Sin datos</p>';document.getElementById('risks').innerHTML=a.risk_reasons.map(x=>'<li>'+esc(x)+'</li>').join('')||'<li>Sin riesgos críticos</li>';document.getElementById('alerts').innerHTML=a.alerts.map(x=>'<div class="alert '+x.level+'">'+issueLink(x)+' — '+esc(x.message)+'</div>').join('')||'<span class="pill go">Sin alertas vencidas</span>';const c=a.team_capacity;document.getElementById('capacity').innerHTML='<p><b>Activos bajo responsabilidad de desarrollo:</b> '+c.developers.active_total+' / '+c.developers.people+' personas ('+c.developers.items_per_person+' por persona)</p><p><b>Pendientes de trabajo de desarrollo:</b> '+c.developers.queue+' ('+c.developers.queue_per_person+' por persona)</p><p><b>Sin desarrollador asignado:</b> '+c.developers.unassigned+'</p><p><b>Cola QA/UAT:</b> '+c.qa.queue+' / '+c.qa.people+' personas ('+c.qa.items_per_person+' por persona)</p>'+Object.entries(c.owner_load).map(([o,v])=>'<p class="muted">'+esc(o)+': '+Object.values(v).reduce((s,n)=>s+n,0)+' activos</p>').join('');const f=a.forecast;document.getElementById('forecast').innerHTML=f.available?'<div class="metric">'+f.probability+'%</div><p><span class="pill '+(f.pace==='En ritmo'?'go':f.pace==='En riesgo'?'warn':'nogo')+'">'+esc(f.pace)+'</span></p><p class="muted">'+f.remaining_days+' días restantes · '+f.completed_percentage+'% completado frente a '+f.elapsed_percentage+'% del tiempo</p>':'<p class="muted">'+esc(f.pace)+'</p>';document.getElementById('operational').innerHTML=a.operational_items.map(x=>'<tr><td>'+issueLink(x)+'<br><span class="muted">'+esc(x.summary)+'</span></td><td>'+esc(x.type)+'</td><td>'+esc(x.status)+'</td><td>'+esc(x.assignee)+'</td><td class="nowrap">'+x.age_hours+' h</td></tr>').join('');document.getElementById('executionrows').innerHTML=te.details.map(x=>'<tr><td>'+issueLink(x)+'</td><td>'+esc(x.test_case_key||'Sin referencia')+'</td><td><span class="pill '+(x.status==='Done'?'go':x.status==='Failed'?'nogo':'warn')+'">'+esc(x.status)+'</span></td><td>'+esc(x.environment)+'</td><td>'+esc(x.qa_executor)+'</td><td><span class="pill '+(x.result==='Passed'?'go':x.result==='Not Run'?'warn':'nogo')+'">'+esc(x.result)+'</span></td><td>'+x.evidence_count+'</td></tr>').join('')||'<tr><td colspan="7" class="muted">Sin Test Execution</td></tr>';document.getElementById('summary').textContent=a.executive_summary;document.getElementById('points').textContent=a.sprint_progress.completed_points+'/'+a.sprint_progress.total_points+' puntos — '+a.sprint_progress.percentage+'%';document.getElementById('pointsbar').style.width=Math.min(a.sprint_progress.percentage,100)+'%';document.getElementById('bugs').innerHTML=Object.entries(a.open_bugs_by_priority).map(([k,v])=>'<span class="pill nogo">'+esc(k)+': '+v+'</span> ').join('')||'<span class="pill go">Sin bugs abiertos</span>';document.getElementById('actions').innerHTML=a.action_items.map(x=>'<li>'+esc(x)+'</li>').join('');document.getElementById('results').classList.remove('hidden')}catch(e){error.textContent=e.message}finally{btn.disabled=false}}
document.addEventListener('DOMContentLoaded',loadSprints);
</script><script>
function renderPM(a,jiraBaseUrl){
 const te=a.test_executions;
 document.getElementById('tefailed').textContent=te.by_result.Failed||0;
 document.getElementById('teblocked').textContent=te.by_result.Blocked||0;
 document.getElementById('tenotrun').textContent=te.by_result['Not Run']||0;
 document.getElementById('executionattention').innerHTML=te.attention_required.map(x=>'<div class="alert '+(x.result==='Not Run'?'':'critical')+'">'+issueLink(x)+' — <b>'+esc(x.result)+'</b> · TC '+esc(x.test_case_key||'sin referencia')+' · Jira: '+esc(x.status)+' · QA: '+esc(x.qa_executor)+'</div>').join('')||'<span class="pill go">Todas las ejecuciones están resueltas</span>';
 document.getElementById('sprintgoal').textContent=a.sprint_goal;
 document.getElementById('health').textContent=a.sprint_health.score+'/100';
 document.getElementById('healthdetail').innerHTML='<span class="pill '+(a.sprint_health.label==='Saludable'?'go':a.sprint_health.label==='En riesgo'?'warn':'nogo')+'>'+esc(a.sprint_health.label)+'</span> · '+a.sprint_health.critical_alerts+' alertas críticas';
 document.getElementById('prioritycount').textContent=a.today_priorities.length;
 document.getElementById('today').innerHTML=a.today_priorities.map(x=>'<div class="alert '+x.level+'"><b>'+x.rank+'.</b> '+(x.url?'<a href="'+esc(x.url)+'" target="_blank"><b>'+esc(x.key)+'</b></a>':'<b>'+esc(x.key)+'</b>')+' — '+esc(x.message)+'</div>').join('')||'<span class="pill go">Sin acciones urgentes</span>';
 document.getElementById('aging').innerHTML=Object.entries(a.stage_aging).map(([stage,x])=>'<p><b>'+esc(stage)+'</b>: '+x.count+' elementos · promedio '+x.average_hours+' h · más antiguo '+esc(x.oldest_key)+' ('+x.oldest_hours+' h)</p>').join('')||'<p class="muted">Sin elementos activos</p>';
 const deps=a.dependencies.filter((x,i,all)=>all.findIndex(y=>y.source===x.source&&y.target===x.target)===i);
 document.getElementById('dependencies').innerHTML=deps.map(x=>'<p>'+(x.source_url?'<a href="'+esc(x.source_url)+'" target="_blank"><b>'+esc(x.source)+'</b></a>':'<b>'+esc(x.source)+'</b>')+' '+esc(x.relationship)+' '+(x.target_url?'<a href="'+esc(x.target_url)+'" target="_blank">'+esc(x.target)+'</a>':esc(x.target))+(x.is_blocking?' <span class="pill nogo">Bloqueo</span>':'')+'</p>').join('')||'<span class="pill go">Sin dependencias registradas</span>';
 document.getElementById('people').innerHTML=a.team_capacity.people.map(x=>'<tr><td><b>'+esc(x.name.replace(/^(QA|Desarrollo) · /,''))+'</b></td><td>'+esc(x.role)+'</td><td>'+x.active_items+'</td><td>'+x.story_points+'</td><td>'+esc(Object.entries(x.stages).map(([k,v])=>k+': '+v).join(' · '))+'</td></tr>').join('')||'<tr><td colspan="5" class="muted">Sin carga asignada</td></tr>';
 document.getElementById('coverage').innerHTML=Object.entries(a.story_coverage).map(([k,v])=>'<tr><td><a href="'+esc(jiraBaseUrl||'')+'/browse/'+esc(k)+'" target="_blank"><b>'+esc(k)+'</b></a><br><span class="muted">'+esc(v.summary)+'</span></td><td><span class="pill warn">'+esc(v.jira_status)+'</span></td><td>'+v.count+'</td><td class="'+v.coverage_risk+'">'+v.coverage_risk+'</td><td><span class="pill '+(v.ready_to_close?'go':'nogo')+'">'+esc(v.delivery_state)+'</span></td></tr>').join('');
}
const sprintGuardFetch=window.fetch.bind(window);
window.fetch=async(...args)=>{const isAnalysis=String(args[0]).includes('/analyze-sprint');if(isAnalysis){args[0]='/analyze-sprint?refresh='+Date.now();args[1]={...(args[1]||{}),cache:'no-store',headers:{...((args[1]||{}).headers||{}),'Cache-Control':'no-cache'}}}const response=await sprintGuardFetch(...args);if(isAnalysis){response.clone().json().then(data=>{if(data.analysis)setTimeout(()=>renderPM(data.analysis,data.jira_base_url),0)}).catch(()=>{})}return response};
</script></body></html>"""

@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return DASHBOARD_HTML


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/sprints")
def available_sprints() -> dict[str, Any]:
    try:
        jira = JiraClient(Settings())
        sprints = jira.list_sprints()
        selected_id = next((sprint.get("id") for sprint in sprints if sprint.get("state") == "active"), None)
        if selected_id is None and sprints:
            selected_id = sprints[0].get("id")
        state_labels = {"active": "Actual", "future": "Próximo", "closed": "Cerrado"}
        return {
            "sprints": [
                {
                    "id": sprint.get("id"),
                    "name": sprint.get("name", ""),
                    "state": sprint.get("state", ""),
                    "label": f"{sprint.get('name', '')} — {state_labels.get(sprint.get('state', ''), 'Sin estado')}",
                    "selected": sprint.get("id") == selected_id,
                }
                for sprint in sprints
            ]
        }
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"No se pudieron cargar los sprints desde Jira ({exc.response.status_code})") from exc


@app.post("/analyze-sprint")
def analyze_sprint(request: AnalyzeRequest) -> dict[str, Any]:
    try:
        cfg = Settings()
        jira = JiraClient(cfg)
        sprint_warning = None
        try:
            sprint = jira.sprint_info(request.sprint_name)
        except httpx.HTTPError:
            sprint = None
            sprint_warning = "No se pudieron leer los datos del sprint; se utilizó el nombre como filtro y la proyección temporal no está disponible"
        jql = build_sprint_jql(cfg.jira_project_key, request.sprint_name, sprint)
        issues, field_ids = jira.search(jql)
        analysis = analyze_coverage(
            issues,
            execution_field_id=field_ids["execution_status"],
            story_points_field_id=field_ids["story_points"],
            qa_assigned_field_id=field_ids["qa_assigned"],
            execution_date_field_id=field_ids["execution_date"],
            jira_base_url=cfg.jira_base_url,
        )
        if sprint_warning:
            analysis["data_warnings"].append(sprint_warning)
        analysis["forecast"] = sprint_forecast(
            sprint,
            analysis["sprint_progress"]["completed_points"],
            analysis["sprint_progress"]["total_points"],
        )
        analysis["sprint_goal"] = (sprint or {}).get("goal") or "Sin objetivo definido en Jira"
        forecast = analysis["forecast"]
        critical_alerts = sum(1 for item in analysis["alerts"] if item["level"] == "critical")
        health_score = 100
        health_score -= min(35, critical_alerts * 10)
        health_score -= min(25, analysis["bug_progress"]["open"] * 5)
        health_score -= min(25, len(analysis["stories_below_target"]) * 8)
        if forecast.get("available"):
            health_score = round((max(0, health_score) + forecast["probability"]) / 2)
        health_score = max(0, min(100, health_score))
        health_label = "Saludable" if health_score >= 75 else "En riesgo" if health_score >= 50 else "Crítico"
        analysis["sprint_health"] = {
            "score": health_score, "label": health_label,
            "critical_alerts": critical_alerts,
            "blocking_dependencies": sum(1 for item in analysis["dependencies"] if item["is_blocking"]),
        }
        analysis["executive_summary"] = (
            f"Objetivo: {analysis['sprint_goal']}. Salud del sprint: {health_label} ({health_score}/100). "
            + analysis["executive_summary"]
        )
        analysis["last_synced_at"] = datetime.now(timezone.utc).isoformat()
        return {
            "sprint": request.sprint_name,
            "jira_base_url": cfg.jira_base_url.rstrip("/"),
            "jira_board_url": cfg.jira_board_url,
            "github_repository_url": cfg.github_repository_url,
            "write_mode": cfg.agent_write_mode,
            "analysis": analysis,
            "recommendation": "Review stories_below_target before closing stories. Jira writes require explicit approval.",
        }
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Jira API error: {exc.response.status_code}") from exc
