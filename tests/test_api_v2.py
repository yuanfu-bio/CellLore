from fastapi.testclient import TestClient

from scdata_agent.interfaces.api import create_app


def test_catalog_and_snapshot_endpoints_are_read_only_and_empty(tmp_path):
    app = create_app(tmp_path / "api_v2.sqlite")
    with TestClient(app) as client:
        assert client.get("/catalog/records").json() == []
        assert client.get("/corpus-snapshots").json() == []
        schema = client.get("/openapi.json").json()
    properties = schema["components"]["schemas"]["CreateRunRequest"]["properties"]
    assert "data_root" in properties
    assert "max_download_bytes" in properties
    assert "evaluation_identifiers" in properties
    assert "approved_license_candidate_ids" in properties
    assert "planner_mode" in properties


def test_planner_mode_defaults_to_deep_so_the_llm_completes_its_analysis():
    """fast races two calls against a 30s budget and degrades to a generic
    template on timeout; deep is what actually lets the model finish."""
    from scdata_agent.interfaces.api import CreateRunRequest

    assert CreateRunRequest(question="x" * 10).planner_mode == "deep"


def test_planner_mode_rejects_values_other_than_fast_or_deep():
    import pytest
    from pydantic import ValidationError

    from scdata_agent.interfaces.api import CreateRunRequest

    with pytest.raises(ValidationError):
        CreateRunRequest(question="x" * 10, planner_mode="thorough")


def test_create_run_rejects_a_bare_run_id_as_the_research_question(tmp_path):
    app = create_app(tmp_path / "reject_run_id.sqlite")

    with TestClient(app) as client:
        response = client.post(
            "/runs", json={"question": "agent_run_48b52065980da09e"}
        )

        assert response.status_code == 422
        assert "cannot be used as a research question" in response.text
        assert client.get("/runs").json() == []


def test_clean_install_serves_the_bundled_react_ui_and_curation_profiles(tmp_path):
    app = create_app(tmp_path / "bundled_ui.sqlite")

    with TestClient(app) as client:
        dashboard = client.get("/")
        profiles = client.get("/curation-profiles")

    assert dashboard.status_code == 200
    assert '<div id="root"></div>' in dashboard.text
    assert "/assets/index-" in dashboard.text
    assert profiles.status_code == 200
    assert [item["profile_id"] for item in profiles.json()] == [
        "paired_atac_rna_human"
    ]
