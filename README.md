# DeerFlow

Minimal local deployment of [ByteDance DeerFlow](https://github.com/bytedance/deer-flow)
`v2.2.0` that uses only self-hosted services, from the published images (no
upstream clone or build).

The gateway connects over the external `ai-backend` Docker network to:

- LiteLLM (or another OpenAI-compatible endpoint): `http://litellm:4000/v1`,
  chat model `swol-doge` (change it under `models:` in `config.yaml`).
- A search gateway (SearXNG search with Crawl4AI page reading) at
  `http://search-gateway:8080`, with `POST /v2/search` and `POST /v2/fetch`.
  It backs DeerFlow's `web_search` and `web_fetch` tools via
  `custom_tools/search_gateway.py`.

Nothing proprietary is required: no hosted model, search API or tracing.

## Files

| Path | Purpose |
| --- | --- |
| `docker-compose.yml` | gateway, frontend, nginx (port `127.0.0.1:2026`) |
| `config.yaml` | DeerFlow config (schema 63): model, tools, sandbox |
| `extensions_config.json` | MCP servers / skill toggles (written by the UI) |
| `custom_tools/search_gateway.py` | `web_search` / `web_fetch` tools |
| `skills/public/` | upstream v2.2.0 skills (not in the image) |
| `skills/custom/` | your own skills (git-ignored) |
| `data/` | state: SQLite, threads, memory (git-ignored) |
| `workspace/`, `reference/` | agent-visible files (rw / ro) |

## Start

```bash
cp .env.example .env    # then set the values below
docker compose pull
docker compose up -d
```

`.env`:

- `SEARCH_GATEWAY_API_KEY`: the search gateway's `GATEWAY_API_KEY`
  (leave empty if the gateway runs without one).
- `LITELLM_API_KEY`: LiteLLM key (any non-empty value without auth).
- `DEERFLOW_API_KEY`: service key for programs that call DeerFlow's API
  (generate one with `openssl rand -hex 32`). Compose passes it to DeerFlow
  as `DEER_FLOW_INTERNAL_AUTH_TOKEN`. Each client is configured with the
  same value, and on every request to `http://deer-flow-nginx:2026/api/...`
  sends:
  - `X-DeerFlow-Internal-Token: <key>`;
  - `X-DeerFlow-Owner-User-Id: <name>`, the user space to act in. Threads
    there are not listed under the web UI's login accounts;
  - on POST, PUT, PATCH and DELETE, an `X-CSRF-Token` header equal to a
    `csrf_token` cookie (any random value). Without the pair DeerFlow
    answers 403; with a wrong key, 401.

  Empty: DeerFlow uses a random key at each start, so no client can call
  the API.
- `BETTER_AUTH_SECRET`: long random string.
- `DEER_FLOW_PORT`: default `2026`.
- `COMPOSE_FILE=docker-compose.yml:docker-compose.ext-shared.yml`
  (optional): also attach nginx to the external `ext-shared` network, so
  containers there reach `http://deer-flow-nginx:2026`.

Open <http://127.0.0.1:2026>. On the first visit, DeerFlow prompts you to
create the local admin account.

## Model and thinking

`swol-doge` is used through `langchain_openai:ChatOpenAI`. DeerFlow's
*flash* mode sends `chat_template_kwargs.enable_thinking: false`; the
thinking modes send `true`. The model's reasoning text is not shown in the
UI (DeerFlow only displays it for vLLM/DeepSeek-style clients). To use
another model, add an entry under `models:` in `config.yaml`.

## Web tools

- `web_search(query, time_range=None)`: `POST /v2/search` with
  `fetch_top: 0`; returns `[{title, url, snippet}]`. If nothing is found
  it names the search engines that did not respond.
- `web_fetch(url, query=None, offset=0)`: `POST /v2/fetch`; returns clean
  Markdown for pages and PDFs, paged by `offset`, or only the passages
  matching `query`.

Gateway errors come back to the model as `Error: ...` text. Limits
(`max_results`, `max_chars`, `timeout`) are set per tool in `config.yaml`.

## Upgrading from v2.1.0

- Back up `./data` first: v2.2.0 migrates the database on start
  (alembic), and that cannot be undone, so going back to v2.1.0 needs
  the backup.
- `docker compose pull && docker compose up -d`. The API that services
  call (threads, uploads, runs, streams, artifacts, the service key) is
  unchanged.
- `config.yaml` keeps every setting; only `config_version` moves to 63.
  New sections (e.g. `pii_redaction`, `knowledge_base`) stay at their
  defaults (off).
- The gateway now stops gracefully (`init`, `exec`, 90 s stop grace) so
  DeerFlow's memory queue is flushed on restart, and it trusts nginx's
  `X-Real-IP` for the login lockout (`AUTH_TRUSTED_PROXIES`).
- Features that store credentials (IM channels, MCP OAuth) encrypt
  them with `DEER_FLOW_CREDENTIALS_KEY`, or a key generated once in
  `data/.credentials_key`; neither feature is on here. If you enable
  one, keep that key with the backups.
- Skills are refreshed to v2.2.0; `vercel-deploy-claimable` is now
  `vercel-deploy`.

## Upgrading from the v2.0.0 setup

- Back up `./data` first; v2.1.0 may migrate stored state on start.
- The config is now the real file `config.yaml` (it was inlined in
  Compose), with `extensions_config.json` next to it.
- SearXNG and Crawl4AI are no longer called directly; only
  `search-gateway` must be on `ai-backend`.
- Skills are vendored in `skills/public`; put your own in `skills/custom`.

## Remote access

The frontend must be served at a site root, not a sub-path. Any HTTPS
reverse proxy that serves `http://127.0.0.1:2026` at `/` and preserves the
original HTTPS scheme works; DeerFlow's secure session and CSRF cookies need
that scheme. With Tailscale Serve, for example:

```bash
tailscale serve --bg --https=443 http://127.0.0.1:2026
```

## Verify

```bash
docker compose ps        # gateway "healthy"
curl -fsS http://127.0.0.1:2026/health
docker compose exec nginx nginx -t
docker compose exec gateway python -c \
  'import urllib.request; print(urllib.request.urlopen("http://deer-flow-nginx:4000/v1/models").status)'
docker compose exec gateway python -c \
  'import urllib.request; print(urllib.request.urlopen("http://deer-flow-nginx:8080/health").status)'
# Must fail (no route out):
docker compose exec gateway python -c \
  'import urllib.request; urllib.request.urlopen("https://example.com", timeout=5)'
docker compose logs gateway | grep -i -E 'error|web_search'
```

Then ask in the UI: "Search the web for the latest DeerFlow release and
summarise its release notes." It should call `web_search` then
`web_fetch`.

Tool tests (stub gateway, needs `httpx`):

```bash
python -m unittest discover -s tests
```

## Design

No egress: `gateway` and `frontend` are only on the internal `deer-flow`
network. `nginx` is the one container also on `ai-backend` (and on
`ext-shared` when started with `docker-compose.ext-shared.yml`); it
publishes the UI and forwards exactly two services for the gateway:
LiteLLM (`deer-flow-nginx:4000`) and the search gateway
(`deer-flow-nginx:8080`). Other containers reach the API at
`http://deer-flow-nginx:2026`. Memory token counting is `char`, so
tiktoken never downloads its encoding. Tracing stays off (no
LangSmith/Langfuse/Monocle variables). The UI still has a few links to
outside sites, and the About page embeds a star-history image that the
browser loads; the Lark status route asks GitHub for the latest
lark-cli release, which fails harmlessly here.

One gateway worker, the in-memory stream bridge, SQLite and DeerFlow's
local sandbox inside the gateway container: no Redis, PostgreSQL,
Kubernetes, sandbox service or Docker socket. Bash and file tools see only
the paths mounted into the gateway (`/workspace`, `/reference`).
