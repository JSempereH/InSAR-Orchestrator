"""credentials.py: CRUD for encrypted provider credentials (Earthdata/EGMS).
Had zero endpoint-level coverage before this."""


def test_upsert_creates_a_new_credential(client):
    resp = client.put("/api/credentials", json={
        "provider": "earthdata-test-create", "username": "alice", "password": "secret",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "earthdata-test-create"
    assert body["username"] == "alice"
    assert "password" not in body  # never echo the secret back


def test_upsert_updates_an_existing_credential(client):
    client.put("/api/credentials", json={
        "provider": "earthdata-test-update", "username": "alice", "password": "secret1",
    })
    resp = client.put("/api/credentials", json={
        "provider": "earthdata-test-update", "username": "bob", "password": "secret2",
    })
    assert resp.status_code == 200
    assert resp.json()["username"] == "bob"

    # Still just one row for this provider, not a duplicate.
    assert client.get("/api/credentials/earthdata-test-update").json()["username"] == "bob"


def test_get_credential_for_unknown_provider_returns_404(client):
    resp = client.get("/api/credentials/does-not-exist-provider")
    assert resp.status_code == 404


def test_delete_credential(client):
    client.put("/api/credentials", json={
        "provider": "earthdata-test-delete", "username": "alice", "password": "secret",
    })
    resp = client.delete("/api/credentials/earthdata-test-delete")
    assert resp.status_code == 204
    assert client.get("/api/credentials/earthdata-test-delete").status_code == 404


def test_delete_credential_for_unknown_provider_returns_404(client):
    resp = client.delete("/api/credentials/does-not-exist-provider")
    assert resp.status_code == 404
