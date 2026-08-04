from fastapi.testclient import TestClient

from scdata_agent.interfaces.api import create_app


def test_app_health_and_empty_runs(tmp_path):
    app = create_app(tmp_path / "api.sqlite")
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/runs").json() == []
        assert 'id="root"' in client.get("/").text
        assert 'id="root"' in client.get("/ui/runs/demo-run").text
