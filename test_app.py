from datetime import datetime, timezone

from app import adf_text, age_hours, analyze_coverage, propose_owners, sprint_forecast


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
    assert len(result["alerts"]) == 1
    assert result["team_capacity"]["qa"]["queue"] == 1


def test_age_and_sprint_forecast():
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    assert age_hours("2026-08-14T00:00:00Z", now) == 24
    forecast = sprint_forecast({"startDate": "2026-08-10T00:00:00Z", "endDate": "2026-08-22T00:00:00Z"}, 20, 40, now)
    assert forecast["available"] is True
    assert forecast["remaining_days"] == 7.0
    assert forecast["probability"] > 0
