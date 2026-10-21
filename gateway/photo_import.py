"""Adding a recipe to Tandoor from a photo, screenshot or PDF shared with Claude.

Claude reads the recipe itself and saves it with save_recipe_from_photo, which writes it to Tandoor with what the
child's create_recipe can't carry: step names, ingredients without an amount, and sub-headings. The tool's description
holds the transcription rules, so Claude sees them even when the user doesn't start from the import_recipe_from_photo
prompt.

What a tool can't do is attach the photo: a tool call carries JSON that Claude writes, not the chat's files, so the
image bytes never reach a tool. Instead create_image_upload_link (and save_recipe_from_photo's result) hands out a
short-lived, single-use link where the user (or Claude's code sandbox, if it can reach this host) uploads the photo.
The gateway checks it and sets it as the recipe's image.
"""

import html
import logging
import secrets
import time
import unicodedata
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlsplit

import httpx2
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, Response

logger = logging.getLogger("tandoor_gateway")

MAX_IMAGE_BYTES = 10 * 1024 * 1024
# Room for the multipart boundary and part headers around the image.
MAX_FORM_BYTES = MAX_IMAGE_BYTES + 64 * 1024
LINK_TTL_SECONDS = 30 * 60
MAX_LINKS = 50

# The image types Tandoor accepts, and the extension of the file it stores.
IMAGE_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def sniff_image_type(data: bytes) -> str | None:
    """The MIME type from the image's first bytes, if Tandoor accepts it. What the uploader claims is ignored."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@dataclass(frozen=True)
class UploadLink:
    recipe_id: int
    recipe_name: str
    expires: float


class UploadLinks:
    """Outstanding upload links, kept in memory: a restart only expires them early."""

    def __init__(self, *, ttl: float = LINK_TTL_SECONDS, limit: int = MAX_LINKS, clock=time.monotonic):
        self.ttl = ttl
        self._limit = limit
        self._clock = clock
        self._links: dict[str, UploadLink] = {}

    def issue(self, recipe_id: int, recipe_name: str) -> str:
        self._purge()
        while len(self._links) >= self._limit:
            del self._links[next(iter(self._links))]  # the oldest
        token = secrets.token_urlsafe(32)
        self._links[token] = UploadLink(recipe_id, recipe_name, self._clock() + self.ttl)
        return token

    def get(self, token: str) -> UploadLink | None:
        self._purge()
        return self._links.get(token)

    def claim(self, token: str) -> UploadLink | None:
        """Takes the link for one upload, so a second request with it fails. restore() gives it back."""
        self._purge()
        return self._links.pop(token, None)

    def restore(self, token: str, link: UploadLink) -> None:
        self._links[token] = link

    def _purge(self) -> None:
        now = self._clock()
        for token in [t for t, link in self._links.items() if link.expires <= now]:
            del self._links[token]


class Tandoor:
    """The Tandoor API calls the gateway makes itself. The child makes all the others."""

    def __init__(self, url: str, token: str, *, transport: httpx2.AsyncBaseTransport | None = None):
        # Like the child: the URL's origin, https:// when it has no scheme.
        parts = urlsplit(url if "://" in url else f"https://{url}")
        self._base_url = f"{parts.scheme}://{parts.netloc}"
        self._headers = {"Authorization": f"Bearer {token}"}
        self._transport = transport

    def _client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            base_url=self._base_url, headers=self._headers, transport=self._transport, timeout=30
        )

    async def recipe_name(self, recipe_id: int) -> str | None:
        """The recipe's name, or None when Tandoor has no such recipe."""
        async with self._client() as client:
            response = await client.get(f"/api/recipe/{recipe_id}/")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json().get("name") or f"Recipe {recipe_id}"

    async def recipes_named(self, name: str) -> list[dict]:
        """The recipes called name, ignoring case. Tandoor itself lets two recipes share a name."""
        async with self._client() as client:
            response = await client.get("/api/recipe/", params={"query": name, "page_size": 50})
        response.raise_for_status()
        return [r for r in response.json().get("results", []) if fold(r.get("name") or "") == fold(name)]

    async def units(self) -> list[dict]:
        """Every unit in the space."""
        units = []
        async with self._client() as client:
            for page in range(1, 51):
                response = await client.get("/api/unit/", params={"page_size": 100, "page": page})
                response.raise_for_status()
                data = response.json()
                units += data.get("results", [])
                if not data.get("next"):
                    break
        return units

    async def create_recipe(self, recipe: dict) -> dict:
        async with self._client() as client:
            response = await client.post("/api/recipe/", json=recipe)
        response.raise_for_status()
        return response.json()

    async def update_recipe(self, recipe_id: int, changes: dict) -> dict | None:
        """The updated recipe, or None when Tandoor has no such recipe."""
        async with self._client() as client:
            response = await client.patch(f"/api/recipe/{recipe_id}/", json=changes)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def set_recipe_image(self, recipe_id: int, data: bytes, mime_type: str) -> None:
        filename = f"recipe-{recipe_id}.{IMAGE_EXTENSIONS[mime_type]}"
        async with self._client() as client:
            response = await client.put(
                f"/api/recipe/{recipe_id}/image/", files={"image": (filename, data, mime_type)}
            )
        response.raise_for_status()


