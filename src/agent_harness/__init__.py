import logging
import sys


def main() -> None:
    from agent_harness.server import mcp

    # Log to stderr: stdout is reserved for the MCP stdio protocol.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    logging.info("agent-harness MCP server started (stdio); waiting for a client...")
    mcp.run()
