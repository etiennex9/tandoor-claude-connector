"""OAuth gateway between Claude and the Tandoor MCP server.

Claude signs in through Authentik (via FastMCP's OIDCProxy); authenticated MCP
traffic is proxied to a long-lived tandoor-recipes-mcp child process over
stdio, which calls Tandoor with a single API token.
"""

import logging
import os
import re
from fnmatch import fnmatchcase
from urllib.parse import urlsplit

from fastmcp.client.transports import StdioTransport
from fastmcp.server import create_proxy
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.middleware import Middleware
from fastmcp.server.providers.proxy import ProxyClient
from fastmcp.server.transforms import Transform
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from photo_import import PHOTO_INSTRUCTIONS, Tandoor, add_photo_import

logger = logging.getLogger("tandoor_gateway")


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def parse_allowed_users(raw: str) -> frozenset[str]:
    return frozenset(u.strip().lower() for u in raw.split(",") if u.strip())


def is_allowed(claims: dict, allowed: frozenset[str]) -> bool:
    """An empty allowlist defers entirely to Authentik's application policy."""
    if not allowed:
        return True
    identities = {
        str(claims.get(key, "")).strip().lower()
        for key in ("email", "preferred_username")
    }
    return bool(identities & allowed)


class AllowlistJWTVerifier(JWTVerifier):
    """Verifies Authentik access tokens and rejects users not on the allowlist."""

    def __init__(self, *, allowed_users: frozenset[str], **kwargs):
        super().__init__(**kwargs)
        self.allowed_users = allowed_users

    async def verify_token(self, token: str) -> AccessToken | None:
        access = await super().verify_token(token)
        if access is None:
            return None
        if not is_allowed(access.claims or {}, self.allowed_users):
            logger.warning(
                "Rejected Authentik user not in ALLOWED_USERS (email=%r, username=%r)",
                (access.claims or {}).get("email"),
                (access.claims or {}).get("preferred_username"),
            )
            return None
        return access


class AuthentikProxy(OIDCProxy):
    def __init__(self, *, allowed_users: frozenset[str], **kwargs):
        self._allowed_users = allowed_users
        super().__init__(**kwargs)

    def get_token_verifier(self, *, algorithm=None, audience=None, required_scopes=None, timeout_seconds=None):
        return AllowlistJWTVerifier(
            allowed_users=self._allowed_users,
            jwks_uri=str(self.oidc_config.jwks_uri),
            issuer=str(self.oidc_config.issuer),
            algorithm=algorithm,
            audience=audience,
            required_scopes=required_scopes,
        )


# Curated read-write tool set (see README). TANDOOR_MCP_INCLUDE_ONLY overrides it.
DEFAULT_INCLUDE_ONLY = (
    "get_version", "search_recipes", "list_recipes", "list_recipes_flat", "get_recipe", "related_recipes",
    "list_foods", "get_food", "list_ingredients", "parse_ingredient", "list_keywords", "get_keyword",
    "list_meal_plans", "get_meal_plan", "list_meal_types", "list_shopping_entries", "get_shopping_entry",
    "list_cook_logs", "list_units", "list_steps", "list_property_types",
    "create_recipe", "update_recipe", "recipe_batch_update", "upload_recipe_image",
    "update_step", "delete_step", "update_ingredient", "delete_ingredient",
    "update_food", "food_batch_update", "create_food", "food_shopping_update",
    "create_shopping_entry", "update_shopping_entry", "delete_shopping_entry",
    "bulk_check_shopping_entries", "add_recipe_to_shopping_list",
    "create_meal_plan", "update_meal_plan", "delete_meal_plan",
    "import_recipe_from_url", "create_cook_log",
)

# Second layer in case the allowlist is ever widened with globs. TANDOOR_MCP_EXCLUDE overrides it.
DEFAULT_EXCLUDE = (
    "delete_recipe*", "delete_food", "merge_*", "*access_token*", "*invite_link*", "authenticate",
    "*space*", "*user*", "enable_tool_group", "disable_tool_group", "list_tool_groups",
)


def is_blank(value: str | None) -> bool:
    """True when tandoor-recipes-mcp would read the value as an empty list.

    It splits on commas and drops items that are empty after a JavaScript
    trim(), so "", "  " and " , " all mean "no filter", and an empty include
    list exposes every tool. trim() also strips U+FEFF, which str.strip() keeps.
    """
    return not any(item.replace("﻿", "").strip() for item in (value or "").split(","))


