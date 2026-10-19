# Tandoor ↔ Claude connector

Lets Claude (web, desktop and the phone apps) read and update your Tandoor Recipes instance: recipes, foods on hand, the shopping list and the meal plan.

```
Claude app ─▶ Anthropic cloud ─HTTPS─▶ nginx  recipe.example.com
                                         ├─ /claude/*, /.well-known/oauth-*  ─▶ gateway container (TrueNAS, <NAS>:30181)
                                         └─ everything else                   ─▶ Tandoor (unchanged)

gateway container
 ├─ FastMCP gateway (Python): OAuth for Claude, sign-in delegated to Authentik
 └─ tandoor-recipes-mcp (Node, stdio child) ── Bearer <Tandoor API token> ─▶ Tandoor API
```

- **Claude → gateway:** OAuth. Claude registers itself with the gateway automatically, and you sign in with Authentik SSO.
- **Gateway → Tandoor:** one Tandoor API token. Everything done through Claude shows up in Tandoor as that token's user.
- **Connector URL:** `PUBLIC_BASE_URL` + `/mcp`, e.g. `https://recipe.example.com/claude/mcp`

Examples below use `recipe.example.com` for Tandoor and `login.example.com` for Authentik. Your real hosts and secrets go in the TrueNAS app's YAML, never in the repo: [deploy/truenas.yaml](deploy/truenas.yaml) is the committed template. With docker compose they go in `.env` (gitignored) instead, from [.env.example](.env.example).

## Requirements

