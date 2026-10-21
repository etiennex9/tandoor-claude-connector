import asyncio

import httpx2
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server import create_proxy
from starlette.testclient import TestClient

from photo_import import MAX_IMAGE_BYTES, Tandoor, UploadLinks, add_photo_import, sniff_image_type

BASE = "https://recipe.example.com/claude"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 64


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeTandoor:
    """Answers the gateway's two Tandoor calls and records them."""

    def __init__(self, recipes=None, image_status=200):
        self.recipes = {7: "Mac & cheese <3"} if recipes is None else recipes
        self.image_status = image_status
        self.down = False
        self.requests = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.down:
            raise httpx2.ConnectError("connection refused", request=request)
        path = request.url.path
        recipe_id = int(path.split("/")[3])
        if recipe_id not in self.recipes:
            return httpx2.Response(404, json={"detail": "Not found."})
        if request.method == "GET" and path == f"/api/recipe/{recipe_id}/":
            return httpx2.Response(200, json={"id": recipe_id, "name": self.recipes[recipe_id]})
        if request.method == "PUT" and path == f"/api/recipe/{recipe_id}/image/":
            return httpx2.Response(self.image_status, json={"image": "https://recipe.example.com/media/x.jpg"})
        return httpx2.Response(405)

    @property
    def uploads(self):
        return [r for r in self.requests if r.method == "PUT"]


@pytest.fixture
def tandoor():
    return FakeTandoor()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def server(tandoor, clock):
    mcp = FastMCP("test")
    links = add_photo_import(
        mcp,
        tandoor=Tandoor("recipe.example.com/some/page", "secret-token", transport=httpx2.MockTransport(tandoor)),
        public_base_url=BASE,
        links=UploadLinks(clock=clock),
    )
    return mcp, links


@pytest.fixture
def http(server):
    mcp, _ = server
    return TestClient(mcp.http_app(path="/mcp"))


def path_of(token):
    return f"/upload/{token}"


# --- image types --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("data", "mime_type"), [(JPEG, "image/jpeg"), (PNG, "image/png"), (WEBP, "image/webp")])
def test_accepted_image_types(data, mime_type):
    assert sniff_image_type(data) == mime_type


@pytest.mark.parametrize(
    "data",
    [b"", b"GIF89a" + b"\x00" * 16, b"\x00\x00\x00\x18ftypheic", b"%PDF-1.7", b"RIFF\x00\x00\x00\x00WAVE", b"<svg/>"],
)
def test_other_files_are_refused(data):
    assert sniff_image_type(data) is None


# --- links --------------------------------------------------------------------------------------------------------


def test_link_expires(clock):
    links = UploadLinks(ttl=60, clock=clock)
    token = links.issue(7, "Soup")
    clock.now += 59
    assert links.get(token).recipe_id == 7
    clock.now += 1
    assert links.get(token) is None


def test_link_is_single_use_until_restored(clock):
    links = UploadLinks(clock=clock)
    token = links.issue(7, "Soup")
    link = links.claim(token)
    assert link.recipe_name == "Soup"
    assert links.claim(token) is None
    links.restore(token, link)
    assert links.get(token) == link


def test_tokens_are_unguessable(clock):
    links = UploadLinks(clock=clock)
    tokens = {links.issue(7, "Soup") for _ in range(20)}
    assert len(tokens) == 20
    assert all(len(t) >= 43 for t in tokens)


def test_oldest_link_goes_past_the_limit(clock):
    links = UploadLinks(limit=2, clock=clock)
    first, second, third = (links.issue(i, "Soup") for i in (1, 2, 3))
    assert links.get(first) is None
    assert links.get(second) and links.get(third)


# --- MCP: tool and prompt -----------------------------------------------------------------------------------------


def run(coro):
    return asyncio.run(coro)


def test_proxy_serves_gateway_tools_next_to_the_childs(tandoor, clock):
    child = FastMCP("child")

    @child.tool
    def get_version() -> str:
        return "2.0.1"

    proxy = create_proxy(child, name="Tandoor Recipes")
    tandoor_api = Tandoor("https://recipe.example.com", "t", transport=httpx2.MockTransport(tandoor))
    add_photo_import(proxy, tandoor=tandoor_api, public_base_url=BASE, links=UploadLinks(clock=clock))

    async def check():
        async with Client(proxy) as client:
            tools = {t.name for t in await client.list_tools()}
            prompts = {p.name for p in await client.list_prompts()}
            version = await client.call_tool("get_version", {})
        return tools, prompts, version.data

    tools, prompts, version = run(check())
    assert {"get_version", "create_image_upload_link", "save_recipe_from_photo"} <= tools
    assert "import_recipe_from_photo" in prompts
    assert version == "2.0.1"


def test_upload_link_tool(server, tandoor):
    mcp, links = server

    async def call():
        async with Client(mcp) as client:
            return await client.call_tool("create_image_upload_link", {"recipe_id": 7})

    text = run(call()).content[0].text
    url = text.split(": ", 1)[1].split()[0]
    assert url.startswith(f"{BASE}/upload/")
    link = links.get(url.rsplit("/", 1)[1])
    assert (link.recipe_id, link.recipe_name) == (7, "Mac & cheese <3")
    assert "Mac & cheese <3" in text and "30 minutes" in text

    (lookup,) = tandoor.requests
    assert str(lookup.url) == "https://recipe.example.com/api/recipe/7/"
    assert lookup.headers["authorization"] == "Bearer secret-token"


