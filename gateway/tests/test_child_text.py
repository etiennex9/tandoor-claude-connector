import asyncio
import logging

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server import create_proxy
from fastmcp.server.providers.proxy import ProxyClient
from fastmcp.tools import Tool

from photo_import import PHOTO_INSTRUCTIONS
from server import (
    DEFAULT_EXCLUDE,
    DEFAULT_INCLUDE_ONLY,
    ChildDescriptions,
    ChildInstructions,
    child_hides,
    gateway_proxy,
)

DEFAULT_ENV = {
    "TANDOOR_MCP_INCLUDE_ONLY": ", ".join(DEFAULT_INCLUDE_ONLY),
    "TANDOOR_MCP_EXCLUDE": ", ".join(DEFAULT_EXCLUDE),
}

# tandoor-recipes-mcp 2.0.1's instructions on stdio (build/lib/instructions.js), with the default stash threshold.
INSTRUCTIONS = """\
Tandoor Recipes MCP server, full access to recipes, meal plans, shopping lists,
ingredients, cook logs, nutrition, and AI-assisted imports.

Where to start:
- Current state (read-only): `tandoor://meal-plan/this-week`, `tandoor://pantry/on-hand`,
  `tandoor://shopping-list/active`, `tandoor://meal-types`.
- Common workflows: use the `plan_week`, `grocery_list_for_plan`,
  `what_can_i_make_tonight`, or `import_and_plan` prompts.
- Recipe search: use `search_recipes` with food/keyword *names* (it resolves IDs
  for you). Fall back to `list_recipes` for the full filter surface.
- Write tools return a slim JSON shape by default. Pass `format: "full"` for the raw
  Tandoor API response when you need substitutes, image URLs, nutrition objects, etc.

Large results: tool responses bigger than ~25000 bytes come back as
`{stashed:true, handle, shape, sample_filters}` instead of the full payload. Call
`jq_query` with that `handle` and a jq filter to extract the parts you need, do
NOT re-run the original tool. Example: `jq_query({handle, filter: ".results | map({id, name})"})`.
Operators can set TANDOOR_MCP_STASH_ENABLED=0 to disable this behavior.

Require Tandoor serializer etiquette: foreign-key writes use `{id: n}` envelopes,
not bare integers. All tools here already handle that, just pass `food_id`, etc.

Transport: stdio. auth-required: no"""

# Their 2.0.1 descriptions (build/tools/recipe.js and ingredient.js).
IMPORT_URL = (
    "Import a recipe from a URL. Tries Tandoor's built-in scraper first, then fetches the page and extracts schema.org "
    "JSON-LD as a real fallback (works when Tandoor can't reach the URL or doesn't support the site). On total "
    "failure, throws with an attempts log — pass create_stub_on_failure=true to write an empty placeholder recipe "
    "instead. If all stages fail, recommend: (1) retry with create_stub_on_failure=true to capture the URL as a stub "
    "you can fill in later; (2) if the page is an image/PDF, use import_recipe_from_image (AI provider required); (3) "
    "if the URL is paywalled or JS-rendered, fall back to manual create_recipe with fields you can see."
)
PARSE_INGREDIENT = (
    'Parse a free-form ingredient string (e.g. "2 cups flour") into amount, unit, food, note. Tandoor\'s parser is '
    "heuristic and can misfire on compound descriptions, brand names, or non-English units. If the result looks wrong "
    "or unit/food come back null, recommend: construct the ingredient directly via create_ingredient with explicit "
    "food_id (via find_or_create on the food name) and unit_id. That path is deterministic."
)
DRAFT_07 = "http://json-schema.org/draft-07/schema#"
CREATE_RECIPE = (
    "Create recipe. Required: name, steps[{instruction, ingredients[{food, amount, unit?}]}]. Optional: description, "
    "servings, working_time, waiting_time, keywords[]"
)


def child() -> FastMCP:
    child = FastMCP("tandoor-recipes-mcp", instructions=INSTRUCTIONS)

    @child.tool(name="import_recipe_from_url", description=IMPORT_URL)
    def import_recipe_from_url(url: str, create_stub_on_failure: bool = False) -> str:
        return f"imported {url}"

    @child.tool(name="parse_ingredient", description=PARSE_INGREDIENT)
    def parse_ingredient(text: str) -> str:
        return text

    def create_recipe(name: str) -> str:
        return name

    # The child's schemas come from zod and carry keywords that a ToolTransformConfig rebuild would drop.
    tool = Tool.from_function(create_recipe, name="create_recipe", description=CREATE_RECIPE)
    child.add_tool(tool.model_copy(update={"parameters": tool.parameters | {"$schema": DRAFT_07}}))

    @child.tool(name="get_version", description="Report this MCP server's name/version.")
    def get_version() -> str:
        return "2.0.1"

    return child


def gateway(env=DEFAULT_ENV):
    # A ProxyClient like build_app's, which speaks the handshake era to the child whatever Claude speaks.
    return gateway_proxy(ProxyClient(child()), env)


