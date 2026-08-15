from datetime import datetime, timezone

from app import adf_text, age_hours, analyze_coverage, propose_owners, sprint_forecast
from organize_sprints import Organizer, SPRINT_PLAN


def test_flags_story_without_test_cases():
    issues = [{
        "key": "PROJ-1",
        "fields": {
            "summary": "Login",
            "issuetype": {"name": "Story"},
            "status": {"name": "To Do"},
            "issuelinks": [],
            "customfield_10016": 5,
        },
    }]
    result = analyze_coverage(issues)
    assert result["stories_without_tests"] == ["PROJ-1"]
    assert result["points_by_type"]["Story"] == 5
    assert result["release_recommendation"] == "NO-GO"


def test_go_when_story_has_three_tests_and_no_failures():
    story = {"key": "PROJ-1", "fields": {"summary": "Login", "issuetype": {"name": "Story"}, "status": {"name": "To Do"}, "issuelinks": [], "customfield_10016": 5}}
    tests = []
    for n in range(2, 5):
        key = f"PROJ-{n}"
        story["fields"]["issuelinks"].append({"outwardIssue": {"key": key}})
        tests.append({"key": key, "fields": {"summary": key, "issuetype": {"name": "Test Case"}, "status": {"name": "Done"}, "issuelinks": [{"inwardIssue": {"key": "PROJ-1"}}], "customfield_10016": 1, "customfield_10097": {"value": "Passed"}}})
    result = analyze_coverage([story, *tests])
    assert result["release_recommendation"] == "GO"
    assert result["execution_progress"]["pass_rate"] == 100.0
    assert result["stories_ready_to_close"] == ["PROJ-1"]


def test_counts_link_when_it_only_exists_on_test_case():
    story = {"key": "PROJ-1", "fields": {"summary": "Login", "issuetype": {"name": "Story"}, "status": {"name": "To Do"}, "issuelinks": [], "customfield_10016": 5}}
    test_case = {"key": "PROJ-2", "fields": {"summary": "Valid login", "issuetype": {"name": "Test Case"}, "status": {"name": "Done"}, "issuelinks": [{"inwardIssue": {"key": "PROJ-1"}}], "customfield_12345": {"value": "Passed"}}}

    result = analyze_coverage(
        [story, test_case],
        execution_field_id="customfield_12345",
    )

    assert result["story_coverage"]["PROJ-1"]["test_cases"] == ["PROJ-2"]
    assert result["story_coverage"]["PROJ-1"]["jira_status"] == "Done"
    assert result["execution_distribution"] == {"Passed": 1}
    assert result["execution_progress"] == {"approved": 1, "total": 1, "pass_rate": 100.0}


def test_reports_missing_execution_field():
    test_case = {"key": "PROJ-2", "fields": {"summary": "Valid login", "issuetype": {"name": "Test Case"}, "status": {"name": "To Do"}, "issuelinks": []}}
    result = analyze_coverage([test_case], execution_field_id=None)
    assert result["execution_distribution"] == {"Not Run": 1}
    assert "No se encontró el campo Execution Status en Jira" in result["data_warnings"]


def test_builds_sprint_report_and_recommended_actions():
    story = {"key": "PROJ-1", "fields": {"summary": "Login", "issuetype": {"name": "Story"}, "status": {"name": "Done"}, "issuelinks": [], "customfield_10016": 5}}
    failed_test = {"key": "PROJ-2", "fields": {"summary": "Invalid login", "issuetype": {"name": "Test Case"}, "status": {"name": "In Progress"}, "issuelinks": [{"inwardIssue": {"key": "PROJ-1"}}], "customfield_10016": 1, "customfield_10097": {"value": "Failed"}}}
    bug = {"key": "PROJ-3", "fields": {"summary": "Login error", "issuetype": {"name": "Bug"}, "status": {"name": "To Do"}, "priority": {"name": "Highest"}, "issuelinks": [], "customfield_10016": 2}}

    result = analyze_coverage([story, failed_test, bug])

    assert result["sprint_progress"] == {"completed_points": 5.0, "total_points": 8.0, "percentage": 62.5}
    assert result["open_bugs_by_priority"] == {"Highest": 1}
    assert result["release_recommendation"] == "NO-GO"
    assert len(result["action_items"]) >= 2
    assert "avance por story points" in result["executive_summary"]


def test_extracts_text_from_jira_description():
    description = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Objetivo"}]}, {"type": "paragraph", "content": [{"type": "text", "text": "Paso 1"}]}]}
    assert adf_text(description) == "Objetivo\nPaso 1"


