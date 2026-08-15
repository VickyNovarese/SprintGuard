from __future__ import annotations

import argparse
from collections import defaultdict
import re
from typing import Any

import httpx

from app import Settings


SPRINT_PLAN = {
    2: ["PROJ-11", "PROJ-15", "PROJ-16"],
    3: ["PROJ-18", "PROJ-19", "PROJ-20"],
    4: ["PROJ-9", "PROJ-10", "PROJ-12"],
    5: ["PROJ-13", "PROJ-17", "PROJ-21"],
    6: ["PROJ-22", "PROJ-23", "PROJ-24"],
    7: ["PROJ-25", "PROJ-34", "PROJ-35"],
    8: ["PROJ-26", "PROJ-27", "PROJ-29"],
    9: ["PROJ-28", "PROJ-30", "PROJ-31"],
    10: ["PROJ-14", "PROJ-32", "PROJ-33"],
}

TEST_VARIANTS = [
    ("happy path", "Verificar el flujo principal con datos válidos.", "La operación finaliza correctamente."),
    ("validation and negative path", "Verificar validaciones con datos inválidos, vacíos o duplicados.", "El sistema rechaza la operación y muestra un mensaje claro."),
    ("permissions and boundary conditions", "Verificar permisos, límites y acceso con roles no autorizados.", "El sistema protege los datos y aplica las restricciones definidas."),
]


