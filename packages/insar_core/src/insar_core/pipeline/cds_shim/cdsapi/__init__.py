"""Stand-in for the `cdsapi` package that answers ERA5 requests from ARCO-ERA5.

PyAPS (inside MintPy) does `cdsapi.Client().retrieve(dataset, request, target=file)` once per
date. With this directory first on PYTHONPATH that call is served from the public ARCO-ERA5
archive instead of the Copernicus queue; see `era5_arco.py`. Only the ERA5 pressure-level
dataset is supported, which is all PyAPS asks for.
"""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("insar_era5_arco", Path(__file__).resolve().parents[2] / "era5_arco.py")
_era5 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_era5)


class Client:
    def __init__(self, *args, **kwargs):
        pass

    def retrieve(self, name, request, target=None):
        if name != "reanalysis-era5-pressure-levels":
            raise ValueError(f"only reanalysis-era5-pressure-levels is available here, not {name}")
        _era5.retrieve(request, target)
        return target