def seen(proxy, mode):
    async def connect():
        async with Client(proxy, mode=mode) as client:
            tools = {t.name: t for t in await client.list_tools()}
            result = await client.call_tool("import_recipe_from_url", {"url": "https://example.com/r"})
            return client.instructions, tools, result.data

    return asyncio.run(connect())


# --- which tools the child hides -------------------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["jq_query", "import_recipe_from_image", "create_ingredient", "delete_recipe"])
def test_hidden_by_default(tool):
    assert child_hides(DEFAULT_ENV, tool)


@pytest.mark.parametrize("tool", ["create_recipe", "import_recipe_from_url", "parse_ingredient"])
def test_exposed_by_default(tool):
    assert not child_hides(DEFAULT_ENV, tool)


def test_globs_and_exclusions():
    env = {"TANDOOR_MCP_INCLUDE_ONLY": "get_*, jq_*", "TANDOOR_MCP_EXCLUDE": "get_version"}
    assert not child_hides(env, "jq_query")
    assert child_hides(env, "get_version")
    assert not child_hides({"TANDOOR_MCP_INCLUDE_ONLY": " , "}, "jq_query")


# --- instructions ----------------------------------------------------------------------------------------------------


def test_instructions_are_edited():
    text = ChildInstructions(DEFAULT_ENV).edit(INSTRUCTIONS)
    assert "jq_query" not in text and "stash" not in text
    assert "AI-assisted" not in text
    assert "ingredients, cook logs and nutrition.\n\nWhere to start:" in text
    assert "etc.\n\nRequire Tandoor serializer etiquette" in text
    assert text.endswith(f"Transport: stdio. auth-required: no\n\n{PHOTO_INSTRUCTIONS}")


def test_stash_paragraph_stays_when_jq_query_is_exposed():
    env = DEFAULT_ENV | {"TANDOOR_MCP_INCLUDE_ONLY": DEFAULT_ENV["TANDOOR_MCP_INCLUDE_ONLY"] + ", jq_query"}
    text = ChildInstructions(env).edit(INSTRUCTIONS)
    assert "Call\n`jq_query` with that `handle`" in text
    assert "AI-assisted" not in text


def test_other_instructions_get_the_photo_paragraph(caplog):
    instructions = ChildInstructions(DEFAULT_ENV)
    with caplog.at_level(logging.WARNING, logger="tandoor_gateway"):
        assert instructions.edit("Something else.") == f"Something else.\n\n{PHOTO_INSTRUCTIONS}"
        instructions.edit("Something else.")
    assert len([r for r in caplog.records if "instructions changed" in r.message]) == 2  # once per edit
    assert instructions.edit(None) == PHOTO_INSTRUCTIONS


@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_claude_sees_the_edited_instructions(mode):
    instructions, _, _ = seen(gateway(), mode)
    assert instructions == ChildInstructions(DEFAULT_ENV).edit(INSTRUCTIONS)


# --- descriptions ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_claude_sees_the_edited_descriptions(mode):
    _, tools, result = seen(gateway(), mode)

    url = tools["import_recipe_from_url"].description
    assert "import_recipe_from_image" not in url
    assert "(2) if the page is an image or PDF, ask the user to share it in the chat, then read it and save" in url
    assert url.endswith("fall back to manual create_recipe with fields you can see.")

    parse = tools["parse_ingredient"].description
    assert "create_ingredient" not in parse and "find_or_create" not in parse
    assert parse.endswith("set the food, amount, unit and note yourself.")

    assert tools["create_recipe"].description.startswith(CREATE_RECIPE + ". For a recipe read from a photo")
    assert tools["get_version"].description == "Report this MCP server's name/version."
    # The edited tools still reach the child.
    assert result == "imported https://example.com/r"


def test_edited_tools_keep_their_schemas():
    async def schemas(server):
        async with Client(server) as client:
            return {t.name: t.input_schema for t in await client.list_tools()}

    original = asyncio.run(schemas(create_proxy(ProxyClient(child()))))
    assert original["create_recipe"]["$schema"] == DRAFT_07
    assert asyncio.run(schemas(gateway())) == original


def test_changed_descriptions_are_left_alone(caplog):
    descriptions = ChildDescriptions(DEFAULT_ENV)
    plain = child()

    async def tool():
        return await plain.get_tool("create_recipe")

    tool = asyncio.run(tool()).model_copy(update={"description": "Create a recipe."})
    with caplog.at_level(logging.WARNING, logger="tandoor_gateway"):
        assert descriptions.edit(tool).description == "Create a recipe."
    assert "create_recipe changed" in caplog.text


def test_descriptions_mentioning_exposed_tools_stay():
    env = DEFAULT_ENV | {"TANDOOR_MCP_INCLUDE_ONLY": DEFAULT_ENV["TANDOOR_MCP_INCLUDE_ONLY"] + ", create_ingredient"}
    _, tools, _ = seen(gateway(env), "legacy")
    assert tools["parse_ingredient"].description == PARSE_INGREDIENT
