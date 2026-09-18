"""A small web chat UI that lets Claude drive the agent-harness MCP tools.

The browser talks to this app; this app runs the Claude tool-use loop and calls the
MCP server over streamable HTTP. Conversation state is kept in memory, keyed by a
conversation id the browser generates - fine for local/single-user use, not for a
shared deployment.
"""

import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anthropic
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)

MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://127.0.0.1:8000/mcp")
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")
MAX_TOKENS = int(os.environ.get("ANTHROPIC_MAX_TOKENS", "8000"))
INDEX_HTML = Path(__file__).parent / "static" / "index.html"

SYSTEM_PROMPT = """You are a financial research assistant with access to live market tools \
over MCP: news headlines and today's biggest movers among major US tech and energy stocks.

Use the tools whenever the user asks about markets, news, or specific stocks - the data is \
live, so never answer from memory. Quote concrete numbers you got back. Keep answers brief \
and conversational; a short list beats a long essay. If a tool fails, say so plainly."""

# conversation_id -> Anthropic messages list
CONVERSATIONS: dict[str, list[dict[str, Any]]] = {}


def _explain(exc: BaseException) -> str:
    """Describe a failure for the UI.

    anyio task groups (used by the MCP client) wrap errors in an ExceptionGroup, so the
    interesting exception is usually nested a couple of levels down.
    """
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    if isinstance(exc, anthropic.AuthenticationError):
        return "Claude rejected the credentials - check that ANTHROPIC_API_KEY is set and valid."
    if isinstance(exc, anthropic.RateLimitError):
        return "Claude is rate limiting this key. Wait a moment and try again."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Could not reach the Claude API. Check network connectivity."
    # The MCP client speaks httpx2; anything transport-level means the server is unreachable.
    if isinstance(exc, (httpx2.HTTPError, ConnectionError, OSError)):
        return f"Could not reach the MCP server at {MCP_SERVER_URL} - is it running?"
    return f"{type(exc).__name__}: {exc}"


def _tool_result_text(result: Any) -> str:
    """Flatten an MCP CallToolResult into text for a tool_result block."""
    if result.structured_content is not None:
        return json.dumps(result.structured_content)
    parts = [block.text for block in result.content if getattr(block, "type", "") == "text"]
    return "\n".join(parts) if parts else "(tool returned no content)"


async def _run_turn(conversation_id: str, user_message: str) -> AsyncIterator[str]:
    """Run one user turn to completion, yielding SSE frames as things happen."""

    def sse(event: str, **data: Any) -> str:
        return f"data: {json.dumps({'event': event, **data})}\n\n"

    messages = CONVERSATIONS.setdefault(conversation_id, [])
    messages.append({"role": "user", "content": user_message})

    client = anthropic.AsyncAnthropic()
    try:
        async with streamable_http_client(MCP_SERVER_URL) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                tools = [
                    {
                        "name": tool.name,
                        "description": tool.description or "",
                        "input_schema": tool.input_schema,
                    }
                    for tool in listed.tools
                ]
                yield sse("tools", names=[t["name"] for t in tools])

                # Tool-use loop: keep going until Claude answers without calling a tool.
                while True:
                    async with client.messages.stream(
                        model=MODEL,
                        max_tokens=MAX_TOKENS,
                        system=SYSTEM_PROMPT,
                        messages=messages,
                        tools=tools,
                        thinking={"type": "adaptive", "display": "summarized"},
                    ) as stream:
                        async for event in stream:
                            if event.type == "text":
                                yield sse("text", text=event.text)
                            elif event.type == "thinking" and getattr(event, "thinking", ""):
                                yield sse("thinking", text=event.thinking)
                        response = await stream.get_final_message()

                    messages.append({"role": "assistant", "content": response.content})

                    if response.stop_reason == "refusal":
                        detail = getattr(response.stop_details, "explanation", "") or ""
                        yield sse("error", message=f"The model declined this request. {detail}".strip())
                        return

                    if response.stop_reason != "tool_use":
                        break

                    # Execute every requested tool, then return all results in one user turn.
                    tool_results = []
                    for block in response.content:
                        if block.type != "tool_use":
                            continue
                        yield sse("tool_call", name=block.name, arguments=block.input)
                        try:
                            result = await session.call_tool(block.name, block.input or {})
                            text = _tool_result_text(result)
                            is_error = bool(result.is_error)
                        except Exception as exc:  # surface tool failures to Claude and the user
                            logger.exception("tool %s failed", block.name)
                            text, is_error = f"Tool call failed: {exc}", True
                        yield sse("tool_result", name=block.name, ok=not is_error, preview=text[:600])
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": text,
                                "is_error": is_error,
                            }
                        )
                    messages.append({"role": "user", "content": tool_results})

        yield sse("done")
    except Exception as exc:
        logger.exception("chat turn failed")
        # Don't leave a half-finished turn in history - the next request would 400.
        if messages and messages[-1]["role"] != "user":
            messages.clear()
        yield sse("error", message=_explain(exc))


async def index(request: Request) -> FileResponse:
    return FileResponse(INDEX_HTML)


async def chat(request: Request) -> StreamingResponse | JSONResponse:
    body = await request.json()
    message = (body.get("message") or "").strip()
    conversation_id = body.get("conversation_id") or "default"
    if not message:
        return JSONResponse({"error": "empty message"}, status_code=400)

    return StreamingResponse(
        _run_turn(conversation_id, message),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def reset(request: Request) -> JSONResponse:
    body = await request.json()
    CONVERSATIONS.pop(body.get("conversation_id") or "default", None)
    return JSONResponse({"ok": True})


app = Starlette(
    routes=[
        Route("/", index),
        Route("/api/chat", chat, methods=["POST"]),
        Route("/api/reset", reset, methods=["POST"]),
    ]
)


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.warning("ANTHROPIC_API_KEY is not set - the chat UI will load but every turn will fail.")

    host = os.environ.get("CHAT_HOST", "127.0.0.1")
    port = int(os.environ.get("CHAT_PORT", "8080"))
    logger.info("chat UI on http://%s:%s (MCP server: %s)", host, port, MCP_SERVER_URL)
    uvicorn.run(app, host=host, port=port, log_level="info")
