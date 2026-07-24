from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional

import jwt
import requests

from insar_core.models.egms import EGMSProduct, EGMSSearchParams
from insar_core.net import stream_download

API_BASE = "https://egms.land.copernicus.eu/insar-api/archive"

# Refresh the access token slightly before it actually expires.
_TOKEN_SAFETY_MARGIN_S = 60


@dataclass(frozen=True)
class EGMSServiceKey:
    """CLMS API service-account key, as downloaded from the CLMS account page.

    Used to sign a JWT assertion and exchange it for an OAuth2 access token
    """
    client_id: str
    user_id: str
    token_uri: str
    private_key: str

    @classmethod
    def from_dict(cls, data: dict) -> "EGMSServiceKey":
        return cls(
            client_id=data["client_id"],
            user_id=data["user_id"],
            token_uri=data["token_uri"],
            private_key=data["private_key"],
        )


class EGMSAdapter:
    """European Ground Motion Service (Copernicus Land) product downloader.

    Unlike ASF, EGMS does not serve raw SLCs to process - it serves finished
    ground-motion products (velocity / displacement time series) for an AOI,
    so this adapter does not implement SceneSearchAdapter.
    """

    def __init__(self, service_key: EGMSServiceKey):
        self._key = service_key
        self._access_token: Optional[str] = None
        self._token_expiry: float = 0.0

    def _get_access_token(self) -> str:
        if self._access_token and time.time() < self._token_expiry - _TOKEN_SAFETY_MARGIN_S:
            return self._access_token

        now = int(time.time())
        claim_set = {
            "iss": self._key.client_id,
            "sub": self._key.user_id,
            "aud": self._key.token_uri,
            "iat": now,
            "exp": now + 3600,
        }
        assertion = jwt.encode(claim_set, self._key.private_key, algorithm="RS256")
        resp = requests.post(
            self._key.token_uri,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        token: str = payload["access_token"]
        self._access_token = token
        self._token_expiry = time.time() + payload.get("expires_in", 3600)
        return token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_access_token()}", "Accept": "application/json"}

    def list_options(self, kind: str) -> list:
        """List valid values for a search filter.

        `kind` is one of: levels, releases, swaths, relative_orbits, bursts,
        directions, tile_ids, product_types.
        """
        resp = requests.get(f"{API_BASE}/{kind}", headers=self._headers(), timeout=30)
        resp.raise_for_status()
        return resp.json()

    def search(self, params: EGMSSearchParams) -> List[EGMSProduct]:
        """Search for products covering the AOI. Max AOI extent is 5x5 degrees."""
        body: dict[str, Any] = {
            "id": None,
            "bbox": _aoi_points(params.aoi),
            "levels": [params.level],
            "releases": [params.release],
        }
        if params.direction:
            body["direction"] = params.direction
        if params.product_type:
            body["productType"] = params.product_type
        if params.tile_id:
            body["tileId"] = params.tile_id

        resp = requests.post(f"{API_BASE}/search", headers=self._headers(), json=body, timeout=60)
        resp.raise_for_status()
        result = resp.json()
        query_id = result.get("id")
        return [
            EGMSProduct(
                query_id=query_id,
                filename=hit["filename"],
                level=params.level,
                size_mb=hit.get("filesize", 0) / 1e6 if hit.get("filesize") else None,
                properties=hit,
            )
            for hit in result.get("hits", [])
        ]

    def download(
        self,
        product: EGMSProduct,
        destination: Path,
        progress_cb: Optional[Callable[..., None]] = None,
    ) -> Path:
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        url = f"{API_BASE}/download/{product.filename}?id={product.query_id}"
        dest = destination / product.filename
        stream_download(url, dest, headers=self._headers(), progress_cb=progress_cb)
        return dest


def _aoi_points(aoi) -> list[list[float]]:
    """EGMS accepts a list of AOI vertices and derives the bounding box itself."""
    geom = aoi.geometry
    if geom.get("type") == "Feature":
        geom = geom["geometry"]
    return [[lon, lat] for lon, lat in geom["coordinates"][0]]
