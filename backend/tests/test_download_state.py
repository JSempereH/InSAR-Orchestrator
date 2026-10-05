from app.services import download_state


def _reset():
    download_state._progress.clear()
    download_state._updated_at.clear()


def test_update_then_get_roundtrips_fields():
    _reset()
    download_state.update("job-1", status="running", pct=50)
    assert download_state.get("job-1") == {"status": "running", "pct": 50}


def test_get_for_unknown_id_returns_empty_dict():
    _reset()
    assert download_state.get("does-not-exist") == {}


def test_clear_removes_the_entry():
    _reset()
    download_state.update("job-1", status="done")
    download_state.clear("job-1")
    assert download_state.get("job-1") == {}
    assert "job-1" not in download_state._updated_at


def test_stale_entries_are_pruned_on_the_next_update():
    _reset()
    download_state.update("old-job", status="done")

    # Simulate the retention window having elapsed since that update.
    download_state._updated_at["old-job"] -= download_state._RETENTION_S + 1

    download_state.update("new-job", status="running")  # triggers a prune sweep

    assert download_state.get("old-job") == {}
    assert "old-job" not in download_state._updated_at
    assert download_state.get("new-job") == {"status": "running"}


def test_fresh_entries_are_not_pruned():
    _reset()
    download_state.update("job-1", status="running")
    download_state.update("job-2", status="running")  # triggers a prune sweep
    assert download_state.get("job-1") == {"status": "running"}
