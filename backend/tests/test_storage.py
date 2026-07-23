"""Per-project storage destination: disk listing + project creation wiring."""


def _project_payload(**overrides):
    payload = {
        "name": "Test Project",
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]},
        "date_start": "2023-01-01",
        "date_end": "2023-06-01",
    }
    payload.update(overrides)
    return payload


def test_list_storage_targets_includes_app_default(client):
    resp = client.get("/api/storage/targets")
    assert resp.status_code == 200
    targets = resp.json()

    default = next(t for t in targets if t["mountpoint"] is None)
    assert default["writable"] is True
    assert default["free_gb"] >= 0


def test_create_project_without_mountpoint_uses_app_default(client):
    resp = client.post("/api/projects", json=_project_payload())
    assert resp.status_code == 200
    assert resp.json()["storage_path"] is None


def test_create_project_with_mountpoint_resolves_and_creates_folder(client, tmp_path):
    resp = client.post("/api/projects", json=_project_payload(
        name="Disk Project",
        storage_mountpoint=str(tmp_path),
    ))
    assert resp.status_code == 200

    expected = tmp_path / "insar-orchestrator" / "disk-project"
    assert resp.json()["storage_path"] == str(expected)
    assert expected.is_dir()


def test_create_project_with_unwritable_mountpoint_returns_400(client, tmp_path):
    readonly_dir = tmp_path / "readonly"
    readonly_dir.mkdir()
    readonly_dir.chmod(0o500)  # r-x: can't create a subdirectory inside it

    try:
        resp = client.post("/api/projects", json=_project_payload(
            name="Bad Project",
            storage_mountpoint=str(readonly_dir),
        ))
        assert resp.status_code == 400
    finally:
        readonly_dir.chmod(0o700)
