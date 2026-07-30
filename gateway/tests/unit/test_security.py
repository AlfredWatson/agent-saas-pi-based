from app.core.security import create_token, decode_token, hash_password, verify_password

def test_password_and_token_roundtrip():
    digest = hash_password("correct-horse-battery-staple")
    assert verify_password("correct-horse-battery-staple", digest)
    assert decode_token(create_token("00000000-0000-0000-0000-000000000001")) == "00000000-0000-0000-0000-000000000001"
