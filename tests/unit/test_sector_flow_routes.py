from unittest.mock import patch

from fastapi.testclient import TestClient

from src.api.main import app


AUTH_HEADERS = {"X-Access-Password": "Lwb1397111398"}
client = TestClient(app)


def test_manual_sector_flow_fetch_uses_run_fetch():
    with patch("src.api.routes.sector_flow.SectorFlowService") as MockService:
        service = MockService.return_value
        service.run_fetch.return_value = {
            "success": True,
            "status": "success",
            "saved_count": 2,
            "fetched_count": 2,
            "run_id": 1,
            "error_message": None,
        }

        response = client.post("/api/sector-flow/fetch", headers=AUTH_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    service.run_fetch.assert_called_once_with(trigger="manual")


def test_sector_flow_fetch_status_endpoint():
    with patch("src.api.routes.sector_flow.SectorFlowService") as MockService:
        service = MockService.return_value
        service.get_fetch_status.return_value = {
            "latest_run": None,
            "latest_data_date": None,
            "today_data_count": 0,
            "displaying_stale_data": False,
        }

        response = client.get("/api/sector-flow/fetch-status", headers=AUTH_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["today_data_count"] == 0
