"""Project CRUD not already covered by test_storage.py (create)."""


def _project_payload(**overrides):
    payload = {
        "name": "Test Project",
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]},
        "date_start": "2023-01-01",
        "date_end": "2023-06-01",
    }
    payload.update(overrides)
    return payload


def test_list_projects_includes_created_project(client):
    created = client.post("/api/projects", json=_project_payload(name="Listed Project")).json()

    resp = client.get("/api/projects")
    assert resp.status_code == 200
    assert any(p["id"] == created["id"] for p in resp.json())


def test_get_project_by_id(client):
    created = client.post("/api/projects", json=_project_payload(name="Fetched Project")).json()

    resp = client.get(f"/api/projects/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Fetched Project"


def test_get_nonexistent_project_returns_404(client):
    resp = client.get("/api/projects/does-not-exist")
    assert resp.status_code == 404


def test_delete_project_removes_it(client):
    created = client.post("/api/projects", json=_project_payload(name="Doomed Project")).json()

    resp = client.delete(f"/api/projects/{created['id']}")
    assert resp.status_code == 200

    assert client.get(f"/api/projects/{created['id']}").status_code == 404


def test_delete_nonexistent_project_returns_404(client):
    resp = client.delete("/api/projects/does-not-exist")
    assert resp.status_code == 404


def test_download_summary_for_project_with_no_jobs(client):
    created = client.post("/api/projects", json=_project_payload(name="Summary Project")).json()

    resp = client.get(f"/api/projects/{created['id']}/download-summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["storage_path"] == created["storage_path"]
    assert body["total_jobs"] == 0
    assert body["downloaded_jobs"] == 0


def test_download_summary_for_nonexistent_project_returns_404(client):
    resp = client.get("/api/projects/does-not-exist/download-summary")
    assert resp.status_code == 404
