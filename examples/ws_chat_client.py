"""Interactive text chat with an agent over WebSocket.

    python examples/ws_chat_client.py lead
    python examples/ws_chat_client.py lead --trace --url ws://localhost:8000

Type a message and press Enter. /reset clears the conversation, /quit exits.
"""
import argparse
import getpass
import asyncio
import json

import websockets



def auth_header(user: str | None, password: str | None) -> dict[str, str]:
    """Basic auth from --user/--password, or ADMIN_USERNAME/ADMIN_PASSWORD in the environment / .env."""
    import base64
    import os
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    user = user or os.getenv("ADMIN_USERNAME", "admin")
    password = password or os.getenv("ADMIN_PASSWORD") or getpass.getpass(f"Password for {user}: ")
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}

async def main(agent: str, url: str, trace: bool, headers: dict[str, str]) -> None:
    async with websockets.connect(f"{url}/ws/agents/{agent}/chat?trace={str(trace).lower()}",
                                  additional_headers=headers) as ws:
        print(json.loads(await ws.recv()))
        while True:
            text = (await asyncio.to_thread(input, "you> ")).strip()
            if not text:
                continue
            if text == "/quit":
                return
            await ws.send(json.dumps({"type": "reset"} if text == "/reset" else {"type": "message", "text": text}))
            while True:
                msg = json.loads(await ws.recv())
                if msg["type"] == "event":
                    d = msg["data"]
                    detail = d.get("name") or (d.get("text") or "")[:80]
                    print(f"  {'  ' * msg['depth']}[{msg['agent']}] {msg['event']} {detail}")
                    continue
                if msg["type"] == "response":
                    print(f"{agent}> {msg['text']}\n  ({msg['steps']} steps, {msg['usage']})")
                else:
                    print(msg)
                break


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("agent")
    p.add_argument("--url", default="ws://localhost:8000")
    p.add_argument("--user", help="login (default ADMIN_USERNAME from .env)")
    p.add_argument("--password", help="default ADMIN_PASSWORD from .env, else asks")
    p.add_argument("--trace", action="store_true", help="show tool calls and sub-agent steps live")
    a = p.parse_args()
    asyncio.run(main(a.agent, a.url, a.trace, auth_header(a.user, a.password)))
