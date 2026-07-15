from tskmon.tokens import derive_token


def test_token_is_32_hex_chars():
    t = derive_token("s3cret", "nightly-db-backup")
    assert len(t) == 32
    assert all(c in "0123456789abcdef" for c in t)


def test_token_is_stable_across_calls():
    # Must survive restarts: the cron's URL cannot change under it.
    a = derive_token("s3cret", "nightly-db-backup")
    b = derive_token("s3cret", "nightly-db-backup")
    assert a == b


def test_token_differs_per_check_name():
    a = derive_token("s3cret", "nightly-db-backup")
    b = derive_token("s3cret", "legacy-etl")
    assert a != b


def test_token_differs_per_secret():
    # Rotating the secret rotates every token.
    a = derive_token("s3cret", "nightly-db-backup")
    b = derive_token("other", "nightly-db-backup")
    assert a != b