def fold(name: str) -> str:
    return name.strip().casefold()


# --- saving a transcribed recipe ----------------------------------------------------------------------------------

# Units French recipes use, by the name the gateway gives them: their other spellings, and the base unit a new one gets
# so that Tandoor can convert it. Existing units are never changed.
FRENCH_UNITS = {
    "c. à soupe": (
        ("c. à s.", "c. à table", "cuillère à soupe", "cuillères à soupe", "cuillère à table", "cuillères à table",
         "cuillerée à soupe", "cuillerées à soupe"),
        "tbsp",
    ),
    "c. à thé": (
        ("c. à t.", "c. à c.", "c. à café", "cuillère à thé", "cuillères à thé", "cuillère à café", "cuillères à café",
         "cuillerée à thé", "cuillerées à thé", "cuillerée à café", "cuillerées à café"),
        "tsp",
    ),
    "tasse": (("tasses",), "us_cup"),
    "pincée": (("pincées",), None),
    "gousse": (("gousses",), None),
    "branche": (("branches",), None),
}


def unit_key(name: str) -> str:
    """Ignores case, accents, spaces and periods, so that "C. à s." and "c.a.s" are the same unit."""
    return "".join(c for c in unicodedata.normalize("NFKD", name.casefold()) if c.isalnum())


# Every spelling of a French unit, by its key: all of the unit's spellings, canonical name first.
UNIT_SPELLINGS = {
    unit_key(spelling): (name, *aliases) for name, (aliases, _) in FRENCH_UNITS.items() for spelling in (name, *aliases)
}


class Units:
    """Matches the units written on a recipe with the space's, so that spelling variants don't each create a unit."""

    def __init__(self, existing: list[dict]):
        # What Tandoor itself finds: it matches a unit's name or plural, ignoring case but not accents.
        self._tandoor_finds = {
            n.strip().casefold() for unit in existing for n in (unit.get("name"), unit.get("plural_name")) if n
        }
        self._names: dict[str, str] = {}
        for field in ("plural_name", "name"):
            for unit in existing:
                if unit.get(field) and unit_key(unit[field]):
                    self._names[unit_key(unit[field])] = unit["name"]

    def payload(self, written: str | None) -> dict | None:
        """The unit for Tandoor's recipe serializer, which finds an existing unit by name or creates it."""
        if not written or not unit_key(written):
            return None
        if written.casefold() in self._tandoor_finds:
            return {"name": written}
        spellings = UNIT_SPELLINGS.get(unit_key(written), ())
        for key in [unit_key(s) for s in spellings] + [unit_key(written)]:
            if key in self._names:
                return {"name": self._names[key]}
        if not spellings:
            return {"name": written}
        name, base_unit = spellings[0], FRENCH_UNITS[spellings[0]][1]
        return {"name": name, "base_unit": base_unit} if base_unit else {"name": name}


def text(max_length: int, min_length: int = 0) -> StringConstraints:
    """A stripped string within Tandoor's length limit for the field. Its writes aren't atomic, so whatever it would
    refuse halfway through a recipe has to be refused here, before the first request."""
    return StringConstraints(strip_whitespace=True, min_length=min_length, max_length=max_length)