def test_sprint_plan_has_three_unique_stories_per_sprint():
    keys = [key for stories in SPRINT_PLAN.values() for key in stories]
    assert all(len(stories) == 3 for stories in SPRINT_PLAN.values())
    assert len(keys) == len(set(keys)) == 27


def test_organizer_finds_linked_test_cases():
    issue = {"fields": {"issuelinks": [{"outwardIssue": {"key": "PROJ-80", "fields": {"issuetype": {"name": "Test Case"}}}}]}}
    assert Organizer.linked_keys(issue, "Test Case") == {"PROJ-80"}


def test_assigns_security_bug_to_specialists():
    developer, qa_owner = propose_owners(
        "ESS user accesses Admin module",
        "Restricted user can bypass role permissions.",
    )
    assert developer == "Camila Torres"
    assert qa_owner == "Tomás Herrera"


def test_assigns_visual_bug_to_frontend_team():
    developer, qa_owner = propose_owners("Save button overlaps form on mobile")
    assert developer == "Lucía Fernández"
    assert qa_owner == "Valentina Silva"


def test_assigns_logout_bug_to_authentication_specialist():
    developer, qa_owner = propose_owners(
        "Protected page remains accessible after logout",
        "The authentication session is not invalidated.",
    )
    assert developer == "Diego Rojas"
    assert qa_owner == "Tomás Herrera"


def test_in_progress_means_development_not_testing():
    test_case = {"key": "PROJ-2", "fields": {"summary": "Valid login", "issuetype": {"name": "Test Case"}, "status": {"name": "In Progress"}, "issuelinks": [], "customfield_10097": {"value": "Not Run"}}}
    result = analyze_coverage([test_case])
    assert result["test_case_workflow"]["development"] == 1
    assert result["test_case_workflow"].get("testing", 0) == 0


def test_review_means_testing_in_progress():
    test_case = {"key": "PROJ-2", "fields": {"summary": "Valid login", "issuetype": {"name": "Test Case"}, "status": {"name": "Review"}, "issuelinks": [], "customfield_10097": {"value": "Not Run"}}}
    result = analyze_coverage([test_case])
    assert result["test_case_workflow"]["testing"] == 1
    assert result["release_recommendation"] == "NO-GO"


def test_ready_for_qa_is_waiting_not_active_testing():
    test_case = {"key": "PROJ-2", "fields": {"summary": "Valid login", "issuetype": {"name": "Test Case"}, "status": {"name": "Ready for QA"}, "issuelinks": [], "customfield_10097": {"value": "Not Run"}}}
    result = analyze_coverage([test_case])
    assert result["test_case_workflow"]["ready_for_qa"] == 1
    assert result["test_case_workflow"].get("testing", 0) == 0
    assert "Existen Test Cases desarrollados esperando QA" in result["risk_reasons"]


def test_ready_for_uat_is_not_final_approval():
    test_case = {"key": "PROJ-2", "fields": {"summary": "Valid login", "issuetype": {"name": "Test Case"}, "status": {"name": "Ready for UAT"}, "issuelinks": [], "customfield_10097": {"value": "Passed"}}}
    result = analyze_coverage([test_case])
    assert result["test_case_workflow"]["ready_for_uat"] == 1
    assert result["execution_progress"]["approved"] == 0
    assert result["execution_progress"]["pass_rate"] == 0.0
    assert result["release_recommendation"] == "NO-GO"


def test_final_pass_rate_uses_all_test_cases():
    passed = {"key": "PROJ-1", "fields": {"summary": "Passed", "issuetype": {"name": "Test Case"}, "status": {"name": "Done"}, "issuelinks": [], "customfield_10097": {"value": "Passed"}}}
    waiting = {"key": "PROJ-2", "fields": {"summary": "Waiting", "issuetype": {"name": "Test Case"}, "status": {"name": "Ready for UAT"}, "issuelinks": [], "customfield_10097": {"value": "Passed"}}}
    result = analyze_coverage([passed, waiting])
    assert result["execution_progress"]["pass_rate"] == 50.0


def test_bug_in_ready_for_qa_is_included_in_delivery_workflow():
    bug = {"key": "PROJ-3", "fields": {"summary": "Login error", "issuetype": {"name": "Bug"}, "status": {"name": "Ready for QA"}, "priority": {"name": "High"}, "issuelinks": []}}
    result = analyze_coverage([bug])
    assert result["delivery_workflow"]["ready_for_qa"] == {"test_cases": 0, "bugs": 1, "total": 1}


def test_bug_in_in_review_is_counted_as_testing():
    bug = {"key": "PROJ-36", "fields": {"summary": "Unauthorized access", "issuetype": {"name": "Bug"}, "status": {"name": "In Review"}, "priority": {"name": "Highest"}, "issuelinks": []}}
    result = analyze_coverage([bug])
    assert result["delivery_workflow"]["testing"] == {"test_cases": 0, "bugs": 1, "total": 1}


