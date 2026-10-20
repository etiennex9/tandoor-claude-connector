# Tandoor ↔ Claude connector

Lets Claude (web, desktop and the phone apps) read and update your Tandoor Recipes instance: recipes, foods on hand, the shopping list and the meal plan.

```
Claude app ─▶ Anthropic cloud ─HTTPS─▶ nginx  recipe.example.com
                                         ├─ /claude/*, /.well-known/oauth-*  ─▶ gateway container (TrueNAS, <NAS>:30181)
                                         └─ everything else                   ─▶ Tandoor (unchanged)

gateway container
 ├─ FastMCP gateway (Python): OAuth for Claude, sign-in delegated to Authentik, recipe photo uploads
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
The `/claude/` block also raises nginx's request size limit to 11 MB, for [recipe photos](#recipes-from-photos). If you copied it before, add its `client_max_body_size` line.

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
- Share a photo, screenshot or PDF of a recipe: "Add this to Tandoor, with the photo as its image." Or start from the connector's `import_recipe_from_photo` prompt in the **+** menu.
- "Import https://… and plan it for Friday dinner."
- "Add my lentil soup recipe: …"
- "Make the lasagna serve 6 and add basil to the sauce step."

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

```bash
curl -s https://recipe.example.com/claude/upload/test
```
Should return the gateway's "This link has expired or was already used" page, not Tandoor's.

Tandoor itself (`https://recipe.example.com/`) should behave exactly as before.

## What Claude can do

The tool list is built into the image: `DEFAULT_INCLUDE_ONLY` in [gateway/server.py](gateway/server.py). `DEFAULT_EXCLUDE` adds a second layer of protection. To override either one, set `TANDOOR_MCP_INCLUDE_ONLY` or `TANDOOR_MCP_EXCLUDE` (comma-separated, `*` globs) in the TrueNAS app's environment, or in `.env` with compose. A blank value means the default, never "no filter".

| Area | Tools |
|---|---|
| Read | search/list/get recipes, related recipes, recipe steps, foods, ingredients, keywords, nutrition property types, meal plans and types, shopping entries, cook log, units, version |
| Pantry | `update_food`, `food_batch_update`, `create_food`, `food_shopping_update` (set or clear "on hand") |
| Shopping list | create/update/delete entries, bulk check, add a recipe's ingredients |
| Meal plan | create/update/delete entries |
| Recipes | `create_recipe`, `update_recipe`, `recipe_batch_update`, `upload_recipe_image` (from a URL), `import_recipe_from_url`, `create_cook_log` |
| Steps and ingredients | `update_step`, `delete_step`, `update_ingredient`, `delete_ingredient` |
| Recipe photos | `create_image_upload_link` (gateway-side): a one-time link for setting a recipe's image; the `import_recipe_from_photo` prompt |

Not exposed:
- Deleting or merging recipes or foods.
- `create_step` and `create_ingredient`: they can't attach to a recipe. Claude adds steps through `update_recipe` and ingredients through `update_step`.
- Admin, API-token, invite, user, space, storage, AI-provider and import/export tools, including Tandoor's AI import.

Editing replaces whole lists: `update_recipe` with steps replaces all of the recipe's steps, and `update_step` with ingredients replaces all of that step's ingredients. To change one amount or unit, Claude uses `update_ingredient`.

`upload_recipe_image` takes an image URL. The gateway sets `TANDOOR_MCP_UPLOAD_ROOT` to empty, so it reads no files inside the container.

