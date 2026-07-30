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
