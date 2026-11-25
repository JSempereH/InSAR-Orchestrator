from app.services import crypto


def test_decrypt_reverses_encrypt():
    assert crypto.decrypt(crypto.encrypt("s3cr3t-password")) == "s3cr3t-password"


def test_encrypted_value_does_not_contain_plaintext():
    token = crypto.encrypt("s3cr3t-password")
    assert "s3cr3t-password" not in token


def test_a_secret_key_that_is_not_a_fernet_key_fails_at_start_with_a_readable_message(monkeypatch):
    import pytest

    monkeypatch.setattr(crypto, "_fernet", None)
    monkeypatch.setattr(crypto.settings, "secret_key", "not-a-fernet-key")
    with pytest.raises(crypto.InvalidSecretKey, match="Fernet"):
        crypto.validate_key()


def test_without_a_secret_key_one_is_generated_once_and_kept_so_credentials_survive_a_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(crypto.settings, "secret_key", "")
    monkeypatch.setattr(crypto.settings, "downloads_dir", str(tmp_path))
    monkeypatch.setattr(crypto, "_fernet", None)
    token = crypto.encrypt("keep me")
    monkeypatch.setattr(crypto, "_fernet", None)                        # a restart
    assert crypto.decrypt(token) == "keep me"
    assert oct((tmp_path / ".secret_key").stat().st_mode & 0o777) == "0o600"
    monkeypatch.setattr(crypto, "_fernet", None)