class Organizer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.http = httpx.Client(
            base_url=settings.jira_base_url.rstrip("/"),
            auth=(settings.jira_email, settings.jira_api_token),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=40,
        )

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.http.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None

    def board_id(self) -> int:
        if match := re.search(r"/boards/(\d+)", self.settings.jira_board_url):
            return int(match.group(1))
        data = self.request("GET", "/rest/agile/1.0/board", params={"projectKeyOrId": self.settings.jira_project_key})
        boards = data.get("values", [])
        if not boards:
            raise RuntimeError("No se encontró un board Scrum para el proyecto")
        return int(boards[0]["id"])

    def sprints(self, board_id: int) -> dict[str, int]:
        data = self.request(
            "GET", f"/rest/agile/1.0/board/{board_id}/sprint",
            params={"state": "active,future,closed", "maxResults": 100},
        )
        return {item["name"]: int(item["id"]) for item in data.get("values", [])}

    def ensure_sprints(self, board_id: int, apply: bool) -> dict[str, int]:
        result = self.sprints(board_id)
        for number in SPRINT_PLAN:
            name = f"{self.settings.jira_project_key} Sprint {number}"
            if name not in result and apply:
                created = self.request("POST", "/rest/agile/1.0/sprint", json={"name": name, "originBoardId": board_id})
                result[name] = int(created["id"])
        return result

    def search(self, jql: str) -> list[dict[str, Any]]:
        data = self.request(
            "GET", "/rest/api/3/search/jql",
            params={"jql": jql, "maxResults": 100, "fields": "summary,issuetype,issuelinks"},
        )
        return data.get("issues", [])

    @staticmethod
    def linked_keys(issue: dict[str, Any], issue_type: str) -> set[str]:
        keys = set()
        for link in issue["fields"].get("issuelinks") or []:
            other = link.get("outwardIssue") or link.get("inwardIssue") or {}
            if (other.get("fields") or {}).get("issuetype", {}).get("name") == issue_type:
                keys.add(other["key"])
        return keys

    def create_test_case(self, story: dict[str, Any], index: int) -> str:
        variant, purpose, expected = TEST_VARIANTS[index]
        summary = f"TC - {story['fields']['summary']} - {variant}"
        description = {
            "type": "doc", "version": 1,
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": f"Historia relacionada: {story['key']}"}]},
                {"type": "paragraph", "content": [{"type": "text", "text": f"Objetivo: {purpose}"}]},
                {"type": "paragraph", "content": [{"type": "text", "text": "Precondiciones: usuario disponible y ambiente OrangeHRM operativo."}]},
                {"type": "paragraph", "content": [{"type": "text", "text": "Pasos: preparar los datos, ejecutar el flujo descrito por la historia y registrar evidencia."}]},
                {"type": "paragraph", "content": [{"type": "text", "text": f"Resultado esperado: {expected}"}]},
            ],
        }
        created = self.request("POST", "/rest/api/3/issue", json={"fields": {
            "project": {"key": self.settings.jira_project_key},
            "summary": summary,
            "description": description,
            "issuetype": {"name": "Test Case"},
            "labels": ["sprintguard-generated"],
        }})
        return created["key"]

    def link(self, story_key: str, test_key: str, link_type: str) -> None:
        self.request("POST", "/rest/api/3/issueLink", json={
            "type": {"name": link_type},
            "inwardIssue": {"key": story_key},
            "outwardIssue": {"key": test_key},
        })

    def link_type(self) -> str:
        types = self.request("GET", "/rest/api/3/issueLinkType").get("issueLinkTypes", [])
        preferred = next((item for item in types if item["name"].casefold() in {"relates", "connects"}), None)
        if not preferred:
            preferred = next((item for item in types if "relat" in item["name"].casefold() or "connect" in item["name"].casefold()), None)
        if not preferred:
            raise RuntimeError("No se encontró un tipo de vínculo Relates/Connects en Jira")
        return preferred["name"]

    def move(self, sprint_id: int, issue_keys: set[str]) -> None:
        if issue_keys:
            self.request("POST", f"/rest/agile/1.0/sprint/{sprint_id}/issue", json={"issues": sorted(issue_keys)})

    def run(self, apply: bool) -> None:
        planned_keys = [key for keys in SPRINT_PLAN.values() for key in keys]
        stories = self.search(f"key in ({','.join(planned_keys)})")
        story_by_key = {item["key"]: item for item in stories}
        missing = sorted(set(planned_keys) - set(story_by_key))
        if missing:
            raise RuntimeError(f"No se encontraron estas historias: {', '.join(missing)}")

        all_tests = self.search(f'project = {self.settings.jira_project_key} AND issuetype = "Test Case"')
        all_bugs = self.search(f'project = {self.settings.jira_project_key} AND issuetype = Bug')
        tests_by_story: defaultdict[str, set[str]] = defaultdict(set)
        bugs_by_story: defaultdict[str, set[str]] = defaultdict(set)
        for test in all_tests:
            for story_key in self.linked_keys(test, "Story"):
                tests_by_story[story_key].add(test["key"])
        for bug in all_bugs:
            for story_key in self.linked_keys(bug, "Story"):
                bugs_by_story[story_key].add(bug["key"])

        print("\nPLAN SPRINTGUARD")
        total_missing = 0
        for number, story_keys in SPRINT_PLAN.items():
            print(f"\nSprint {number}: {', '.join(story_keys)}")
            for key in story_keys:
                missing_tests = max(0, 3 - len(tests_by_story[key]))
                total_missing += missing_tests
                print(f"  {key}: {len(tests_by_story[key])} TC existentes, {missing_tests} por crear, {len(bugs_by_story[key])} bugs vinculados")
        print(f"\nSe crearán {total_missing} Test Cases. Los elementos no incluidos permanecerán en su sprint actual.")
        if not apply:
            confirmation = input("\nEscribe ORGANIZAR para aplicar los cambios (Enter cancela): ").strip()
            if confirmation != "ORGANIZAR":
                print("Simulación finalizada sin modificar Jira.")
                return

        board_id = self.board_id()
        sprint_ids = self.ensure_sprints(board_id, apply=True)
        relation = self.link_type()
        for number, story_keys in SPRINT_PLAN.items():
            destination = sprint_ids[f"{self.settings.jira_project_key} Sprint {number}"]
            to_move = set(story_keys)
            for story_key in story_keys:
                story = story_by_key[story_key]
                existing = tests_by_story[story_key]
                to_move.update(existing)
                to_move.update(bugs_by_story[story_key])
                for index in range(len(existing), 3):
                    test_key = self.create_test_case(story, index)
                    self.link(story_key, test_key, relation)
                    to_move.add(test_key)
                    print(f"Creado {test_key} y vinculado con {story_key}")
            self.move(destination, to_move)
            print(f"Sprint {number}: {len(to_move)} elementos organizados")
        print("\nOrganización completada. Actualiza el backlog y ejecuta SprintGuard para verificar la cobertura.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Organiza Sprint 2-10 con 3 historias y 3 Test Cases por historia.")
    parser.add_argument("--apply", action="store_true", help="Aplica sin pedir la palabra de confirmación.")
    args = parser.parse_args()
    Organizer(Settings()).run(apply=args.apply)


if __name__ == "__main__":
    main()
