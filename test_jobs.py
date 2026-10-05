"""Job-runner test with a stub RealityScan.exe (a .cmd that echoes its arguments)."""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).parent
TMP = Path(tempfile.mkdtemp(prefix="rsmcp_"))
STUB = TMP / "RealityScan.cmd"
STUB.write_text("@echo off\r\necho STUB RealityScan args: %*\r\nping -n 3 127.0.0.1 >nul\r\necho done\r\nexit /b 0\r\n")


async def main() -> None:
    env = dict(os.environ, REALITYSCAN_EXE=str(STUB), RS_JOBS_DIR=str(TMP / "jobs"))
    params = StdioServerParameters(command=sys.executable, args=[str(HERE / "server.py")], env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = json.loads((await s.call_tool("rs_run", {"commands": ["-newScene", "-addFolder C:\\x y", "-align", "-quit"], "name": "stub"})).content[0].text)
            print("STARTED:", res["job_id"], res["state"])
            for _ in range(20):
                await asyncio.sleep(1)
                st = json.loads((await s.call_tool("rs_job_status", {"job_id": res["job_id"]})).content[0].text)
                if st["state"] != "running":
                    break
            print("FINAL:", st["state"], "exit", st["exit_code"])
            print("LOG:", st["log_tail"])
            lst = json.loads((await s.call_tool("rs_job_list", {})).content[0].text)["jobs"]
            print("LIST:", [(j["job_id"], j["state"]) for j in lst])
            assert st["state"] == "done" and "-addFolder" in st["log_tail"] and '"C:\\x y"' in st["log_tail"]
            print("OK")


asyncio.run(main())
