import asyncio
import json
from pathlib import Path

import httpx2
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from photo_import import (
    PHOTO_IMPORT_PROMPT,
    SAVE_TOOL_DESCRIPTION,
    TRANSCRIPTION_RULES,
    Tandoor,
    UploadLinks,
    Units,
    add_photo_import,
)

BASE = "https://recipe.example.com/claude"
FIXTURE = Path(__file__).parent / "fixtures" / "poulet-miel-ail.json"
CARD = json.loads(FIXTURE.read_text(encoding="utf-8"))["arguments"]

# Per step, what Tandoor should store: food, amount, unit, note, no_amount. From the recipe Claude created with
# create_recipe on 2026-10-19, except that "au goût" and "pour huiler" no longer need amount 0.
EXPECTED = [
    [
        ("riz au jasmin", 160, "g", None, False),
        ("eau", 1.5, "tasse", None, False),
        ("sel", 1, "pincée", "grosse", False),
        ("huile", 0, None, "pour huiler le panier", True),
    ],
    [
        ("ail", 3, "gousse", None, False),
        ("sauce ponzu à la lime", 30, "ml", None, False),
        ("miel", 21, "g", "¾ du miel", False),
        ("hauts de cuisses de poulet", 4, None, "ou 2 poitrines de poulet", False),
        ("épices sésame doux", 12, "g", "mélange : cassonade, sel de mer, sésame blanc, ail, gingembre, poivre noir",
         False),
    ],
    [("oignons verts", 2, None, None, False), ("céleri", 3, "branche", None, False), ("pomme", 1, None, None, False)],
    [
        ("vinaigre de riz", 30, "ml", None, False),
        ("miel", 7, "g", "le reste", False),
        ("huile", 2, "c. à soupe", None, False),
        ("sel", 0, None, "au goût", True),
        ("poivre", 0, None, "au goût", True),
    ],
    [],
]

# Some of the space's units on 2026-10-19: open-data English ones, and French ones create_recipe added by name.
UNITS = [
    {"id": 1, "name": "can", "plural_name": "cans", "base_unit": None},
    {"id": 3, "name": "g", "plural_name": "g", "base_unit": "g"},
    {"id": 7, "name": "pinch", "plural_name": "pinches", "base_unit": "ml"},
    {"id": 8, "name": "ml", "plural_name": "ml", "base_unit": "ml"},
    {"id": 11, "name": "tbsp", "plural_name": "tbsps", "base_unit": "tbsp"},
    {"id": 13, "name": "cup", "plural_name": "cups", "base_unit": "us_cup"},
    {"id": 15, "name": "tasse", "plural_name": "tasse", "base_unit": None},
    {"id": 16, "name": "pincée", "plural_name": "pincée", "base_unit": None},
    {"id": 17, "name": "gousse", "plural_name": "gousse", "base_unit": None},
    {"id": 18, "name": "branche", "plural_name": "branche", "base_unit": None},
    {"id": 19, "name": "c. à soupe", "plural_name": "c. à soupe", "base_unit": None},
]


