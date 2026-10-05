"""Smoke test: start server.py over stdio, list tools, call rs_status and a dry-run pipeline."""
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).parent


async def main() -> None:
    params = StdioServerParameters(command=sys.executable, args=[str(HERE / "server.py")])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("TOOLS:", ", ".join(t.name for t in tools.tools))
            res = await s.call_tool("rs_status", {})
            print("STATUS:", res.content[0].text[:400])
            res = await s.call_tool("rs_drone_pipeline", {
                "images_folder": r"C:\DroneJobs\inbox\site01",
                "output_folder": r"C:\DroneJobs\output",
                "name": "site01", "dry_run": True})
            print("DRYRUN:", json.loads(res.content[0].text)["cmdline"])
            res = await s.call_tool("rs_inspect_images", {"folder": str(HERE)})
            print("INSPECT:", res.content[0].text[:300])


asyncio.run(main())
