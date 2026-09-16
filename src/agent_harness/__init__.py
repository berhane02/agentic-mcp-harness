import logging
import os
import sys


def main() -> None:
    from agent_harness.server import mcp

    # Log to stderr: stdout is reserved for the MCP stdio protocol.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")

    # stdio is the default for local MCP clients; containers set MCP_TRANSPORT=streamable-http.
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        logging.info("agent-harness MCP server started (stdio); waiting for a client...")
        mcp.run()
        return

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    logging.info("agent-harness MCP server listening on http://%s:%s/mcp (%s)", host, port, transport)
    mcp.run(transport=transport, host=host, port=port)