class FakeTandoor:
    """The parts of Tandoor's API that save_recipe_from_photo uses. Records every request."""

    def __init__(self):
        self.recipes = {1: {"id": 1, "name": "Udon au boeuf haché japonais", "image": "https://r.example.com/1.jpg"}}
        self.units = UNITS
        self.max_page_size = 100
        # (method, path) -> status code, or "down" for a refused connection.
        self.failures = {}
        self.requests = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        method, path, params = request.method, request.url.path, request.url.params
        failure = self.failures.get((method, path))
        if failure == "down":
            raise httpx2.ConnectError("connection refused", request=request)
        if failure:
            return httpx2.Response(failure, json={"steps": [{"ingredients": [{"food": ["This field is required."]}]}]})
        if method == "GET" and path == "/api/unit/":
            page, size = int(params["page"]), min(int(params["page_size"]), self.max_page_size)
            more = page * size < len(self.units)
            return httpx2.Response(200, json={
                "count": len(self.units),
                "next": f"https://recipe.example.com/api/unit/?page={page + 1}" if more else None,
                "results": self.units[(page - 1) * size:page * size],
            })
        if method == "GET" and path == "/api/recipe/":
            # Tandoor's search is fuzzy: a recipe sharing a word with the query is found too.
            words = set(params["query"].casefold().split())
            found = [r for r in self.recipes.values() if words & set(r["name"].casefold().split())]
            return httpx2.Response(200, json={"results": [{"id": r["id"], "name": r["name"]} for r in found]})
        if method == "POST" and path == "/api/recipe/":
            recipe = json.loads(request.content) | {"id": max(self.recipes) + 1, "image": None}
            self.recipes[recipe["id"]] = recipe
            return httpx2.Response(201, json=recipe)
        if method == "PATCH" and path.startswith("/api/recipe/"):
            recipe_id = int(path.split("/")[3])
            if recipe_id not in self.recipes:
                return httpx2.Response(404, json={"detail": "Not found."})
            self.recipes[recipe_id] |= json.loads(request.content)
            return httpx2.Response(200, json=self.recipes[recipe_id])
        return httpx2.Response(405)

    def bodies(self, method: str, path: str) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if (r.method, r.url.path) == (method, path)]

    def calls(self, method: str, path: str) -> int:
        return sum(1 for r in self.requests if (r.method, r.url.path) == (method, path))


@pytest.fixture
def tandoor():
    return FakeTandoor()


@pytest.fixture
def server(tandoor):
    mcp = FastMCP("test")
    links = add_photo_import(
        mcp,
        tandoor=Tandoor("https://recipe.example.com", "secret-token", transport=httpx2.MockTransport(tandoor)),
        public_base_url=BASE,
        links=UploadLinks(),
    )
    return mcp, links


def save(server, **arguments) -> str:
    mcp, _ = server

    async def call():
        async with Client(mcp) as client:
            return await client.call_tool("save_recipe_from_photo", arguments)

    return asyncio.run(call()).content[0].text


def tandoor_picks(units: list[dict], sent: dict | None):
    """The unit Tandoor 2.6's recipe serializer uses for a unit sent by name: the first by name whose name or plural is
    the one sent, ignoring case, or a new one."""
    if sent is None:
        return None
    name = sent["name"].casefold()
    found = sorted((u for u in units if name in (u["name"].casefold(), (u["plural_name"] or "").casefold())),
                   key=lambda u: u["name"])
    return found[0]["name"] if found else sent


def stored(ingredient: dict) -> tuple:
    unit = ingredient["unit"]["name"] if ingredient["unit"] else None
    return ingredient["food"]["name"], ingredient["amount"], unit, ingredient["note"], ingredient["no_amount"]


# --- the card --------------------------------------------------------------------------------------------------------


def test_saves_the_card(server, tandoor):
    text = save(server, **CARD)

    (recipe,) = tandoor.bodies("POST", "/api/recipe/")
    assert (recipe["name"], recipe["servings"], recipe["working_time"]) == (CARD["name"], 2, 20)
    assert recipe["internal"] is True
    assert (recipe["description"], recipe["servings_text"]) == (CARD["description"], "portions")
    assert recipe["keywords"] == [{"name": "Poulet"}, {"name": "Friteuse à air"}, {"name": "Goodfood"}]
    assert "waiting_time" not in recipe
    assert [s["name"] for s in recipe["steps"]] == [s["name"] for s in CARD["steps"]]
    assert [s["order"] for s in recipe["steps"]] == [0, 1, 2, 3, 4]
    assert all(s["show_as_header"] for s in recipe["steps"])
    assert [s["time"] for s in recipe["steps"]] == [16, 15, 0, 0, 0]
    assert [[stored(i) for i in s["ingredients"]] for s in recipe["steps"]] == EXPECTED
    assert all(i["order"] == n for s in recipe["steps"] for n, i in enumerate(s["ingredients"]))
    assert not any(i["is_header"] for s in recipe["steps"] for i in s["ingredients"])
    assert tandoor.calls("GET", "/api/unit/") == 1

    assert text.startswith(f"Saved {CARD['name']} (recipe 2): 5 steps, 17 ingredients.")
    assert "finished dish" in text


