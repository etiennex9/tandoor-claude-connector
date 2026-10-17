# Tandoor ↔ Claude connector

Lets Claude (web, desktop and the phone apps) read and update your Tandoor Recipes instance: recipes, foods on hand, the shopping list and the meal plan.

```
Claude app ─▶ Anthropic cloud ─HTTPS─▶ nginx  recipe.example.com
                                         ├─ /claude/*, /.well-known/oauth-*  ─▶ gateway container (127.0.0.1:8737)
                                         └─ everything else                   ─▶ Tandoor (unchanged)

gateway container
 ├─ FastMCP gateway (Python): OAuth for Claude, sign-in delegated to Authentik
 └─ tandoor-recipes-mcp (Node, stdio child) ── Bearer <Tandoor API token> ─▶ Tandoor API
```

- **Claude → gateway:** OAuth. Claude registers itself with the gateway automatically, and you sign in with Authentik SSO.
- **Gateway → Tandoor:** one Tandoor API token. Everything done through Claude shows up in Tandoor as that token's user.
- **Connector URL:** `PUBLIC_BASE_URL` + `/mcp`, e.g. `https://recipe.example.com/claude/mcp`

Examples below use `recipe.example.com` for Tandoor and `login.example.com` for Authentik. Your real hosts go in `.env` (gitignored); [.env.example](.env.example) is the committed reference.

## Requirements

- Tandoor **2.3.6 or later** (check Tandoor's System / About page).
- Docker with Compose v2 on a host nginx can reach.
- `recipe.example.com` reachable from the internet over HTTPS. Claude's requests come from Anthropic's servers (`160.79.104.0/21`), not from your phone.

## Setup

### 1. Tandoor API token
1. In Tandoor go to **Settings → API Tokens** and create a token named `claude-mcp`.
2. Give it read/write scope and an expiry date.
3. Copy it for `TANDOOR_TOKEN`.

The token works no matter how you log in to Tandoor (SSO or password).

### 2. Authentik
1. **Applications → Providers → Create → OAuth2/OpenID Provider**:
   - Name: `claude-tandoor`
   - Client type: **Confidential**
   - Redirect URI: **Strict**, `https://recipe.example.com/claude/auth/callback`
   - **Signing key:** pick one, such as the self-signed certificate. Access tokens must be RS256 JWTs.
   - Advanced protocol settings:
     - Scopes: `openid`, `email`, `profile` **and `offline_access`**. Without `offline_access` there's no refresh token, and Claude will ask you to reconnect often.
     - Access token validity: `hours=1`
     - Refresh token validity: `days=30` or more
2. **Applications → Applications → Create**:
   - Name and slug: `claude-tandoor`
   - Provider: `claude-tandoor`
3. On that application's **Policy / Group / User Bindings**, bind a group that contains only the people who may connect (you and your SO). Users outside it can't sign in.
4. From the provider, copy the **Client ID** and **Client Secret**.

The discovery URL is `https://login.example.com/application/o/claude-tandoor/.well-known/openid-configuration`.

### 3. Configure and start
```bash
cp .env.example .env
openssl rand -hex 32
```
Fill in `.env`:
- Put the output of `openssl rand -hex 32` in `JWT_SIGNING_KEY`.
- Add the Authentik client ID and secret, and the Tandoor token.
- Optionally, list the allowed Authentik emails in `ALLOWED_USERS`.

```bash
docker compose up -d --build
docker compose logs -f gateway
```
The log should show `tandoor-recipes-mcp@2.0.1 ... tandoor=2.x` on the first request.

### 4. nginx
Copy the two `location` blocks from [nginx/claude-gateway.conf](nginx/claude-gateway.conf) into your existing HTTPS `server` block for `recipe.example.com`. Then:
```bash
sudo nginx -t
```
```bash
sudo systemctl reload nginx
```
That file also has an optional block that limits `/claude/mcp` to Anthropic's IP range.

### 5. Claude
1. Go to claude.ai → **Customize → Connectors → Add custom connector**.
   - Name: `Tandoor`
   - URL: `https://recipe.example.com/claude/mcp`
   - Authentication: sign-in required. OAuth client: **Register automatically**. Leave the client ID and secret blank.
2. Click **Connect**. A gateway consent page appears; click **Allow Access**. Then sign in with Authentik.
3. Open the connector and set read tools to **Always allow**. Leave write tools on **Needs approval**.
4. On your phone, open a chat and turn the connector on under **+ → Connectors**.

Things to try:
- "What's on hand?"
- "What can I make tonight with what's on hand?"
- "Add milk to the shopping list."
- "Import https://… and plan it for Friday dinner."

## Checks

```bash
curl -i https://recipe.example.com/claude/mcp
```
Should return `401` with a `WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource/claude/mcp"` header.

```bash
curl -s https://recipe.example.com/.well-known/oauth-protected-resource/claude/mcp
```
Should return JSON whose `resource` is `https://recipe.example.com/claude/mcp`.

```bash
curl -s https://recipe.example.com/.well-known/oauth-authorization-server
```
Should list `registration_endpoint` and `S256`.

Tandoor itself (`https://recipe.example.com/`) should behave exactly as before.

## What Claude can do

The tool list is set in `docker-compose.yml` (`TANDOOR_MCP_INCLUDE_ONLY`). `TANDOOR_MCP_EXCLUDE` adds a second layer of protection.

| Area | Tools |
|---|---|
| Read | search/list/get recipes, related recipes, foods, ingredients, keywords, meal plans and types, shopping entries, cook log, units, version |
| Pantry | `update_food`, `food_batch_update`, `create_food`, `food_shopping_update` (set or clear "on hand") |
| Shopping list | create/update/delete entries, bulk check, add a recipe's ingredients |
| Meal plan | create/update/delete entries |
| Recipes | `import_recipe_from_url`, `create_cook_log` |

Not exposed:
- Deleting or merging recipes or foods.
- Admin, API-token, invite, user, space, storage, AI-provider and import/export tools.

To change the tool list, edit the list and run `docker compose up -d`.

Tandoor stores "on hand" as a **yes/no flag per food**, with no quantities or fridge/pantry locations.

## Maintenance

- **Rotate the Tandoor token:** create a new one, update `.env`, run `docker compose up -d`, then delete the old token in Tandoor.
- **Sign everyone out:** change `JWT_SIGNING_KEY`, or run `docker compose down -v` to also drop stored registrations. Claude will ask you to reconnect.
- **Upgrade:** bump `fastmcp` in `gateway/requirements.txt` or the `tandoor-recipes-mcp` version in `gateway/Dockerfile`, then run `docker compose up -d --build`.
- **Unit tests:**
  ```bash
  docker run --rm --user root -e PYTHONPATH=/src -v "$PWD/gateway:/src" -w /src tandoor-claude-gateway sh -c "pip install -q pytest && python -m pytest -q tests"
  ```

## Notes

- tandoor-recipes-mcp runs over **stdio** inside the gateway container. Its built-in HTTP mode (2.0.1) answers only the first request and returns 500 for every later one, because it reuses a stateless MCP SDK transport.
- The gateway issues its own short-lived tokens to Claude. It stores the Authentik refresh tokens encrypted in the `gateway-data` volume and checks them against Authentik on every request, so disabling a user in Authentik cuts off their access.
