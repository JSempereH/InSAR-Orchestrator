import pytest

from insar_core.credentials import EARTHDATA_HOST, load_earthdata_credentials


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("EARTHDATA_USER", raising=False)
    monkeypatch.delenv("EARTHDATA_PASS", raising=False)


def test_explicit_args_take_precedence(monkeypatch):
    monkeypatch.setenv("EARTHDATA_USER", "env-user")
    monkeypatch.setenv("EARTHDATA_PASS", "env-pass")

    creds = load_earthdata_credentials(username="explicit-user", password="explicit-pass")
    assert creds.username == "explicit-user"
    assert creds.password == "explicit-pass"


def test_falls_back_to_env_vars(monkeypatch):
    monkeypatch.setenv("EARTHDATA_USER", "env-user")
    monkeypatch.setenv("EARTHDATA_PASS", "env-pass")

    creds = load_earthdata_credentials()
    assert creds.username == "env-user"
    assert creds.password == "env-pass"


def test_falls_back_to_netrc(monkeypatch, tmp_path):
    netrc_file = tmp_path / ".netrc"
    netrc_file.write_text(
        f"machine {EARTHDATA_HOST}\n    login netrc-user\n    password netrc-pass\n"
    )
    netrc_file.chmod(0o600)
    monkeypatch.setattr("insar_core.credentials.Path.home", lambda: tmp_path)

    creds = load_earthdata_credentials()
    assert creds.username == "netrc-user"
    assert creds.password == "netrc-pass"


def test_raises_when_no_credentials_available(monkeypatch, tmp_path):
    monkeypatch.setattr("insar_core.credentials.Path.home", lambda: tmp_path)

    with pytest.raises(EnvironmentError):
        load_earthdata_credentials()