To change the default tool list, edit it, push to `main`, then update the app in TrueNAS (see [Deploy](#deploy)). With compose, run `docker compose up -d --build`.

Tandoor stores "on hand" as a **yes/no flag per food**, with no quantities or fridge/pantry locations.

### Recipes from photos

Share a photo, screenshot or PDF of a recipe and ask Claude to add it. Claude reads it itself and creates the recipe with `create_recipe`, which takes foods, units and keywords by name and finds or creates them in Tandoor. Tandoor's AI import and AI provider aren't involved. The `import_recipe_from_photo` prompt tells Claude how to transcribe: keep amounts and units as written, put each ingredient in the step that uses it, and ask about anything illegible instead of guessing. `create_recipe` has no ingredient headers, so sections such as "For the sauce" become steps of their own, and an ingredient without an amount ("salt to taste") gets 0, with the rest in its note.

Setting the photo as the recipe's image takes one more tap. A tool call can't carry a photo from the chat, and `upload_recipe_image` only takes a public URL, so the gateway adds its own tool, `create_image_upload_link`. Claude gets a link such as `https://recipe.example.com/claude/upload/<token>` and gives it to you. Open it on the device that has the photo and choose the photo; the gateway checks it and sets it as the recipe's image, replacing any image the recipe had.
- Each link is for one recipe and one upload, and expires after 30 minutes. The gateway keeps links in memory, so a restart expires them too.
- It takes a JPEG, PNG or WebP of up to 10 MB, checked by its content. iPhones convert HEIC photos to JPEG when you choose them.
- The link itself is the credential: opening it needs no sign-in, so share it only with whoever should set the image.
- If Claude can run code that reaches your host (code execution with network access in Claude's settings), it can upload the image itself, such as a crop of the dish. That depends on your Claude plan and settings, and hasn't been tried here.

The tool and prompt are served by the gateway, not tandoor-recipes-mcp, so `TANDOOR_MCP_INCLUDE_ONLY` doesn't hide them.

## Deploy

On every push to `main` that changes more than Markdown files, [the publish workflow](.github/workflows/publish.yml) runs the unit tests. Then, if the image's inputs changed since the workflow's last successful push run, it builds the image and pushes it to the GitHub Container Registry as `ghcr.io/etiennex9/tandoor-claude-gateway`, tagged `latest` and `sha-<commit>`. The inputs are read from [gateway/Dockerfile](gateway/Dockerfile): the Dockerfile and what it copies ([image-changed.sh](.github/scripts/image-changed.sh)). So a push that only changes tests, docs, nginx or deploy files doesn't publish a new image. A manual run (**Actions → Publish → Run workflow**), or a change to the workflow or its scripts in `.github/scripts/`, always builds. On a branch other than `main`, a manual run pushes only `sha-<commit>`. Before any push, [smoke-test.sh](.github/scripts/smoke-test.sh) starts the image against a mock Authentik, as `568:568` (TrueNAS) and as `node` with a new volume (compose), where it must answer `/healthz`, and as `568:568` with a root-owned `/data`, where it must stop with the entrypoint's message; if any of these fails, nothing is pushed. Pull requests run the tests, and build and smoke-test the image without pushing it when its inputs changed. If a run fails or is canceled, the next run picks up its changes. A Markdown-only push doesn't start a run, though, so use a manual run if you don't have another push coming.

To roll out a new image, open **Apps** in TrueNAS and update the `tandoor-claude` app. It runs `latest`. TrueNAS offers the update once a day after a publish.

To roll out each publish automatically instead, within about 10 minutes, use the cron job from Essence's [truenas-autodeploy.py](https://github.com/etiennex9/essence/blob/main/deploy/truenas-autodeploy.py) (Essence's README → Deploy → *Rolling out each push automatically* has the steps; written for TrueNAS 25.10). Every 5 minutes it compares the digest `latest` points to on GHCR with the image the app runs; when they differ, it waits 2 minutes for the publish to settle, snapshots the data dataset, pulls and redeploys the app (what **Update** does), waits until the gateway's healthcheck passes, and checks that it doesn't restart in the 2 minutes after. It mails only failures, or every rollout too, depending on the cron job's **Hide Standard Output**. Nothing reaches the NAS from outside. For this app, check the setup with `--check` first, then use this as the cron job's command:

```sh
python3 /mnt/POOL/scripts/truenas-autodeploy.py tandoor-claude --snapshot POOL/apps/tandoor-claude/data
```

A failed rollout isn't retried until the next publish. If the pull failed, `rm /mnt/POOL/scripts/.autodeploy-tandoor-claude.json` retries it; if the gateway failed after the redeploy, it already has the new image: fix the cause, then **Stop** and **Start** the app, or push a fix. To hold the gateway at a version, set its image to a `:sha-<commit>` tag listed on the package page in the app's YAML; the script leaves sha tags alone.

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
