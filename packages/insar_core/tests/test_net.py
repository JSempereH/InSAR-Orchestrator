import requests

from insar_core.net import request_with_retry, stream_download


class FakeResponse:
    def __init__(self, *, status_code=200, headers=None, chunks=(b"data",), error=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = list(chunks)
        self._error = error

    def raise_for_status(self):
        if self._error is not None:
            raise self._error
        if self.status_code >= 400:
            err = requests.exceptions.HTTPError(response=self)
            raise err

    def iter_content(self, chunk_size):
        return iter(self._chunks)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeRequester:
    """Stands in for `requests`/a `requests.Session`: .get() returns whatever
    response the test queued next, and every call (with its headers) is
    recorded so tests can assert on Range usage across retries."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url, headers=None, stream=True, timeout=600):
        self.calls.append({"url": url, "headers": dict(headers or {})})
        return self._responses.pop(0)


def test_stream_download_writes_full_file_on_first_try(tmp_path):
    dest = tmp_path / "file.tif"
    requester = FakeRequester([
        FakeResponse(status_code=200, headers={"content-length": "8"}, chunks=[b"abcd", b"efgh"]),
    ])

    stream_download("https://example.com/f", dest, session=requester)

    assert dest.read_bytes() == b"abcdefgh"
    assert not dest.with_name(dest.name + ".part").exists()  # renamed away, no leftover


def test_stream_download_reports_progress(tmp_path):
    dest = tmp_path / "file.tif"
    requester = FakeRequester([
        FakeResponse(status_code=200, headers={"content-length": "8"}, chunks=[b"abcd", b"efgh"]),
    ])
    calls = []

    stream_download("https://example.com/f", dest, session=requester, progress_cb=lambda **kw: calls.append(kw))

    assert [c["downloaded_bytes"] for c in calls] == [4, 8]
    assert all(c["total_bytes"] == 8 for c in calls)
    assert all(c["filename"] == "file.tif" for c in calls)  # never the .part name


def test_stream_download_retries_transient_connection_error(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)  # don't actually wait in tests
    dest = tmp_path / "file.tif"

    class FlakyRequester(FakeRequester):
        def get(self, url, headers=None, stream=True, timeout=600):
            self.calls.append({"url": url, "headers": dict(headers or {})})
            if len(self.calls) == 1:
                raise requests.exceptions.ConnectionError("reset by peer")
            return FakeResponse(status_code=200, headers={"content-length": "4"}, chunks=[b"data"])

    requester = FlakyRequester([])
    stream_download("https://example.com/f", dest, session=requester, max_retries=2)

    assert dest.read_bytes() == b"data"
    assert len(requester.calls) == 2


def test_stream_download_does_not_retry_client_errors(tmp_path):
    dest = tmp_path / "file.tif"
    requester = FakeRequester([FakeResponse(status_code=404)])

    try:
        stream_download("https://example.com/f", dest, session=requester, max_retries=3)
        assert False, "expected HTTPError to propagate"
    except requests.exceptions.HTTPError:
        pass

    assert len(requester.calls) == 1  # no retry attempted for a 4xx


def test_stream_download_gives_up_after_max_retries(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    dest = tmp_path / "file.tif"

    class AlwaysFlakyRequester(FakeRequester):
        def get(self, url, headers=None, stream=True, timeout=600):
            self.calls.append({"url": url, "headers": dict(headers or {})})
            raise requests.exceptions.Timeout("too slow")

    requester = AlwaysFlakyRequester([])
    try:
        stream_download("https://example.com/f", dest, session=requester, max_retries=2)
        assert False, "expected Timeout to propagate after exhausting retries"
    except requests.exceptions.Timeout:
        pass

    assert len(requester.calls) == 3  # initial attempt + 2 retries


def test_stream_download_resumes_from_partial_file_via_range(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    dest = tmp_path / "file.tif"
    partial = dest.with_name(dest.name + ".part")
    partial.write_bytes(b"abcd")  # 4 bytes already on disk from a previous attempt

    class ResumingRequester(FakeRequester):
        def get(self, url, headers=None, stream=True, timeout=600):
            self.calls.append({"url": url, "headers": dict(headers or {})})
            if len(self.calls) == 1:
                raise requests.exceptions.ConnectionError("dropped")
            # Second attempt: honors the Range request with 206 + Content-Range.
            return FakeResponse(
                status_code=206,
                headers={"content-range": "bytes 4-7/8"},
                chunks=[b"efgh"],
            )

    requester = ResumingRequester([])
    stream_download("https://example.com/f", dest, session=requester, max_retries=2)

    assert dest.read_bytes() == b"abcdefgh"  # existing bytes preserved, not re-fetched
    assert requester.calls[1]["headers"]["Range"] == "bytes=4-"


def test_request_with_retry_returns_response_on_success(monkeypatch):
    monkeypatch.setattr(
        "insar_core.net.requests.request",
        lambda method, url, **kw: FakeResponse(status_code=200, chunks=[]),
    )
    resp = request_with_retry("GET", "https://example.com/x")
    assert resp.status_code == 200


def test_request_with_retry_retries_transient_errors_then_succeeds(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = []

    def fake_request(method, url, **kw):
        calls.append(url)
        if len(calls) == 1:
            raise requests.exceptions.ConnectionError("reset")
        return FakeResponse(status_code=200, chunks=[])

    monkeypatch.setattr("insar_core.net.requests.request", fake_request)
    resp = request_with_retry("POST", "https://example.com/x", max_retries=2)

    assert resp.status_code == 200
    assert len(calls) == 2


def test_request_with_retry_does_not_retry_4xx(monkeypatch):
    monkeypatch.setattr(
        "insar_core.net.requests.request",
        lambda method, url, **kw: FakeResponse(status_code=400, chunks=[]),
    )
    try:
        request_with_retry("POST", "https://example.com/x", max_retries=3)
        assert False, "expected HTTPError to propagate"
    except requests.exceptions.HTTPError:
        pass


def test_stream_download_restarts_when_server_ignores_range(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    dest = tmp_path / "file.tif"
    partial = dest.with_name(dest.name + ".part")
    partial.write_bytes(b"stale-partial-data")

    class NonResumingRequester(FakeRequester):
        def get(self, url, headers=None, stream=True, timeout=600):
            self.calls.append({"url": url, "headers": dict(headers or {})})
            # Ignores the Range header entirely - plain 200 with the full body.
            return FakeResponse(status_code=200, headers={"content-length": "8"}, chunks=[b"abcdefgh"])

    requester = NonResumingRequester([])
    stream_download("https://example.com/f", dest, session=requester)

    # Old partial content must not have been kept/appended to.
    assert dest.read_bytes() == b"abcdefgh"