def test_the_result_has_an_upload_link(server):
    _, links = server
    text = save(server, **CARD)
    url = text.split("Upload link for its image: ", 1)[1].split()[0]
    assert url.startswith(f"{BASE}/upload/")
    link = links.get(url.rsplit("/", 1)[1])
    assert (link.recipe_id, link.recipe_name) == (2, CARD["name"])
    assert "It works once, for 30 minutes." in text


def test_amount_zero_means_no_amount(server, tandoor):
    save(server, name="Salade", steps=[{"ingredients": [{"food": "sel", "amount": 0, "note": "au goût"}]}])
    (recipe,) = tandoor.bodies("POST", "/api/recipe/")
    (ingredient,) = recipe["steps"][0]["ingredients"]
    assert (ingredient["amount"], ingredient["no_amount"]) == (0, True)


def test_sub_headings(server, tandoor):
    save(server, name="Salade", steps=[{"ingredients": [{"header": "Pour la sauce"}, {"food": "miel", "amount": 28}]}])
    (recipe,) = tandoor.bodies("POST", "/api/recipe/")
    header, honey = recipe["steps"][0]["ingredients"]
    assert header == {
        "food": None, "unit": None, "amount": 0, "note": "Pour la sauce", "order": 0,
        "is_header": True, "no_amount": True,
    }
    assert (honey["food"], honey["is_header"], honey["order"]) == ({"name": "miel"}, False, 1)


def test_a_step_without_a_name_is_no_header(server, tandoor):
    save(server, name="Salade", steps=[{"instruction": "Mélanger."}])
    (recipe,) = tandoor.bodies("POST", "/api/recipe/")
    assert recipe["steps"] == [
        {"name": "", "instruction": "Mélanger.", "time": 0, "order": 0, "show_as_header": False, "ingredients": []}
    ]


def test_units_are_read_only_when_needed(server, tandoor):
    save(server, name="Salade", steps=[{"ingredients": [{"food": "pomme", "amount": 1}]}])
    assert tandoor.calls("GET", "/api/unit/") == 0


def test_units_are_read_page_by_page(server, tandoor):
    tandoor.max_page_size = 4
    save(server, name="Salade", steps=[{"ingredients": [{"food": "huile", "amount": 2, "unit": "C. à S."}]}])
    assert tandoor.calls("GET", "/api/unit/") == 3
    (recipe,) = tandoor.bodies("POST", "/api/recipe/")
    assert recipe["steps"][0]["ingredients"][0]["unit"] == {"name": "c. à soupe"}


# --- units -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "unit"),
    [
        ("g", "g"),
        ("C. à s.", "c. à soupe"),
        ("c.a.s", "c. à soupe"),
        ("cuillères à soupe", "c. à soupe"),
        ("tasses", "tasse"),
        ("Gousses", "gousse"),
        ("pincees", "pincée"),
        ("cups", "cup"),
        ("TBSP", "tbsp"),
    ],
)
def test_written_units_match_existing_ones(written, unit):
    assert tandoor_picks(UNITS, Units(UNITS).payload(written)) == unit


@pytest.mark.parametrize(
    ("written", "unit"),
    [
        ("cuillères à café", "c. à thé"),
        ("Cuillerées à thé", "c. à thé"),
        ("cuillère à table", "c. à soupe"),
        ("cuillères à table", "c. à soupe"),
        ("cuillerée à soupe", "c. à soupe"),
    ],
)
def test_spellings_of_french_units(written, unit):
    assert Units([]).payload(written)["name"] == unit


@pytest.mark.parametrize(
    ("written", "unit"),
    [
        ("c. à t.", {"name": "c. à thé", "base_unit": "tsp"}),
        ("c. à soupe", {"name": "c. à soupe", "base_unit": "tbsp"}),
        ("Tasses", {"name": "tasse", "base_unit": "us_cup"}),
        ("gousse", {"name": "gousse"}),
        ("sachet", {"name": "sachet"}),
    ],
)
def test_new_units(written, unit):
    assert Units([]).payload(written) == unit


