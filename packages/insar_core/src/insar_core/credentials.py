from __future__ import annotations

import json
import netrc
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

EARTHDATA_HOST = "urs.earthdata.nasa.gov"
EGMS_KEY_FILE_ENV = "EGMS_SERVICE_KEY_FILE"


@dataclass(frozen=True)
class EarthdataCredentials:
    username: str
    password: str


def load_earthdata_credentials(
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> EarthdataCredentials:
    """Return Earthdata credentials from explicit args, env vars, or ~/.netrc.

    Resolution order:
    1. Explicit username / password arguments
    2. EARTHDATA_USER and EARTHDATA_PASS environment variables
    3. ~/.netrc entry for urs.earthdata.nasa.gov
    """
    if username and password:
        return EarthdataCredentials(username=username, password=password)

    env_user = os.environ.get("EARTHDATA_USER")
    env_pass = os.environ.get("EARTHDATA_PASS")
    if env_user and env_pass:
        return EarthdataCredentials(username=env_user, password=env_pass)

    netrc_path = Path.home() / ".netrc"
    if netrc_path.exists():
        try:
            n = netrc.netrc(str(netrc_path))
            auth = n.authenticators(EARTHDATA_HOST)
            if auth:
                return EarthdataCredentials(username=auth[0], password=auth[2])
        except netrc.NetrcParseError:
            pass

    raise EnvironmentError(
        "Earthdata credentials not found. "
        "Set EARTHDATA_USER and EARTHDATA_PASS environment variables, "
        "or add an entry for urs.earthdata.nasa.gov in ~/.netrc."
    )


def load_egms_service_key(path: Optional[str] = None) -> dict:
    """Return the parsed CLMS API service-account key JSON.

    Resolution order:
    1. Explicit `path` argument
    2. EGMS_SERVICE_KEY_FILE environment variable

    Generate the key from your CLMS account page (land.copernicus.eu) and
    save it as a JSON file with client_id, user_id, token_uri, private_key.
    """
    key_path = path or os.environ.get(EGMS_KEY_FILE_ENV)
    if not key_path:
        raise EnvironmentError(
            "EGMS service-account key not found. Set EGMS_SERVICE_KEY_FILE to the path "
            "of the JSON key downloaded from your CLMS account, or pass `path` explicitly."
        )
    return json.loads(Path(key_path).read_text())
