from fnmatch import fnmatchcase

import pytest

from server import DEFAULT_EXCLUDE, DEFAULT_INCLUDE_ONLY, is_blank, tandoor_env

TOOL_VARS = ("TANDOOR_MCP_PROFILE", "TANDOOR_MCP_INCLUDE_ONLY", "TANDOOR_MCP_EXCLUDE")


@pytest.fixture(autouse=True)
def tandoor(monkeypatch):
    monkeypatch.setenv("TANDOOR_URL", "https://recipe.example.com")
    monkeypatch.setenv("TANDOOR_TOKEN", "token")
    for name in TOOL_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("value", [None, "", "   ", " , ", ",,", "﻿", " ﻿ , "])
def test_blank_values(value):
    assert is_blank(value)


def test_a_single_tool_is_not_blank():
    assert not is_blank(" get_version ")


def test_defaults_apply_when_unset():
    env = tandoor_env()
    assert env["TANDOOR_MCP_PROFILE"] == "full"
    assert env["TANDOOR_MCP_INCLUDE_ONLY"] == ", ".join(DEFAULT_INCLUDE_ONLY)
    assert env["TANDOOR_MCP_EXCLUDE"] == ", ".join(DEFAULT_EXCLUDE)


@pytest.mark.parametrize("value", ["", "   ", " , ", "﻿"])
def test_blank_counts_as_unset(monkeypatch, value):
    for name in TOOL_VARS:
        monkeypatch.setenv(name, value)
    env = tandoor_env()
    assert env["TANDOOR_MCP_PROFILE"] == "full"
    assert env["TANDOOR_MCP_INCLUDE_ONLY"] == ", ".join(DEFAULT_INCLUDE_ONLY)
    assert env["TANDOOR_MCP_EXCLUDE"] == ", ".join(DEFAULT_EXCLUDE)


def test_explicit_values_are_kept(monkeypatch):
    monkeypatch.setenv("TANDOOR_MCP_PROFILE", "basic")
    monkeypatch.setenv("TANDOOR_MCP_INCLUDE_ONLY", "get_version, search_recipes")
    monkeypatch.setenv("TANDOOR_MCP_EXCLUDE", "merge_*")
    env = tandoor_env()
    assert env["TANDOOR_MCP_PROFILE"] == "basic"
    assert env["TANDOOR_MCP_INCLUDE_ONLY"] == "get_version, search_recipes"
    assert env["TANDOOR_MCP_EXCLUDE"] == "merge_*"


def test_transport_is_always_stdio(monkeypatch):
    monkeypatch.setenv("TANDOOR_MCP_TRANSPORT", "http")
    assert tandoor_env()["TANDOOR_MCP_TRANSPORT"] == "stdio"


def test_gateway_secrets_stay_out_of_the_child(monkeypatch):
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "secret")
    monkeypatch.setenv("JWT_SIGNING_KEY", "key")
    env = tandoor_env()
    assert "OIDC_CLIENT_SECRET" not in env
    assert "JWT_SIGNING_KEY" not in env
    assert all(name.startswith("TANDOOR_") for name in env)


def test_default_include_list_has_no_excluded_tool():
    # The exclude list is a second layer: nothing curated should hit it.
    hits = [t for t in DEFAULT_INCLUDE_ONLY for p in DEFAULT_EXCLUDE if fnmatchcase(t, p)]
    assert hits == []