class PhotoIngredient(BaseModel):
    """One line of a step's ingredient list: an ingredient, or a sub-heading."""

    model_config = ConfigDict(extra="forbid")

    food: Annotated[str, text(128, 1)] | None = Field(None, description='A short singular name, such as "oignon vert".')
    amount: float | None = Field(None, ge=0, description='Leave it out when the recipe gives none ("au goût").')
    unit: Annotated[str, text(128)] | None = Field(None, description='As written, such as "g", "tasse" or "c. à s.".')
    note: Annotated[str, text(256)] | None = Field(
        None, description='Preparation, size, an alternative, the share of a split ingredient, or "au goût".'
    )
    header: Annotated[str, text(256, 1)] | None = Field(
        None, description='A sub-heading instead of an ingredient, such as "Pour la sauce", with no other field.'
    )

    @model_validator(mode="after")
    def food_or_header(self):
        if (self.food is None) == (self.header is None):
            raise ValueError("give either food or header")
        if self.header is not None and (self.amount, self.unit, self.note) != (None, None, None):
            raise ValueError("a header has no amount, unit or note")
        return self


class PhotoStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, text(128)] = Field(
        "", description='The step\'s heading, such as "Cuire le riz". Not repeated in the instruction.'
    )
    instruction: Annotated[str, StringConstraints(strip_whitespace=True)] = ""
    time: int = Field(0, ge=0, description="Minutes, where a timer helps.")
    ingredients: list[PhotoIngredient] = Field(default_factory=list)


def ingredient_payload(order: int, ingredient: PhotoIngredient, units: Units) -> dict:
    if ingredient.header is not None:
        # As Tandoor's own importers write a sub-heading.
        return {
            "food": None, "unit": None, "amount": 0, "note": ingredient.header, "order": order,
            "is_header": True, "no_amount": True,
        }
    return {
        "food": {"name": ingredient.food},
        "unit": units.payload(ingredient.unit),
        "amount": ingredient.amount or 0,
        "note": ingredient.note or None,
        "order": order,
        "is_header": False,
        # 0 too: that's how create_recipe used to write "au goût".
        "no_amount": not ingredient.amount,
    }


def step_payload(order: int, step: PhotoStep, units: Units) -> dict:
    return {
        "name": step.name,
        "instruction": step.instruction,
        "time": step.time,
        "order": order,
        # Tandoor's overview of the steps shows a step's name only with show_as_header.
        "show_as_header": bool(step.name),
        "ingredients": [ingredient_payload(i, ingredient, units) for i, ingredient in enumerate(step.ingredients)],
    }


def plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


TRANSCRIPTION_RULES = (
    "If it's a screenshot of a web page and you can read the page's URL, offer import_recipe_from_url instead.",
    "Read the recipe yourself; don't use Tandoor's AI import. Combine all the images or pages of one recipe, such as "
    "the front and back of a card.",
    "Write in the recipe's own language. Rewrite the steps concisely, but keep every action, amount, time and "
    'temperature. Make cues explicit ("Entretemps" → "Pendant ce temps") and add °C after °F.',
    "Use the recipe's own steps, each with its heading as the step's name, not repeated in the instruction. Set a "
    "step's time, in minutes, where a timer helps.",
    "Keep ingredient amounts and units as written: don't convert them. Write fractions as decimals (1½ → 1.5). For a "
    "range such as 2–3, use the first number and put the range in the note.",
    "Put each ingredient in the step that uses it, with the amount used there. Split one used in several steps by "
    'share, and say so in the note ("¾ du miel", "le reste"). Add the pantry items the recipe takes for granted '
    '("Il vous faudra : huile, sel et poivre") and amounts that only appear in the steps, so that they scale and reach '
    "the shopping list.",
    'Give the food a short singular name ("oignon vert"); Tandoor finds or creates foods by name, ignoring case, so '
    'check list_foods when it may have one under another name. Preparation, size, alternatives ("ou 2 poitrines de '
    'poulet") and what a blend contains go in the note. With no amount ("au goût", "pour huiler"), leave amount out '
    'and put those words in the note. A sub-heading in the ingredient list ("Pour la sauce") is a header.',
    'Set servings to the base yield (2 for "2 ou 4 portions"; Tandoor scales it), and working_time and waiting_time '
    "only when the recipe states them. For the description (512 characters at most), sum up its introduction in a "
    'sentence or two, then give the calories per serving, the allergens and the source ("Source : carte Goodfood"). '
    "Add at most three keywords, such as the main ingredient, the appliance and the source, and reuse existing ones.",
    "Keep safety notes, such as a minimum internal temperature. Leave out hashtags, social handles, generic URLs and "
    "card codes.",
    "If anything is illegible, cut off or ambiguous, ask about it before saving. Don't guess.",
)