def tandoor_env() -> dict[str, str]:
    """Only TANDOOR_* settings reach the child; gateway secrets stay here."""
    require_env("TANDOOR_URL")
    require_env("TANDOOR_TOKEN")
    env = {k: v for k, v in os.environ.items() if k.startswith("TANDOOR_")}
    env["TANDOOR_MCP_TRANSPORT"] = "stdio"
    defaults = {
        # All groups registered at boot; TANDOOR_MCP_INCLUDE_ONLY decides what is exposed.
        # A blank profile would fall back to "core", which has no write tools.
        "TANDOOR_MCP_PROFILE": "full",
        "TANDOOR_MCP_INCLUDE_ONLY": ", ".join(DEFAULT_INCLUDE_ONLY),
        "TANDOOR_MCP_EXCLUDE": ", ".join(DEFAULT_EXCLUDE),
    }
    for key, default in defaults.items():
        if is_blank(env.get(key)):
            env[key] = default
    # Unset means ~/Downloads, ~/Pictures, ~/Documents and the temp dir; empty means
    # upload_recipe_image may read no local file. Claude can't put files here anyway.
    env.setdefault("TANDOOR_MCP_UPLOAD_ROOT", "")
    return env


def child_hides(env: dict[str, str], tool: str) -> bool:
    """True when the child's tool lists in env keep it from registering tool (the child matches * globs too)."""

    def listed(name: str) -> bool:
        return any(fnmatchcase(tool, p.strip()) for p in env.get(name, "").split(",") if p.strip())

    included = is_blank(env.get("TANDOOR_MCP_INCLUDE_ONLY")) or listed("TANDOOR_MCP_INCLUDE_ONLY")
    return not included or listed("TANDOOR_MCP_EXCLUDE")


# tandoor-recipes-mcp 2.0.1 text that sends Claude to tools the gateway hides, or past save_recipe_from_photo. Each
# edit applies while the tool it names last is hidden (always for None), and is skipped with a warning once the
# child's text changes.
INSTRUCTION_EDITS = (
    # The child stashes large results only when jq_query is registered.
    (re.compile(r"Large results: .*?TANDOOR_MCP_STASH_ENABLED=0 to disable this behavior\.\n\n", re.S), "", "jq_query"),
    (
        re.compile(re.escape("cook logs, nutrition, and AI-assisted imports.")),
        "cook logs and nutrition.",
        "import_recipe_from_image",
    ),
)

DESCRIPTION_EDITS = (
    (
        "import_recipe_from_url",
        "(2) if the page is an image/PDF, use import_recipe_from_image (AI provider required);",
        "(2) if the page is an image or PDF, ask the user to share it in the chat, then read it and save it with "
        "save_recipe_from_photo;",
        "import_recipe_from_image",
    ),
    (
        "parse_ingredient",
        " If the result looks wrong or unit/food come back null, recommend: construct the ingredient directly via "
        "create_ingredient with explicit food_id (via find_or_create on the food name) and unit_id. That path is "
        "deterministic.",
        " If the result looks wrong or unit/food come back null, set the food, amount, unit and note yourself.",
        "create_ingredient",
    ),
    (
        "create_recipe",
        "Optional: description, servings, working_time, waiting_time, keywords[]",
        "Optional: description, servings, working_time, waiting_time, keywords[]. For a recipe read from a photo, "
        "screenshot or PDF, use save_recipe_from_photo instead: it keeps step names and ingredients without an amount.",
        None,
    ),
)


def edit_applies(env: dict[str, str], hidden_tool: str | None) -> bool:
    return hidden_tool is None or child_hides(env, hidden_tool)


