import asyncio
import json
import os
from pyats_mcp_server import run_show_command_async, apply_device_configuration_async

async def verify():
    print("--- 1. Testing SSHPass + Offline Parsing (Show Command) ---")
    res1 = await run_show_command_async("r1", "show ip interface brief")
    print(f"Status: {res1.get('status')}")
    print(f"Output Type: {type(res1.get('output'))}")
    if isinstance(res1.get('output'), dict):
        print("Success: Output is parsed JSON.")
    else:
        print("Fallback: Output is raw string.")

    print("\n--- 2. Testing SSHPass Configuration ---")
    res2 = await apply_device_configuration_async("r1", "interface Loopback88\n description Testing SSHPass Migration")
    print(f"Status: {res2.get('status')}")
    print(f"Message: {res2.get('message')}")

    print("\n--- 3. Verifying Config Change ---")
    res3 = await run_show_command_async("r1", "show ip interface brief")
    print(f"Loopback88 Present? {'Loopback88' in str(res3.get('output'))}")

if __name__ == "__main__":
    asyncio.run(verify())