- Tandoor **2.3.6 or later** (check Tandoor's System / About page).
- TrueNAS with apps (custom apps, Install via YAML), reachable from your nginx host. Or Docker with Compose v2 on the nginx host itself.
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

### 3. Deploy on TrueNAS
The image is public on GHCR, so TrueNAS pulls it without a login (see [Deploy](#deploy)). Moving an existing docker compose setup? Follow [Moving from docker compose to TrueNAS](#moving-from-docker-compose-to-truenas) instead, so Claude stays connected.

1. Create the data dataset, `/mnt/POOL/apps/tandoor-claude/data`, with the **Generic** preset. Then give it to TrueNAS's built-in `apps` user and group (`568:568`), which the YAML runs the gateway as: on the dataset's **Permissions** card, click **Edit**, type `apps` in **User** and in **Group**, check **Apply User** and **Apply Group**, and save. If the dataset already holds files, also check **Apply permissions recursively**. If **Edit** opens the ACL Editor instead (the parent dataset has an ACL), set **Owner** and **Owner Group** to `apps` with **Apply Owner** and **Apply Group**.
2. Generate the signing key:
   ```bash
   openssl rand -hex 32
   ```
3. Copy [deploy/truenas.yaml](deploy/truenas.yaml) and fill it in:
   - Your hosts in place of `recipe.example.com` and `login.example.com`.
   - The signing key in `JWT_SIGNING_KEY`.
   - The Authentik client ID and secret, and the Tandoor token.
   - Optionally, the allowed Authentik emails in `ALLOWED_USERS`.
   - `POOL`, and the port if `30181` is taken.
4. In TrueNAS, go to **Apps → Discover Apps → ⋮ → Install via YAML**, name the app `tandoor-claude`, and paste the YAML.
5. Check the app's logs. They should show `tandoor-recipes-mcp@2.0.1 ... tandoor=2.x` on the first request. If the app stops right away instead, its last log line names the path `apps` can't use: redo step 1 with **Apply permissions recursively**.

**Or with docker compose** on the nginx host. This builds the image locally and listens on `127.0.0.1:8737`:
```bash
cp .env.example .env
```
Fill in `.env` with the same values, then:
```bash
docker compose up -d --build
```
```bash
docker compose logs -f gateway
```

#### Moving from docker compose to TrueNAS
This copies the gateway's data, so Claude stays connected. The stored data is keyed and encrypted with `JWT_SIGNING_KEY`, so keep the one from your `.env`.
1. Create the data dataset as in step 3.1.
2. On the old host, in the repository folder, stop the gateway:
   ```bash
   docker compose stop gateway
   ```
3. Archive its volume to your home folder. It holds the encrypted refresh tokens, so keep it out of the repository:
   ```bash
   docker run --rm -v tandoor-claude_gateway-data:/data -v "$HOME:/out" alpine tar -czf /out/gateway-data.tgz -C /data .
   ```
4. Copy `~/gateway-data.tgz` to the NAS. There, as root, extract it into the dataset:
   ```bash
   tar -xzf gateway-data.tgz -C /mnt/POOL/apps/tandoor-claude/data
   ```
5. Give the files to `apps`. The compose volume's files belong to uid 1000, the image's own user:
   ```bash
   chown -R 568:568 /mnt/POOL/apps/tandoor-claude/data
   ```
6. Fill in the YAML as in step 3.3, but with the values from your `.env`: the **same** `JWT_SIGNING_KEY`, OIDC client and `PUBLIC_BASE_URL`. Then install the app (step 3.4).
7. Repoint nginx ([Setup step 4](#4-nginx)), then run the [Checks](#checks).
8. Remove the old stack and its volume:
   ```bash
   docker compose down -v
   ```
9. Delete `gateway-data.tgz` on both hosts.

Or skip the copy, install fresh, and reconnect the connector in Claude once.

### 4. nginx
Copy the two `location` blocks from [nginx/claude-gateway.conf](nginx/claude-gateway.conf) into your existing HTTPS `server` block for `recipe.example.com`. Replace `NAS_ADDRESS:30181` with your NAS address and the app's port (`127.0.0.1:8737` with docker compose). Then:
```bash
sudo nginx -t
```
```bash
sudo systemctl reload nginx
```
That file also has an optional block that limits `/claude/mcp` to Anthropic's IP range. On TrueNAS, the app's port is reachable from the whole LAN, which bypasses nginx, so that limit holds only if you firewall the port or bind it to an address only nginx can reach (`"<address>:30181:8000"` in the YAML). Every endpoint still requires OAuth either way.

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

The tool list is built into the image: `DEFAULT_INCLUDE_ONLY` in [gateway/server.py](gateway/server.py). `DEFAULT_EXCLUDE` adds a second layer of protection. To override either one, set `TANDOOR_MCP_INCLUDE_ONLY` or `TANDOOR_MCP_EXCLUDE` (comma-separated, `*` globs) in the TrueNAS app's environment, or in `.env` with compose. A blank value means the default, never "no filter".

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

To change the default tool list, edit it, push to `main`, then update the app in TrueNAS (see [Deploy](#deploy)). With compose, run `docker compose up -d --build`.

Tandoor stores "on hand" as a **yes/no flag per food**, with no quantities or fridge/pantry locations.

## Deploy

On every push to `main` that changes more than Markdown files, [the publish workflow](.github/workflows/publish.yml) runs the unit tests. Then, if the image's inputs changed since the workflow's last successful push run, it builds the image and pushes it to the GitHub Container Registry as `ghcr.io/etiennex9/tandoor-claude-gateway`, tagged `latest` and `sha-<commit>`. The inputs are read from [gateway/Dockerfile](gateway/Dockerfile): the Dockerfile and what it copies ([image-changed.sh](.github/scripts/image-changed.sh)). So a push that only changes tests, docs, nginx or deploy files doesn't publish a new image. A manual run (**Actions → Publish → Run workflow**), or a change to the workflow, always builds. On a branch other than `main`, a manual run pushes only `sha-<commit>`. Pull requests run the tests, and build the image without pushing it when its inputs changed. If a run fails or is canceled, the next run picks up its changes. A Markdown-only push doesn't start a run, though, so use a manual run if you don't have another push coming.

To roll out a new image, open **Apps** in TrueNAS and update the `tandoor-claude` app. It runs `latest`.

The package is public, like the repository, so TrueNAS pulls it without a login. If a pull is ever denied, open the package from the repository's **Packages** list and check **Package settings → Change visibility**.

## Maintenance

- **Rotate the Tandoor token:** create a new one, put it in `TANDOOR_TOKEN` in the TrueNAS app (Edit), then delete the old token in Tandoor. With compose, update `.env` and run `docker compose up -d`.
- **Sign everyone out:** change `JWT_SIGNING_KEY` in the app. To also drop stored registrations, stop the app and empty the data dataset (with compose, `docker compose down -v`). Claude will ask you to reconnect.
- **Upgrade:** bump `fastmcp` in `gateway/requirements.txt` or the `tandoor-recipes-mcp` version in `gateway/Dockerfile`, push to `main`, then update the app in TrueNAS. With compose, run `docker compose up -d --build`.
- **Unit tests** run in the publish workflow. To run them locally against an image built with `docker compose build`:
  ```bash
  docker run --rm --user root -e PYTHONPATH=/src -v "$PWD/gateway:/src" -w /src tandoor-claude-gateway sh -c "pip install -q pytest && python -m pytest -q tests"
  ```

## Notes

- tandoor-recipes-mcp runs over **stdio** inside the gateway container. Its built-in HTTP mode (2.0.1) answers only the first request and returns 500 for every later one, because it reuses a stateless MCP SDK transport.
- The gateway issues its own short-lived tokens to Claude. It stores the Authentik refresh tokens encrypted in the data dataset (the `gateway-data` volume with compose) and checks them against Authentik on every request, so disabling a user in Authentik cuts off their access.
