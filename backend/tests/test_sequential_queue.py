"""The generic engine shared by download_queue.py, slc_download_queue.py and
egms_download_queue.py."""
import threading
import time

from app.services import download_state
from app.services.sequential_queue import SequentialQueue


def _wait_until_idle(queue, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not queue.get_state()["active"]:
            return
        time.sleep(0.01)
    raise AssertionError("queue never became idle")


def test_start_processes_all_items_in_order():
    queue = SequentialQueue(name="test", item_key=lambda item: item["id"])
    processed = []

    def download_fn(item):
        processed.append(item["id"])

    queue.start([{"id": "a"}, {"id": "b"}, {"id": "c"}], download_fn)
    _wait_until_idle(queue)

    assert processed == ["a", "b", "c"]
    state = queue.get_state()
    assert state["done"] == 3
    assert state["total"] == 3
    assert not state["active"]


def test_get_state_reflects_progress_via_download_state():
    queue = SequentialQueue(name="test", item_key=lambda item: item["id"])
    started = threading.Event()
    release = threading.Event()

    def download_fn(item):
        download_state.update(item["id"], status="running", pct=42)
        started.set()
        release.wait(timeout=2)

    queue.start([{"id": "job-x"}], download_fn)
    assert started.wait(timeout=2)

    state = queue.get_state()
    assert state["active"] is True
    assert state["current_item"] == {"id": "job-x"}
    assert state["current_progress"]["pct"] == 42

    release.set()
    _wait_until_idle(queue)


def test_a_failing_item_is_marked_error_and_the_queue_continues():
    queue = SequentialQueue(name="test", item_key=lambda item: item["id"])
    processed = []

    def download_fn(item):
        if item["id"] == "bad":
            raise RuntimeError("boom")
        processed.append(item["id"])

    queue.start([{"id": "bad"}, {"id": "good"}], download_fn)
    _wait_until_idle(queue)

    assert processed == ["good"]
    assert download_state.get("bad")["status"] == "error"
    assert download_state.get("good")["status"] == "done"


def test_cancel_clears_pending_but_lets_current_item_finish():
    queue = SequentialQueue(name="test", item_key=lambda item: item["id"])
    started = threading.Event()
    release = threading.Event()
    processed = []

    def download_fn(item):
        if item["id"] == "first":
            started.set()
            release.wait(timeout=2)
        processed.append(item["id"])

    queue.start([{"id": "first"}, {"id": "second"}], download_fn)
    assert started.wait(timeout=2)

    queue.cancel()
    state = queue.get_state()
    assert state["pending_count"] == 0

    release.set()
    _wait_until_idle(queue)

    assert processed == ["first"]  # "second" was dropped by cancel, never ran


def test_enqueue_appends_without_disturbing_pending_items():
    queue = SequentialQueue(name="test", item_key=lambda item: item["id"])
    started = threading.Event()
    release = threading.Event()
    processed = []

    def download_fn(item):
        if item["id"] == "first":
            started.set()
            release.wait(timeout=2)
        processed.append(item["id"])

    queue.start([{"id": "first"}], download_fn)
    assert started.wait(timeout=2)

    queue.enqueue([{"id": "second"}], download_fn)
    assert queue.get_state()["pending_count"] == 1

    release.set()
    _wait_until_idle(queue)
    assert processed == ["first", "second"]


def test_enqueue_skips_items_already_pending_or_in_flight():
    queue = SequentialQueue(name="test", item_key=lambda item: item["id"])
    started = threading.Event()
    release = threading.Event()

    def download_fn(item):
        if item["id"] == "first":
            started.set()
            release.wait(timeout=2)

    queue.start([{"id": "first"}, {"id": "second"}], download_fn)
    assert started.wait(timeout=2)

    queue.enqueue([{"id": "first"}, {"id": "second"}], download_fn)  # both duplicates
    assert queue.get_state()["pending_count"] == 1  # still just "second", nothing added

    release.set()
    _wait_until_idle(queue)