PHOTO_IMPORT_PROMPT = (
    "Add the recipe in the photo, screenshot or PDF I've shared to Tandoor.\n\n"
    + "".join(f"{number}. {rule}\n" for number, rule in enumerate(TRANSCRIPTION_RULES, 1))
    + "\nSave it with save_recipe_from_photo. If one of the images shows the finished dish, offer me the upload link "
    "it returns, to attach that photo as the recipe's image.\n"
)

SAVE_TOOL_DESCRIPTION = (
    "Save a recipe you read from a photo, screenshot or PDF in the chat to Tandoor. Unlike create_recipe, it keeps "
    "step names, sub-headings and ingredients without an amount. Transcribe the recipe this way:\n"
    + "".join(f"- {rule}\n" for rule in TRANSCRIPTION_RULES)
    + "If Tandoor already has a recipe with the same name, the tool refuses: ask the user whether to replace that "
    "recipe (recipe_id) or keep both (allow_duplicate). With recipe_id, the recipe's steps and every field you give "
    "are replaced, fields you leave out keep their value, and its image stays. Use recipe_id only when the user asked "
    "to replace or fill in that recipe. Returns the recipe's id and a one-time link for uploading its image."
)

UPLOAD_TOOL_DESCRIPTION = """\
Get a one-time link for attaching a photo to a recipe as its image (JPEG, PNG or WebP, up to 10 MB). Tool calls can't \
carry a photo from the chat, so give the link to the user: they open it on the device that has the photo and choose \
it. The upload replaces the recipe's current image. The link works once, for 30 minutes. If you can run code that \
reaches the link's host, you can upload the image yourself instead, such as a crop of the dish: POST it to the link \
as multipart/form-data field "image"."""

PAGE_HEADERS = {
    "Cache-Control": "no-store",
    # The token is in the URL.
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
    "frame-ancestors 'none'",
}

EXPIRED = "This link has expired or was already used. Ask Claude for a new one."


def describe(exc: httpx2.HTTPError) -> str:
    if isinstance(exc, httpx2.HTTPStatusError):
        return f"Tandoor answered {exc.response.status_code}"
    return f"couldn't reach Tandoor ({type(exc).__name__})"


def partly_saved(recipe_id: int | None) -> str:
    """After a failed write: Tandoor saves a recipe's parts one by one, without a transaction."""
    if recipe_id is None:
        return "Part of the recipe may have been saved: check list_recipes before trying again."
    return f"Recipe {recipe_id} may be partly updated: check it with get_recipe before trying again."


