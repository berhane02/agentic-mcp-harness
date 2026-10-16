# agent-harness

An MCP server exposing a few financial tools:

- `get_financial_news` — latest headlines from Google News' Business feed, or a topic search.
- `get_market_movers` — today's biggest gainers and losers among major US `tech` or `energy` stocks.
- `add` — trivial demo tool.

Both data sources are keyless public endpoints (Google News RSS, Yahoo Finance), so there is
nothing to configure before running.

## Transports

`main()` picks its transport from the environment:

| Variable        | Default     | Notes                                                    |
| --------------- | ----------- | -------------------------------------------------------- |
| `MCP_TRANSPORT` | `stdio`     | `streamable-http` for containers/remote clients           |
| `HOST`          | `127.0.0.1` | The image sets `0.0.0.0` so the port is reachable         |
| `PORT`          | `8000`      | HTTP endpoint is served at `/mcp`                         |

## Chat UI

A browser chat interface that lets Claude call the MCP tools for you. It runs the tool-use
loop server-side: the page streams tokens as they arrive and shows every tool call and its
result inline, so you can see which tool fired and what came back.

It needs an Anthropic API key:

```sh
export ANTHROPIC_API_KEY=sk-ant-...
```

Start the MCP server over HTTP, then the chat app:

```sh
MCP_TRANSPORT=streamable-http uv run agent-harness   # terminal 1
uv run agent-harness-chat                            # terminal 2
```

Open <http://localhost:8080>. Both come up together under `docker compose up --build`.

| Variable              | Default                      | Notes                              |
| --------------------- | ---------------------------- | ---------------------------------- |
| `ANTHROPIC_API_KEY`   | —                            | Required                           |
| `MCP_SERVER_URL`      | `http://127.0.0.1:8000/mcp`  | Which MCP server to drive          |
| `ANTHROPIC_MODEL`     | `claude-opus-5`              |                                    |
| `CHAT_HOST`/`CHAT_PORT` | `127.0.0.1` / `8080`       | Compose sets `0.0.0.0`             |

Conversation history is kept in memory, keyed by a per-tab id — fine for local use, but it
is not shared between workers and is lost on restart. "New chat" clears it.

## Run locally (stdio)

```sh
uv run agent-harness
```

Point a local MCP client at that command — this is the right mode for editor/CLI clients.

## Run with Docker Desktop

```sh
docker compose up --build
```

The server listens on `http://localhost:8000/mcp`. Connect an MCP client using the
streamable-HTTP transport, e.g.:

```sh
claude mcp add --transport http agent-harness http://localhost:8000/mcp
```

Plain Docker works too:

```sh
docker build -t agent-harness:local .
docker run --rm -p 8000:8000 agent-harness:local
```

## Deploying

The same image runs anywhere that can host a container (Cloud Run, ECS, Fly, a plain VM).
Most platforms inject the port to bind, which the image already honors via `PORT`.

Two things to know before exposing it publicly:

- **No authentication.** Anything that can reach `/mcp` can call the tools. Put it behind a
  proxy, gateway, or the platform's own auth. The MCP SDK also supports an
  `AuthSettings`/`TokenVerifier` path if you'd rather enforce tokens in-process.
- **DNS-rebinding protection is off when bound to `0.0.0.0`.** The SDK only auto-enables the
  Host/Origin allowlist for localhost binds. For a public deployment, pass explicit
  `TransportSecuritySettings` with your real hostname, or terminate at a proxy that does.

If you scale past one replica, either enable session affinity at the load balancer or switch
the server to `stateless_http=True` — MCP sessions are held in the process that created them.
