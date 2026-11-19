import pytest
from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def client():
    """Full app in dry-run mode. A very long monitor interval keeps the background loop quiet,
    so tests drive the monitor by hand."""
    settings = Settings(dry_run=True, monitor_interval_s=3600)
    with TestClient(create_app(settings)) as c:
        yield c


@pytest.fixture
def orch(client):
    return client.app.state.orch
