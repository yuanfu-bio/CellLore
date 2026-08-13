from fastapi.testclient import TestClient

from celllore import __version__
from celllore.interfaces.api import create_app


def test_app_health_and_empty_runs(tmp_path):
    app = create_app(tmp_path / "api.sqlite")
    assert app.version == __version__
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/runs").json() == []
        assert client.get("/").status_code == 200
