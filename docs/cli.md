# CLI

The `gradus` command is a REPL: it reads a line, runs one turn, prints the result, and repeats until `exit`, `quit`, or EOF (`Ctrl-D`).

## Connection

A key alone cannot identify a provider, so the service, model, and key are configured together. Precedence, highest first:

1. CLI flags (`--service` / `--provider` / `--model` / `--base-url`)
2. `<workspace>/.env`
3. `~/.gradus/config.json`

A three-line `.env` is enough:

```ini
GRADUS_SERVICE=kimi-compat
GRADUS_MODEL=kimi-k2.7-code
GRADUS_API_KEY=sk-...
```

## Flags

| Flag | Purpose |
| --- | --- |
| `--workspace` | Root for tools and commands (default: current directory) |
| `--sandbox` | `auto` / `local` / `docker` / `appcontainer` / `restricted` |
| `--service` / `--provider` / `--model` / `--base-url` | Override the connection |
| `--resume` / `--session-dir` | Resume a JSONL session, or choose where new ones go |
| `--mcp` / `--config` | Explicit MCP and config file paths |
| `--untrust [path]` | Revoke trust for a project's MCP config, then exit |
| `--reset-sandbox` | Clear orphan copies, AppContainer grants, and the profile |
| `--log` | Append tool and error events to a JSONL file |

## Inside the REPL

| Input | Effect |
| --- | --- |
| `/reload` | Rebuild tools and extensions; history and MCP connections are kept |
| `/command` | Expand a saved slash command |
| `/skill:name` | Expand a saved skill prompt |
| `exit` / `quit` / `Ctrl-D` | Leave |
| `Ctrl-C` | Abort the running turn, stay in the REPL |

## Session files

Each session is appended to `<workspace>/.gradus/sessions/<timestamp>-<id>.jsonl` (gitignored). `--resume` continues one; `--session-dir` puts new ones elsewhere.