class ChildInstructions(Middleware):
    """Edits the child's server instructions, which the proxy forwards to Claude, and adds the gateway's own.

    Passed to create_proxy(middleware=...) so that it runs outside the proxy's own middleware, which merges the child's
    instructions into the result. Instructions set on the gateway would replace the child's instead.
    """

    def __init__(self, env: dict[str, str]):
        self._edits = [(pattern, new) for pattern, new, tool in INSTRUCTION_EDITS if edit_applies(env, tool)]
        self._warned: set[str] = set()

    def edit(self, instructions: str | None) -> str:
        text = instructions or ""
        for pattern, new in self._edits:
            text, count = pattern.subn(new, text, count=1)
            if not count and instructions and pattern.pattern not in self._warned:
                self._warned.add(pattern.pattern)
                logger.warning("The child's instructions changed; not edited: %s", pattern.pattern)
        return f"{text.rstrip()}\n\n{PHOTO_INSTRUCTIONS}" if text.strip() else PHOTO_INSTRUCTIONS

    async def on_initialize(self, context, call_next):
        result = await call_next(context)
        return None if result is None else result.model_copy(update={"instructions": self.edit(result.instructions)})

    async def on_discover(self, context, call_next):
        result = await call_next(context)
        if isinstance(result, dict):  # not a DiscoverResult, which the proxy passes on as it is
            return result
        return result.model_copy(update={"instructions": self.edit(result.instructions)})


class ChildDescriptions(Transform):
    """Edits some of the child's tool descriptions. ToolTransformConfig would also rebuild their input schemas."""

    def __init__(self, env: dict[str, str]):
        self._edits = {name: (old, new) for name, old, new, tool in DESCRIPTION_EDITS if edit_applies(env, tool)}
        self._warned: set[str] = set()

    def edit(self, tool):
        if tool.name not in self._edits:
            return tool
        old, new = self._edits[tool.name]
        if old not in (tool.description or ""):
            if tool.name not in self._warned:
                self._warned.add(tool.name)
                logger.warning("The description of the child's %s changed; not edited", tool.name)
            return tool
        return tool.model_copy(update={"description": tool.description.replace(old, new)})

    async def list_tools(self, tools):
        return [self.edit(tool) for tool in tools]

    async def get_tool(self, name, call_next, *, version=None):
        tool = await call_next(name, version=version)
        return None if tool is None else self.edit(tool)


def gateway_proxy(backend: ProxyClient, child_env: dict[str, str], **settings):
    """The proxy in front of the child, with the child's text edited."""
    mcp = create_proxy(backend, name="Tandoor Recipes", middleware=[ChildInstructions(child_env)], **settings)
    mcp.add_transform(ChildDescriptions(child_env))
    return mcp


def build_app():
    """App factory for `uvicorn server:build_app --factory`."""
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

    # Public URL of the gateway, including the path nginx strips (e.g. /claude).
    public_base_url = require_env("PUBLIC_BASE_URL").rstrip("/")
    parts = urlsplit(public_base_url)
    # Authorization-server metadata lives at the host root so Claude's
    # /.well-known/oauth-authorization-server lookup finds it.
    issuer_url = f"{parts.scheme}://{parts.netloc}"

    auth = AuthentikProxy(
        allowed_users=parse_allowed_users(os.environ.get("ALLOWED_USERS", "")),
        config_url=require_env("OIDC_CONFIG_URL"),
        client_id=require_env("OIDC_CLIENT_ID"),
        client_secret=require_env("OIDC_CLIENT_SECRET"),
        base_url=public_base_url,
        issuer_url=issuer_url,
        jwt_signing_key=require_env("JWT_SIGNING_KEY"),
        # Authentik ignores RFC 8707 resource indicators; don't send one.
        forward_resource=False,
        # Always request the claims and refresh token we need from Authentik,
        # whatever scopes the MCP client asked for.
        extra_authorize_params={"scope": "openid email profile offline_access"},
    )

    child_env = tandoor_env()
    backend = ProxyClient(
        # keep_alive: one tandoor-recipes-mcp process serves every request and
        # is restarted automatically if it exits.
        StdioTransport(
            command="tandoor-recipes-mcp",
            args=[],
            env=child_env,
            keep_alive=True,
        ),
    )

    mcp = gateway_proxy(backend, child_env, auth=auth)
    # Gateway-side: the photo-import prompt, save_recipe_from_photo and the upload link for a recipe's image.
    add_photo_import(
        mcp,
        tandoor=Tandoor(require_env("TANDOOR_URL"), require_env("TANDOOR_TOKEN")),
        public_base_url=public_base_url,
    )

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return mcp.http_app(path="/mcp")
