"""
Minimal MCP client connectivity check — run this BEFORE the full agent
loop is written. Confirms two things that are NOT yet verified against
live data:
  1. That mcp.Client(url) can connect to our running streamable-http
     server at all (v2's single-object Client class, replacing v1's
     three-layer transport+ClientSession+initialize()).
  2. The exact shape of client.list_tools() on this new Client class —
     every v2 example found so far shows call_tool(), none show
     list_tools(), so whether it returns .tools[i].name/.input_schema
     the same way v1's ClientSession did is an assumption, not a fact.

Requires the server running separately first:
    cd ../server && python server.py
(this uses the streamable-http transport on port 8000, NOT `mcp dev`,
which spawns its own stdio subprocess and is a different code path)
"""

import asyncio

from mcp import Client

SERVER_URL = "http://localhost:8000/mcp"


async def main() -> None:
    async with Client(SERVER_URL) as client:
        print("Connected. Server info:", client.server_info)

        tools_result = await client.list_tools()
        print(f"\nlist_tools() returned type: {type(tools_result)}")
        print(f"Raw repr (first 500 chars): {repr(tools_result)[:500]}")

        # Best-effort attempt at the v1-style shape — if this fails,
        # the printed repr above tells us the actual shape to code against.
        try:
            for t in tools_result.tools:
                print(f"\n- {t.name}")
                print(f"  description: {t.description[:100]}...")
                print(f"  input_schema keys: {list(t.input_schema.get('properties', {}).keys())}")
        except AttributeError as e:
            print(f"\n[EXPECTED-POSSIBLE FAILURE] .tools access didn't work: {e}")
            print("This confirms the shape assumption was wrong — use the raw repr above to fix the real agent code.")


if __name__ == "__main__":
    asyncio.run(main())