def page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="robots" content="noindex">
<title>{html.escape(title)}</title>
<style>
body {{ font: 17px/1.5 system-ui, sans-serif; max-width: 34rem; margin: 2rem auto; padding: 0 1rem; }}
input, button {{ font: inherit; display: block; margin: 1rem 0; }}
button {{ padding: 0.6rem 1.2rem; }}
</style>
</head>
<body>
<h1>{html.escape(title)}</h1>
{body}
</body>
</html>
"""


def reply(request: Request, status: int, title: str, message: str) -> Response:
    """A page for a browser, plain text for anything else (curl from Claude's sandbox)."""
    if "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(page(title, f"<p>{html.escape(message)}</p>"), status, headers=PAGE_HEADERS)
    return PlainTextResponse(f"{message}\n", status, headers=PAGE_HEADERS)


def add_photo_import(
    mcp: FastMCP, *, tandoor: Tandoor, public_base_url: str, links: UploadLinks | None = None
) -> UploadLinks:
    """Adds the photo-import prompt, save_recipe_from_photo, create_image_upload_link and its /upload/<token> page to
    the gateway."""
    if links is None:
        links = UploadLinks()

    def upload_url(recipe_id: int, name: str) -> str:
        return f"{public_base_url}/upload/{links.issue(recipe_id, name)}"

    @mcp.prompt(
        name="import_recipe_from_photo",
        description="Add the recipe in a photo, screenshot or PDF in this chat to Tandoor, read by Claude.",
    )
    def import_recipe_from_photo() -> str:
        return PHOTO_IMPORT_PROMPT

    @mcp.tool(
        name="create_image_upload_link",
        description=UPLOAD_TOOL_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=False, open_world_hint=False),
    )
    async def create_image_upload_link(recipe_id: Annotated[int, Field(description="Recipe ID", ge=1)]) -> str:
        try:
            name = await tandoor.recipe_name(recipe_id)
        except httpx2.HTTPError as exc:
            raise ToolError(f"Couldn't look up recipe {recipe_id}: {describe(exc)}.") from exc
        if name is None:
            raise ToolError(f"Tandoor has no recipe {recipe_id}.")
        return (
            f"Upload link for {name} (recipe {recipe_id}): {upload_url(recipe_id, name)}\n"
            f"It works once, for {round(links.ttl / 60)} minutes, and replaces the recipe's current image."
        )

    @mcp.tool(
        name="save_recipe_from_photo",
        description=SAVE_TOOL_DESCRIPTION,
        # Destructive with recipe_id, which replaces a recipe's content.
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
    )
    async def save_recipe_from_photo(
        name: Annotated[str, text(128, 1)],
        steps: Annotated[list[PhotoStep], Field(min_length=1)],
        description: Annotated[str, text(512)] | None = None,
        servings: Annotated[int | None, Field(ge=1)] = None,
        servings_text: Annotated[
            Annotated[str, text(32)] | None, Field(description='What a serving is, such as "portions".')
        ] = None,
        working_time: Annotated[int | None, Field(ge=0, description="Minutes")] = None,
        waiting_time: Annotated[int | None, Field(ge=0, description="Minutes")] = None,
        keywords: Annotated[list[Annotated[str, text(64, 1)]] | None, Field(max_length=3)] = None,
        recipe_id: Annotated[
            int | None, Field(ge=1, description="Replace this recipe's content instead of creating a recipe.")
        ] = None,
        allow_duplicate: Annotated[
            bool, Field(description="Create the recipe even though Tandoor has one with the same name.")
        ] = False,
    ) -> str:
        fields = {
            "description": description, "servings": servings, "servings_text": servings_text,
            "working_time": working_time, "waiting_time": waiting_time,
        }
        try:
            units = Units(await tandoor.units() if any(i.unit for s in steps for i in s.ingredients) else [])
            same_name = [] if recipe_id or allow_duplicate else await tandoor.recipes_named(name)
        except httpx2.HTTPError as exc:
            raise ToolError(f"Couldn't save the recipe: {describe(exc)}. Nothing was saved.") from exc
        if same_name:
            found = ", ".join(f"{r.get('name')} (recipe {r.get('id')})" for r in same_name)
            raise ToolError(
                f"Tandoor already has {found}. Ask the user whether to replace it (recipe_id) or keep both "
                "(allow_duplicate). Nothing was saved."
            )

        recipe = {"name": name, "steps": [step_payload(i, step, units) for i, step in enumerate(steps)]}
        recipe |= {key: value for key, value in fields.items() if value is not None}
        if keywords is not None:
            recipe["keywords"] = [{"name": keyword} for keyword in keywords]
        try:
            if recipe_id is None:
                # Tandoor's editor saves every recipe as internal; external ones are labelled as such.
                saved = await tandoor.create_recipe(recipe | {"internal": True})
            else:
                saved = await tandoor.update_recipe(recipe_id, recipe)
                if saved is None:
                    raise ToolError(f"Tandoor has no recipe {recipe_id}. Nothing was saved.")
        except httpx2.HTTPStatusError as exc:
            # Tandoor checks the token, permissions, throttling and then the whole recipe before writing any of it.
            if exc.response.status_code == 400:
                raise ToolError(f"Tandoor refused the recipe: {exc.response.text[:2000]}. Nothing was saved.") from exc
            if exc.response.status_code < 500:
                raise ToolError(f"Couldn't save the recipe: {describe(exc)}. Nothing was saved.") from exc
            raise ToolError(f"Couldn't save the recipe: {describe(exc)}. {partly_saved(recipe_id)}") from exc
        except httpx2.HTTPError as exc:
            raise ToolError(f"Couldn't save the recipe: {describe(exc)}. {partly_saved(recipe_id)}") from exc

        saved_id, saved_name = saved["id"], saved.get("name") or name
        ingredients = sum(1 for step in steps for i in step.ingredients if i.header is None)
        image = (
            "It already has an image, which an upload replaces: offer the link only if the user wants a new one."
            if saved.get("image")
            else "If a photo shows the finished dish, offer the user this link to attach it."
        )
        return (
            f"{'Saved' if recipe_id is None else 'Replaced the content of'} {saved_name} (recipe {saved_id}): "
            f"{plural(len(steps), 'step')}, {plural(ingredients, 'ingredient')}.\n"
            f"Upload link for its image: {upload_url(saved_id, saved_name)}\n"
            f"It works once, for {round(links.ttl / 60)} minutes. {image}"
        )

    @mcp.custom_route("/upload/{token}", methods=["GET"])
    async def upload_form(request: Request) -> Response:
        link = links.get(request.path_params["token"])
        if link is None:
            return HTMLResponse(page("Link expired", f"<p>{html.escape(EXPIRED)}</p>"), 404, headers=PAGE_HEADERS)
        body = f"""<p>Choose the photo for <strong>{html.escape(link.recipe_name)}</strong>. It replaces the recipe's
current image. JPEG, PNG or WebP, up to 10 MB.</p>
<form method="post" enctype="multipart/form-data">
<input type="file" name="image" accept="image/jpeg,image/png,image/webp" required>
<button>Attach photo</button>
</form>
<p>This link works once.</p>"""
        return HTMLResponse(page("Attach a photo", body), headers=PAGE_HEADERS)

    @mcp.custom_route("/upload/{token}", methods=["POST"])
    async def upload(request: Request) -> Response:
        token = request.path_params["token"]
        if links.get(token) is None:
            return reply(request, 404, "Link expired", EXPIRED)
        # The server enforces Content-Length framing, so this caps the body before it's read.
        length = request.headers.get("content-length", "")
        if not length.isdigit():
            return reply(request, 411, "Not attached", "Send the photo with a Content-Length header.")
        if int(length) > MAX_FORM_BYTES:
            return reply(request, 413, "Too large", "The photo is over 10 MB.")
        async with request.form(max_files=1, max_fields=5) as form:
            image = form.get("image")
            data = await image.read(MAX_IMAGE_BYTES + 1) if isinstance(image, UploadFile) else b""
        if not data:
            return reply(request, 400, "Not attached", 'Choose a photo first (form field "image").')
        if len(data) > MAX_IMAGE_BYTES:
            return reply(request, 413, "Too large", "The photo is over 10 MB.")
        mime_type = sniff_image_type(data)
        if mime_type is None:
            return reply(request, 415, "Not attached", "That isn't a JPEG, PNG or WebP image.")

        link = links.claim(token)
        if link is None:
            return reply(request, 404, "Link expired", EXPIRED)
        try:
            await tandoor.set_recipe_image(link.recipe_id, data, mime_type)
        except httpx2.HTTPError as exc:
            links.restore(token, link)
            logger.warning("Couldn't set the image of recipe %d: %s", link.recipe_id, exc)
            return reply(request, 502, "Not attached", f"The photo wasn't attached: {describe(exc)}. Try again.")
        logger.info("Attached a %d-byte %s image to recipe %d", len(data), mime_type, link.recipe_id)
        return reply(request, 200, "Photo attached", f"The photo is now the image of {link.recipe_name}.")

    return links
