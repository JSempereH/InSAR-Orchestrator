from __future__ import annotations

import netrc
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

EARTHDATA_HOST = "urs.earthdata.nasa.gov"


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