@pytest.mark.parametrize("written", [None, "", "  ", " . "])
def test_no_unit(written):
    assert Units(UNITS).payload(written) is None


def test_any_spelling_finds_an_existing_french_unit():
    units = Units([{"name": "c. à s.", "plural_name": "c. à s."}, {"name": "cuillère à thé", "plural_name": None}])
    assert units.payload("c. à soupe") == {"name": "c. à s."}
    assert units.payload("c. à t.") == {"name": "cuillère à thé"}


def test_what_tandoor_finds_is_sent_as_written():
    # Tandoor finds "z" as y's plural. Rewritten to "y", it would pick x, whose plural is "y".
    units = [{"name": "x", "plural_name": "y"}, {"name": "y", "plural_name": "z"}]
    assert tandoor_picks(units, Units(units).payload("Z")) == "y"


# --- duplicates and replacing ----------------------------------------------------------------------------------------


def test_a_recipe_with_the_same_name_is_refused(server, tandoor):
    tandoor.recipes[2] = {"id": 2, "name": CARD["name"].upper(), "image": None}
    with pytest.raises(ToolError, match=r"already has .*\(recipe 2\).*Nothing was saved"):
        save(server, **CARD)
    assert tandoor.calls("POST", "/api/recipe/") == 0


def test_a_similar_name_is_no_duplicate(server, tandoor):
    tandoor.recipes[2] = {"id": 2, "name": "Poulet collant miel et ail", "image": None}
    save(server, **CARD)
    assert tandoor.calls("POST", "/api/recipe/") == 1


def test_duplicates_on_request(server, tandoor):
    tandoor.recipes[2] = {"id": 2, "name": CARD["name"], "image": None}
    assert "(recipe 3)" in save(server, **CARD, allow_duplicate=True)
    assert tandoor.calls("GET", "/api/recipe/") == 0


def test_replaces_a_recipe(server, tandoor):
    _, links = server
    text = save(server, **CARD, recipe_id=1)

    assert tandoor.calls("POST", "/api/recipe/") == 0
    assert tandoor.calls("GET", "/api/recipe/") == 0
    (changes,) = tandoor.bodies("PATCH", "/api/recipe/1/")
    assert changes["name"] == CARD["name"]
    assert [[stored(i) for i in s["ingredients"]] for s in changes["steps"]] == EXPECTED
    assert changes["keywords"] == [{"name": k} for k in CARD["keywords"]]
    for field in ("description", "servings", "servings_text", "working_time"):
        assert changes[field] == CARD[field]
    assert "internal" not in changes and "image" not in changes

    assert text.startswith(f"Replaced the content of {CARD['name']} (recipe 1): 5 steps, 17 ingredients.")
    assert "already has an image" in text
    url = text.split("Upload link for its image: ", 1)[1].split()[0]
    assert links.get(url.rsplit("/", 1)[1]).recipe_id == 1


def test_fields_left_out_are_kept(server, tandoor):
    save(server, name="Udon", steps=[{"instruction": "Cuire."}], recipe_id=1)
    (changes,) = tandoor.bodies("PATCH", "/api/recipe/1/")
    assert set(changes) == {"name", "steps"}


def test_replacing_needs_an_existing_recipe(server, tandoor):
    with pytest.raises(ToolError, match="no recipe 99"):
        save(server, **CARD, recipe_id=99)


