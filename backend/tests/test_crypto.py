from app.services import crypto


def test_decrypt_reverses_encrypt():
    assert crypto.decrypt(crypto.encrypt("s3cr3t-password")) == "s3cr3t-password"


def test_encrypted_value_does_not_contain_plaintext():
    token = crypto.encrypt("s3cr3t-password")
    assert "s3cr3t-password" not in token
