"""Adding a recipe to Tandoor from a photo, screenshot or PDF shared with Claude.

Claude reads the recipe itself and creates it with the child's create_recipe tool. What it can't do is attach the
photo: a tool call carries JSON that Claude writes, not the chat's files, so the image bytes never reach a tool. Instead
create_image_upload_link hands out a short-lived, single-use link where the user (or Claude's code sandbox, if it can
reach this host) uploads the photo. The gateway checks it and sets it as the recipe's image.
"""

import html
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlsplit

import httpx2
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
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

    async def set_recipe_image(self, recipe_id: int, data: bytes, mime_type: str) -> None:
        filename = f"recipe-{recipe_id}.{IMAGE_EXTENSIONS[mime_type]}"
        async with self._client() as client:
            response = await client.put(
                f"/api/recipe/{recipe_id}/image/", files={"image": (filename, data, mime_type)}
            )
        response.raise_for_status()


PHOTO_IMPORT_PROMPT = """\
Add the recipe in the photo, screenshot or PDF I've shared to Tandoor. Read it yourself; don't use Tandoor's AI import.

1. Transcribe it as written, in its own language. Keep every amount and unit as given: don't convert units or scale \
the servings. Write fractions as decimals (1½ → 1.5). For a range such as 2–3, use the first number and put the range \
in the note.
2. For each ingredient, give the food as a short singular name ("onion"), the unit as written ("g", "tbsp", "cup"), and \
preparation or size ("finely chopped", "large") in the note. With no amount ("salt to taste"), use amount 0 and put \
"to taste" in the note. When unsure whether Tandoor already has a food or unit under another name, check list_foods or \
list_units and use its name.
3. Use the recipe's own steps. Put each ingredient in the step that uses it, with the amount used there. Keep sections \
such as "For the sauce" as steps of their own. Don't add steps or ingredients the recipe doesn't have.
4. Set servings, working_time and waiting_time (in minutes) only when the recipe states them. Use its introduction, if \
any, as the description, and add at most three keywords, existing ones where they fit.
5. If the recipe spans several images or pages, combine them into one recipe.
6. If anything is illegible, cut off or ambiguous, ask me about it before creating the recipe. Don't guess.
7. Create it with create_recipe. If one of the images shows the finished dish, offer to attach it as the recipe's \
image with create_image_upload_link.

If it's a screenshot of a web page and you can read the page's URL, offer import_recipe_from_url instead.
"""

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
    """Adds the photo-import prompt, create_image_upload_link and its /upload/<token> page to the gateway."""
    if links is None:
        links = UploadLinks()

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
        url = f"{public_base_url}/upload/{links.issue(recipe_id, name)}"
        return (
            f"Upload link for {name} (recipe {recipe_id}): {url}\n"
            f"It works once, for {round(links.ttl / 60)} minutes, and replaces the recipe's current image."
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