def test_failed_workflow_column_is_reported_as_failed():
    test_case = {"key": "PROJ-2", "fields": {"summary": "Invalid login", "issuetype": {"name": "Test Case"}, "status": {"name": "Failed"}, "issuelinks": [], "customfield_10097": {"value": "Failed"}}}
    result = analyze_coverage([test_case])
    assert result["test_case_workflow"]["failed"] == 1
    assert "Existen Test Cases en Failed esperando corrección" in result["risk_reasons"]


def test_in_progress_after_failure_is_development():
    test_case = {"key": "PROJ-2", "fields": {"summary": "Invalid login", "issuetype": {"name": "Test Case"}, "status": {"name": "In Progress"}, "issuelinks": [], "customfield_10097": {"value": "Failed"}}}
    result = analyze_coverage([test_case])
    assert result["test_case_workflow"]["development"] == 1


def test_done_without_passed_is_not_approved():
    test_case = {"key": "PROJ-2", "fields": {"summary": "Valid login", "issuetype": {"name": "Test Case"}, "status": {"name": "Done"}, "issuelinks": [], "customfield_10097": {"value": "Failed"}}}
    result = analyze_coverage([test_case])
    assert result["test_case_workflow"]["workflow_anomaly"] == 1
    assert result["execution_progress"]["approved"] == 0
    assert result["release_recommendation"] == "NO-GO"
    assert "Revisar los Test Cases en Done sin Passed y moverlos a Failed o Review según el último resultado." in result["action_items"]


def test_generates_operational_alerts_and_links():
    now = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
    bug = {"key": "PROJ-36", "fields": {"summary": "Unauthorized access", "issuetype": {"name": "Bug"}, "status": {"name": "Ready for QA"}, "priority": {"name": "Highest"}, "assignee": {"displayName": "Camila Torres"}, "statuscategorychangedate": "2026-08-12T08:00:00Z", "issuelinks": []}}
    result = analyze_coverage([bug], jira_base_url="https://example.atlassian.net", now=now)
    assert result["operational_items"][0]["age_hours"] == 52
    assert result["operational_items"][0]["url"] == "https://example.atlassian.net/browse/PROJ-36"
    assert len(result["alerts"]) == 2
    assert result["team_capacity"]["qa"]["queue"] == 1
    assert result["team_capacity"]["developers"]["active_total"] == 1
    assert result["team_capacity"]["developers"]["queue"] == 0
    assert result["today_priorities"][0]["key"] == "PROJ-36"
    assert result["stage_aging"]["ready_for_qa"]["oldest_hours"] == 52
    assert result["team_capacity"]["people"][0]["active_items"] == 1


def test_age_and_sprint_forecast():
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    assert age_hours("2026-08-14T00:00:00Z", now) == 24
    forecast = sprint_forecast({"startDate": "2026-08-10T00:00:00Z", "endDate": "2026-08-22T00:00:00Z"}, 20, 40, now)
    assert forecast["available"] is True
    assert forecast["remaining_days"] == 7.0
    assert forecast["probability"] > 0


def test_shows_developer_and_qa_assigned_separately():
    test_case = {"key": "PROJ-57", "fields": {"summary": "Invalid login", "issuetype": {"name": "Test Case"}, "status": {"name": "In Review"}, "assignee": {"displayName": "Diego Rojas"}, "customfield_qa": {"displayName": "Valentina Silva"}, "issuelinks": [], "customfield_10097": {"value": "Not Run"}}}
    result = analyze_coverage([test_case], qa_assigned_field_id="customfield_qa")
    item = result["operational_items"][0]
    assert item["developer_assigned"] == "Diego Rojas"
    assert item["qa_assigned"] == "Valentina Silva"
    assert item["assignee"] == "Dev: Diego Rojas · QA: Valentina Silva"
    assert result["team_capacity"]["qa_load"]["Valentina Silva"]["testing"] == 1
    assert not any("sin QA Assigned" in alert["message"] for alert in result["alerts"])


def test_separates_active_development_responsibility_from_development_queue():
    in_development = {"key": "PROJ-1", "fields": {"summary": "Build", "issuetype": {"name": "Test Case"}, "status": {"name": "In Progress"}, "assignee": {"displayName": "Clara"}, "issuelinks": []}}
    in_testing = {"key": "PROJ-2", "fields": {"summary": "Test", "issuetype": {"name": "Test Case"}, "status": {"name": "In Review"}, "assignee": {"displayName": "Ramón"}, "issuelinks": []}}
    result = analyze_coverage([in_development, in_testing])
    capacity = result["team_capacity"]["developers"]
    assert capacity["active_total"] == 2
    assert capacity["queue"] == 1
    assert capacity["unassigned"] == 0


