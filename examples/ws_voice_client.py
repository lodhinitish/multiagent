"""Send a recorded audio file to an agent over the voice WebSocket and save the spoken reply.

    python examples/ws_voice_client.py lead question.wav            # writes reply.mp3
    python examples/ws_voice_client.py lead question.m4a -o answer.mp3 --voice nova

The input format is taken from the file extension (wav, mp3, m4a, mp4, webm, ogg, flac).
"""
import argparse
import getpass
import asyncio
import json
from pathlib import Path

import websockets

CHUNK = 32 * 1024  # stream the upload in pieces, like a microphone would



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

async def main(agent: str, audio_path: Path, out: Path, url: str, voice: str | None,
               headers: dict[str, str]) -> None:
    fmt = audio_path.suffix.lstrip(".").lower()
    out_fmt = out.suffix.lstrip(".").lower() or "mp3"
    query = f"input_format={fmt}&output_format={out_fmt}" + (f"&voice={voice}" if voice else "")
    async with websockets.connect(f"{url}/ws/agents/{agent}/voice?{query}", max_size=None,
                                  additional_headers=headers) as ws:
        print(json.loads(await ws.recv()))
        data = audio_path.read_bytes()
        for i in range(0, len(data), CHUNK):
            await ws.send(data[i:i + CHUNK])
        await ws.send(json.dumps({"type": "end"}))

        audio = bytearray()
        while True:
            msg = await ws.recv()
            if isinstance(msg, bytes):
                audio.extend(msg)
                continue
            msg = json.loads(msg)
            if msg["type"] == "transcript":
                print(f"you said> {msg['text']}")
            elif msg["type"] == "response":
                print(f"{agent}> {msg['text']}")
            elif msg["type"] == "audio_end":
                out.write_bytes(audio)
                print(f"saved {len(audio)} bytes of speech to {out}")
                return
            elif msg["type"] == "error":
                print("error:", msg["message"])
                if not msg["message"].startswith("Text-to-speech"):  # audio_end still follows a TTS error
                    return


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("agent")
    p.add_argument("audio", type=Path)
    p.add_argument("-o", "--out", type=Path, default=Path("reply.mp3"))
    p.add_argument("--url", default="ws://localhost:8000")
    p.add_argument("--user", help="login (default ADMIN_USERNAME from .env)")
    p.add_argument("--password", help="default ADMIN_PASSWORD from .env, else asks")
    p.add_argument("--voice", help="e.g. alloy, nova, shimmer, echo")
    a = p.parse_args()
    asyncio.run(main(a.agent, a.audio, a.out, a.url, a.voice, auth_header(a.user, a.password)))
