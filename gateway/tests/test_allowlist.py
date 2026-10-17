from server import is_allowed, parse_allowed_users


def test_parse_allowed_users_normalises_and_skips_blanks():
    assert parse_allowed_users(" Me@Example.com, ,so ") == {"me@example.com", "so"}


def test_empty_allowlist_defers_to_authentik():
    assert is_allowed({}, frozenset())


def test_matches_email_case_insensitively():
    assert is_allowed({"email": "Me@Example.com"}, frozenset({"me@example.com"}))


def test_matches_preferred_username():
    assert is_allowed({"preferred_username": "so"}, frozenset({"so"}))


def test_rejects_unknown_user():
    assert not is_allowed(
        {"email": "intruder@example.com", "preferred_username": "intruder"},
        frozenset({"me@example.com"}),
    )


def test_rejects_token_without_identity_claims():
    assert not is_allowed({"sub": "abc"}, frozenset({"me@example.com"}))
