# Web UI

The browser client is its own bundle under `web/`, served by the Python host in `gradus/host`. Two processes: the host and the bundle.

## Running it

Start the host first:

```bash
uv sync --extra web
uv run gradus-serve --port 8765 --token dev
```

It prints the address to open, token included:

```
Gradus web · kimi-k2.7-code · H:\your\workspace
open: http://127.0.0.1:8765/?token=dev
```

Then pick one:

```bash
cd web && npm install && npm run build   # host serves web/dist — open the printed address
cd web && npm run dev                    # Vite on :5173, proxies /ws to 127.0.0.1:8765
```

With the dev server, open `http://localhost:5173/?token=dev`: the frontend reads `token` from the page URL, so it has to match `--token`. Adding `&mock=1` loads the UI against a scripted connection with no host at all.

Restart the host after editing anything under `gradus/`; the frontend reloads on save by itself.

## Candidate models

`~/.gradus/config.json` (or whatever `--config` points at) may carry a candidate list; the web model picker reads it directly:

```json
{
  "models": [
    { "name": "Kimi · Chat", "service": "kimi-compat", "model": "kimi-k2.7-code" },
    { "name": "Kimi · Messages", "service": "kimi-anthropic", "model": "kimi-k2.7-code" },
    { "name": "Kimi · Responses", "service": "kimi-responses", "model": "kimi-k2.7-code" }
  ]
}
```

Each entry accepts the same connection fields as the top level (`service` / `provider` / `model` / `base_url` / `api_key` / `context_window` / `max_output_tokens`); `name` is what the UI shows, and an omitted `api_key` reuses the current one. The list is read from the file only — environment variables still describe just the current model.

You do not have to hand-edit the JSON: the gear icon in the header opens a settings panel that adds, edits, and removes candidate models for you (it never echoes keys back). The last model you pick is written to the top-level `"active"` field and restored on the next start.

## Appearance

The same panel's "Appearance" section switches between light / dark / follow-system. The choice lives in browser `localStorage`, not in the config file, so it is per-browser rather than per-workspace.

## Switching providers mid-session

Switching does not start a new session. History is the provider-neutral representation, translated per request by whichever provider is active, so one session can move between `openai_compat`, `anthropic`, and `openai_responses`.

Vendor-private fields such as a thinking block's `signature` are not converted; they may be dropped or passed through unchanged.

## The token

`/ws` closes with `4401` unless the request carries the token the host started with. The frontend reads it from the page URL (`?token=…`), so the page URL and `--token` have to agree. Omit `--token` and the host generates one and prints the matching URL.

## Mock mode

Appending `&mock=1` swaps the WebSocket connection for a scripted one (`web/src/plugins/mock-connection.ts`), so the UI runs with no host and no API key. This is what `docs/web-ui.gif` was recorded with.