def test_analyzes_test_executions_and_evidence():
    test_case = {"key": "PROJ-52", "fields": {"summary": "Valid login", "issuetype": {"name": "Test Case"}, "status": {"name": "Done"}, "issuelinks": [], "customfield_10097": {"value": "Passed"}}}
    execution = {"key": "PROJ-70", "fields": {"summary": "TE - QA - PROJ-52 - Valid login", "description": "Test Case relacionado: PROJ-52\nAmbiente: QA", "issuetype": {"name": "Test Execution"}, "status": {"name": "To Do"}, "assignee": None, "attachment": [{"id": "1"}], "created": "2026-08-14T12:00:00Z", "issuelinks": [], "customfield_10097": {"value": "Passed"}, "customfield_qa": {"displayName": "Valentina Silva"}}}
    result = analyze_coverage([test_case, execution], qa_assigned_field_id="customfield_qa", jira_base_url="https://example.atlassian.net")
    data = result["test_executions"]
    assert data["total"] == 1
    assert data["by_result"] == {"Passed": 1}
    assert data["by_workflow"] == {"To Do": 1}
    assert data["by_qa"] == {"Valentina Silva": 1}
    assert data["by_environment"] == {"QA": 1}
    assert data["details"][0]["evidence_count"] == 1
    assert data["latest_by_test_case"]["PROJ-52"]["key"] == "PROJ-70"
    assert data["test_cases_without_execution"] == []
    assert data["incomplete_workflow"] == ["PROJ-70"]


def test_ignores_executions_from_other_sprints():
    selected_test = {"key": "PROJ-52", "fields": {"summary": "Selected", "issuetype": {"name": "Test Case"}, "status": {"name": "To Do"}, "issuelinks": []}}
    historical_execution = {"key": "PROJ-90", "fields": {"summary": "TE - QA - PROJ-99", "description": "Ambiente: QA", "issuetype": {"name": "Test Execution"}, "status": {"name": "Done"}, "assignee": {"displayName": "Mercedes"}, "attachment": [], "created": "2026-08-10T10:00:00Z", "issuelinks": [], "customfield_10097": {"value": "Passed"}}}
    result = analyze_coverage([selected_test, historical_execution])
    assert result["test_executions"]["total"] == 0
    assert result["test_executions"]["by_result"] == {}
    assert result["test_executions"]["test_cases_without_execution"] == ["PROJ-52"]


def test_failed_execution_requires_evidence_and_bug():
    test_case = {"key": "PROJ-52", "fields": {"summary": "Valid login", "issuetype": {"name": "Test Case"}, "status": {"name": "In Review"}, "issuelinks": []}}
    execution = {"key": "PROJ-70", "fields": {"summary": "TE - QA - PROJ-52", "description": "Ambiente: UAT", "issuetype": {"name": "Test Execution"}, "status": {"name": "To Do"}, "attachment": [], "created": "2026-08-14T12:00:00Z", "issuelinks": [], "customfield_10097": {"value": "Failed"}}}
    result = analyze_coverage([test_case, execution])
    assert result["test_executions"]["failed_without_evidence"] == ["PROJ-70"]
    assert result["test_executions"]["failed_without_bug"] == ["PROJ-70"]
    assert result["test_executions"]["failed"] == ["PROJ-70"]
    assert result["test_executions"]["blocked"] == []
    assert result["test_executions"]["pending"] == []
    assert result["test_executions"]["attention_required"][0]["key"] == "PROJ-70"
    assert result["release_recommendation"] == "NO-GO"


def test_test_execution_uses_assignee_as_qa_executor():
    test_case = {"key": "PROJ-53", "fields": {"summary": "Logout", "issuetype": {"name": "Test Case"}, "status": {"name": "Done"}, "issuelinks": []}}
    execution = {"key": "PROJ-71", "fields": {"summary": "TE - QA - PROJ-53", "description": "Ambiente: QA", "issuetype": {"name": "Test Execution"}, "status": {"name": "Done"}, "assignee": {"displayName": "Mercedes"}, "attachment": [], "created": "2026-08-15T10:00:00Z", "issuelinks": [], "customfield_10097": {"value": "Passed"}, "customfield_qa": None}}
    result = analyze_coverage([test_case, execution], qa_assigned_field_id="customfield_qa")
    detail = result["test_executions"]["details"][0]
    assert detail["qa_executor"] == "Mercedes"
    assert result["test_executions"]["by_qa"] == {"Mercedes": 1}
    assert not any("PROJ-71 no tiene QA" in alert["message"] for alert in result["alerts"])
