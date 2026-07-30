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