def test_upload_link_tool_needs_an_existing_recipe(server, tandoor):
    mcp, links = server

    async def call():
        async with Client(mcp) as client:
            await client.call_tool("create_image_upload_link", {"recipe_id": 99})

    with pytest.raises(ToolError, match="no recipe 99"):
        run(call())


def test_upload_link_tool_reports_tandoor_errors(server, tandoor):
    mcp, _ = server
    tandoor.down = True

    async def call():
        async with Client(mcp) as client:
            await client.call_tool("create_image_upload_link", {"recipe_id": 7})

    with pytest.raises(ToolError, match="recipe 7: couldn't reach Tandoor"):
        run(call())


def test_upload_link_tool_schema(server):
    mcp, _ = server

    async def tool():
        async with Client(mcp) as client:
            return next(t for t in await client.list_tools() if t.name == "create_image_upload_link")

    tool = run(tool())
    assert tool.input_schema["required"] == ["recipe_id"]
    assert tool.input_schema["properties"]["recipe_id"]["type"] == "integer"
    assert tool.annotations.read_only_hint is False


def test_photo_prompt(server):
    mcp, _ = server

    async def get():
        async with Client(mcp) as client:
            return await client.get_prompt("import_recipe_from_photo")

    text = run(get()).messages[0].content.text
    for phrase in ("save_recipe_from_photo", "upload link", "don't use Tandoor's AI import", "Don't guess"):
        assert phrase in text


# --- the upload page ----------------------------------------------------------------------------------------------


def test_form_page(server, http):
    _, links = server
    response = http.get(path_of(links.issue(7, "Mac & cheese <3")))
    assert response.status_code == 200
    assert "Mac &amp; cheese &lt;3" in response.text
    assert 'enctype="multipart/form-data"' in response.text
    assert 'name="image"' in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_unknown_link_page(http):
    response = http.get(path_of("nope"))
    assert response.status_code == 404
    assert "expired" in response.text


def test_upload_sets_the_recipe_image_once(server, http, tandoor):
    _, links = server
    token = links.issue(7, "Soup")
    response = http.post(path_of(token), files={"image": ("IMG_0001.HEIC", JPEG, "image/heic")})
    assert response.status_code == 200, response.text
    assert "Soup" in response.text
    assert response.headers["content-type"].startswith("text/plain")

    (put,) = tandoor.uploads
    assert str(put.url) == "https://recipe.example.com/api/recipe/7/image/"
    assert put.headers["authorization"] == "Bearer secret-token"
    assert put.headers["content-type"].startswith("multipart/form-data")
    # Sent with the sniffed type, not the one the browser claimed.
    assert b'name="image"; filename="recipe-7.jpg"' in put.content
    assert b"Content-Type: image/jpeg" in put.content
    assert JPEG in put.content

    again = http.post(path_of(token), files={"image": ("a.jpg", JPEG, "image/jpeg")})
    assert again.status_code == 404
    assert len(tandoor.uploads) == 1


def test_browser_gets_a_page(server, http):
    _, links = server
    response = http.post(
        path_of(links.issue(7, "Soup")),
        files={"image": ("a.png", PNG, "image/png")},
        headers={"Accept": "text/html,application/xhtml+xml"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Photo attached" in response.text


@pytest.mark.parametrize(
    ("files", "status"),
    [
        ({"image": ("a.gif", b"GIF89a" + b"\x00" * 16, "image/gif")}, 415),
        ({"image": ("a.jpg", b"", "image/jpeg")}, 400),
        ({"photo": ("a.jpg", JPEG, "image/jpeg")}, 400),
        ({"image": ("a.jpg", JPEG + b"\x00" * (MAX_IMAGE_BYTES - len(JPEG) + 1), "image/jpeg")}, 413),
    ],
)
def test_refused_uploads_keep_the_link(server, http, tandoor, files, status):
    _, links = server
    token = links.issue(7, "Soup")
    assert http.post(path_of(token), files=files).status_code == status
    assert tandoor.uploads == []
    assert links.get(token) is not None


def test_oversized_body_is_refused_before_it_is_read(server, http, tandoor):
    _, links = server
    token = links.issue(7, "Soup")
    big = JPEG + b"\x00" * (MAX_IMAGE_BYTES + 100 * 1024)
    assert http.post(path_of(token), files={"image": ("a.jpg", big, "image/jpeg")}).status_code == 413
    assert tandoor.uploads == []


def test_upload_needs_a_live_link(server, http, tandoor, clock):
    _, links = server
    token = links.issue(7, "Soup")
    clock.now += links.ttl
    assert http.post(path_of(token), files={"image": ("a.jpg", JPEG, "image/jpeg")}).status_code == 404
    assert http.post(path_of("nope"), files={"image": ("a.jpg", JPEG, "image/jpeg")}).status_code == 404
    assert tandoor.uploads == []


def test_tandoor_failure_keeps_the_link(server, http, tandoor):
    _, links = server
    tandoor.image_status = 500
    token = links.issue(7, "Soup")
    response = http.post(path_of(token), files={"image": ("a.webp", WEBP, "image/webp")})
    assert response.status_code == 502
    assert "Tandoor answered 500" in response.text
    assert links.get(token) is not None

    tandoor.image_status = 200
    assert http.post(path_of(token), files={"image": ("a.webp", WEBP, "image/webp")}).status_code == 200


def test_upload_needs_a_content_length(server, http, tandoor):
    _, links = server
    token = links.issue(7, "Soup")
    chunks = iter([JPEG])  # sent chunked, without a Content-Length
    response = http.post(path_of(token), content=chunks, headers={"Content-Type": "multipart/form-data; boundary=x"})
    assert response.status_code == 411
    assert tandoor.uploads == []