# --- refusals and failures -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        {"name": "x" * 129},
        {"name": "  "},
        {"description": "x" * 513},
        {"keywords": ["a", "b", "c", "d"]},
        {"keywords": ["x" * 65]},
        {"steps": []},
        {"steps": [{"name": "x" * 129}]},
        {"steps": [{"ingredients": [{"food": "sel", "note": "x" * 257}]}]},
        {"steps": [{"ingredients": [{"food": "x" * 129}]}]},
        {"steps": [{"ingredients": [{"amount": 1}]}]},
        {"steps": [{"ingredients": [{"food": "sel", "header": "Sauce"}]}]},
        {"steps": [{"ingredients": [{"header": "Sauce", "amount": 1}]}]},
        {"steps": [{"ingredients": [{"food": "sel", "no_amount": True}]}]},
        {"steps": [{"ingredients": [{"food": "sel", "amount": -1}]}]},
    ],
)
def test_refused_before_any_request(server, tandoor, arguments):
    with pytest.raises(ToolError):
        save(server, **({"name": "Salade", "steps": [{"instruction": "Mélanger."}]} | arguments))
    assert tandoor.requests == []


@pytest.mark.parametrize("status", [401, 403, 429])
@pytest.mark.parametrize(
    ("method", "path", "replace"), [("POST", "/api/recipe/", {}), ("PATCH", "/api/recipe/1/", {"recipe_id": 1})]
)
def test_writes_refused_before_saving(server, tandoor, status, method, path, replace):
    tandoor.failures[(method, path)] = status
    with pytest.raises(ToolError, match=f"Tandoor answered {status}. Nothing was saved"):
        save(server, **CARD, **replace)


def test_tandoor_refusal_is_passed_on(server, tandoor):
    tandoor.failures[("POST", "/api/recipe/")] = 400
    with pytest.raises(ToolError, match=r"refused the recipe: .*This field is required.*Nothing was saved"):
        save(server, **CARD)


@pytest.mark.parametrize("failure", [500, 502, "down"])
def test_a_failed_create_may_have_saved_part(server, tandoor, failure):
    tandoor.failures[("POST", "/api/recipe/")] = failure
    with pytest.raises(ToolError, match="Part of the recipe may have been saved: check list_recipes"):
        save(server, **CARD)
    assert tandoor.calls("POST", "/api/recipe/") == 1


def test_a_failed_replace_may_have_changed_part(server, tandoor):
    tandoor.failures[("PATCH", "/api/recipe/1/")] = 500
    with pytest.raises(ToolError, match="Recipe 1 may be partly updated: check it with get_recipe"):
        save(server, **CARD, recipe_id=1)


@pytest.mark.parametrize("path", ["/api/unit/", "/api/recipe/"])
def test_failed_lookups_save_nothing(server, tandoor, path):
    tandoor.failures[("GET", path)] = "down"
    with pytest.raises(ToolError, match=r"couldn't reach Tandoor \(ConnectError\)\. Nothing was saved"):
        save(server, **CARD)
    assert tandoor.calls("POST", "/api/recipe/") == 0


# --- what Claude sees ------------------------------------------------------------------------------------------------


def test_tool_schema(server):
    mcp, _ = server

    async def tool():
        async with Client(mcp) as client:
            return next(t for t in await client.list_tools() if t.name == "save_recipe_from_photo")

    tool = asyncio.run(tool())
    schema = tool.input_schema
    assert schema["required"] == ["name", "steps"]
    assert {"recipe_id", "allow_duplicate", "keywords", "servings"} <= set(schema["properties"])
    step = schema["properties"]["steps"]["items"]
    assert {"name", "instruction", "time", "ingredients"} == set(step["properties"])
    assert {"food", "amount", "unit", "note", "header"} == set(step["properties"]["ingredients"]["items"]["properties"])
    # Inlined for clients, which don't all follow $ref.
    assert "$defs" not in json.dumps(schema) and "$ref" not in json.dumps(schema)
    assert tool.description == SAVE_TOOL_DESCRIPTION
    assert (tool.annotations.read_only_hint, tool.annotations.destructive_hint) == (False, True)


def test_prompt_and_tool_share_the_rules():
    for rule in TRANSCRIPTION_RULES:
        assert rule in PHOTO_IMPORT_PROMPT
        assert rule in SAVE_TOOL_DESCRIPTION
    assert "save_recipe_from_photo" in PHOTO_IMPORT_PROMPT
    assert "create_recipe" not in PHOTO_IMPORT_PROMPT
