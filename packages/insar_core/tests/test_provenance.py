import importlib.util

import pytest

from insar_core.adapters.hyp3 import DEFAULT_INSAR_PARAMETERS, HyP3Adapter
from insar_core.pipeline import provenance


def test_software_versions_has_python_and_the_library():
    versions = provenance.software_versions()
    assert versions["python"]
    assert versions["insar-core"]            # installed in the test environment
    assert "mintpy" in versions               # None when MintPy is absent, but always listed


def test_parse_mintpy_options_ignores_comments_and_blank_lines():
    text = """
## a comment
mintpy.load.processor   = hyp3   # trailing comment
mintpy.subset.lalo      = 40.1:40.9,-3.9:-3.4

not.an.option = 1
"""
    assert provenance.parse_mintpy_options(text) == {
        "mintpy.load.processor": "hyp3",
        "mintpy.subset.lalo": "40.1:40.9,-3.9:-3.4",
    }


@pytest.mark.skipif(importlib.util.find_spec("mintpy") is None, reason="MintPy not installed")
def test_effective_options_resolve_auto_and_apply_the_custom_config():
    effective = provenance.mintpy_effective_options("mintpy.load.processor = hyp3\nmintpy.subset.lalo = 1:2,3:4\n")
    assert effective is not None
    assert effective["mintpy.load.processor"] == "hyp3"        # custom wins over the default "auto"
    assert effective["mintpy.subset.lalo"] == "1:2,3:4"
    assert effective["mintpy.troposphericDelay.method"] != "auto"   # resolved by MintPy's auto table
    assert "auto" not in {v for k, v in effective.items() if k.startswith("mintpy.unwrapError")}


def test_effective_options_are_none_without_mintpy(monkeypatch):
    monkeypatch.setattr(provenance, "_mintpy_defaults_dir", lambda: None)
    assert provenance.mintpy_effective_options("mintpy.load.processor = hyp3") is None


class _FakeSdk:
    def __init__(self):
        self.calls = []

    def submit_insar_job(self, reference, secondary, **kwargs):
        self.calls.append((reference, secondary, kwargs))
        return [_FakeJob()]

    def find_jobs(self, name=None, **_kw):
        self.name = name
        return [_FakeJob()]


class _FakeJob:
    job_id = "job-1"
    status_code = "PENDING"
    job_parameters = {"granules": ["S1A_IW_SLC__1SDV_20250104T000000_x", "S1A_IW_SLC__1SDV_20250116T000000_y"]}
    request_time = None


def _adapter():
    adapter = HyP3Adapter.__new__(HyP3Adapter)
    adapter._client = _FakeSdk()
    return adapter


def test_submit_pair_uses_the_published_defaults():
    adapter = _adapter()
    adapter.submit_pair("ref", "sec", name="n")
    _ref, _sec, kwargs = adapter._client.calls[0]
    assert kwargs == {"name": "n", **DEFAULT_INSAR_PARAMETERS}


def test_submit_pair_overrides_win_over_the_defaults():
    adapter = _adapter()
    adapter.submit_pair("ref", "sec", looks="10x2", include_dem=False)
    kwargs = adapter._client.calls[0][2]
    assert kwargs["looks"] == "10x2" and kwargs["include_dem"] is False
    assert kwargs["include_inc_map"] is True


def test_find_jobs_by_name_returns_pairs_with_their_granules():
    adapter = _adapter()
    jobs = adapter.find_jobs_by_name("insar-madrid")
    assert adapter._client.name == "insar-madrid"
    assert jobs[0].hyp3_job_id == "job-1"
    assert jobs[0].reference_granule.endswith("_x") and jobs[0].secondary_granule.endswith("_y")
