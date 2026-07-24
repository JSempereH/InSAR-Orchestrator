from pathlib import Path

import pytest

from insar_core.adapters.egms import API_BASE, EGMSAdapter, EGMSServiceKey
from insar_core.models.egms import EGMSSearchParams
from insar_core.models.scene import AOI

_TEST_KEY = EGMSServiceKey(
    client_id="test-client",
    user_id="test-user",
    token_uri="https://land.copernicus.eu/@@oauth2-token",
    private_key="not-a-real-key",  # only used if jwt.encode is not stubbed
)


def _aoi() -> AOI:
    return AOI.from_bbox(3.0, 49.0, 3.6, 49.5)


class _FakeResponse:
    def __init__(self, json_data=None, headers=None):
        self._json = json_data or {}
        self.headers = headers or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


@pytest.fixture(autouse=True)
def _stub_jwt_encode(monkeypatch):
    monkeypatch.setattr("insar_core.adapters.egms.jwt.encode", lambda *a, **k: "signed-jwt")


def _install_post(monkeypatch, calls, token_payload=None, search_payload=None):
    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url == _TEST_KEY.token_uri:
            return _FakeResponse(token_payload or {"access_token": "tok-abc", "expires_in": 3600})
        if url == f"{API_BASE}/search":
            return _FakeResponse(search_payload or {"id": "q1", "hits": []})
        raise AssertionError(f"Unexpected POST to {url}")

    monkeypatch.setattr("insar_core.adapters.egms.requests.post", fake_post)


def test_access_token_is_requested_via_jwt_bearer_and_cached(monkeypatch):
    calls = []
    _install_post(monkeypatch, calls)
    adapter = EGMSAdapter(_TEST_KEY)

    token1 = adapter._get_access_token()
    token2 = adapter._get_access_token()

    assert token1 == token2 == "tok-abc"
    # Only one token request despite two calls: caching works.
    token_calls = [c for c in calls if c[0] == _TEST_KEY.token_uri]
    assert len(token_calls) == 1

    _, kwargs = token_calls[0]
    assert kwargs["data"]["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    assert kwargs["data"]["assertion"] == "signed-jwt"


def test_search_sends_aoi_level_and_release(monkeypatch):
    calls = []
    _install_post(
        monkeypatch, calls,
        search_payload={
            "id": "q42",
            "hits": [
                {"filename": "EGMS_L3_E40N30_100km_U_2019_2023_1.zip", "filesize": 2_000_000},
            ],
        },
    )
    adapter = EGMSAdapter(_TEST_KEY)

    params = EGMSSearchParams(
        aoi=_aoi(), level="L3", release="2019-2023", product_type="ORTHO-UP", tile_id="E40N30",
    )
    products = adapter.search(params)

    search_calls = [c for c in calls if c[0] == f"{API_BASE}/search"]
    assert len(search_calls) == 1
    _, kwargs = search_calls[0]
    body = kwargs["json"]
    assert body["levels"] == ["L3"]
    assert body["releases"] == ["2019-2023"]
    assert body["productType"] == "ORTHO-UP"
    assert body["tileId"] == "E40N30"
    assert body["bbox"] == [[3.0, 49.0], [3.6, 49.0], [3.6, 49.5], [3.0, 49.5], [3.0, 49.0]]
    assert kwargs["headers"]["Authorization"] == "Bearer tok-abc"

    assert len(products) == 1
    p = products[0]
    assert p.query_id == "q42"
    assert p.filename == "EGMS_L3_E40N30_100km_U_2019_2023_1.zip"
    assert p.size_mb == 2.0


def test_search_omits_optional_fields_when_not_set(monkeypatch):
    calls = []
    _install_post(monkeypatch, calls)
    adapter = EGMSAdapter(_TEST_KEY)

    adapter.search(EGMSSearchParams(aoi=_aoi(), level="L2A", release="2019-2023"))

    _, kwargs = [c for c in calls if c[0] == f"{API_BASE}/search"][0]
    body = kwargs["json"]
    assert "direction" not in body
    assert "productType" not in body
    assert "tileId" not in body


def test_list_options_hits_the_right_endpoint(monkeypatch):
    calls = []
    _install_post(monkeypatch, calls)

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        assert url == f"{API_BASE}/releases"
        return _FakeResponse(["2015-2021", "2018-2022", "2019-2023"])

    monkeypatch.setattr("insar_core.adapters.egms.requests.get", fake_get)
    adapter = EGMSAdapter(_TEST_KEY)

    releases = adapter.list_options("releases")
    assert releases == ["2015-2021", "2018-2022", "2019-2023"]


def test_download_builds_url_with_filename_and_query_id(monkeypatch, tmp_path: Path):
    from insar_core.models.egms import EGMSProduct

    calls = []
    _install_post(monkeypatch, calls)

    captured = {}

    def fake_stream_download(url, dest, **kwargs):
        captured["url"] = url
        captured["dest"] = dest
        captured["headers"] = kwargs.get("headers")
        dest.write_text("fake content")

    monkeypatch.setattr("insar_core.adapters.egms.stream_download", fake_stream_download)

    adapter = EGMSAdapter(_TEST_KEY)
    product = EGMSProduct(query_id="q42", filename="product.zip", level="L3")

    result = adapter.download(product, tmp_path)

    assert result == tmp_path / "product.zip"
    assert captured["url"] == f"{API_BASE}/download/product.zip?id=q42"
    assert captured["headers"]["Authorization"] == "Bearer tok-abc"
    assert result.read_text() == "fake content"
