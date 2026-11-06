"""WebSocket endpoints: text chat and voice chat with an agent.

Both require a logged-in user: the web app's session cookie, an `Authorization: Bearer <token>`
or `Basic` header, or `?token=<token>` (see auth.py). Unauthenticated connections get an error
message and are closed with code 4401.

Both keep the conversation history for the life of the connection, so each
message is a follow-up to the previous ones (send {"type": "reset"} to clear it).

Text  ws://host/ws/agents/{name}/chat?trace=true
    client -> "plain text"  or  {"type": "message", "text": "..."}
              {"type": "reset"}  {"type": "ping"}
    server -> {"type": "ready", ...}
              {"type": "event", "event", "agent", "depth", "data", "ts"}   live trace (trace=true);
                  event is llm_response | tool_call | tool_result | error
              {"type": "response", "text", "stop_reason", "steps", "usage"}
              {"type": "error", "message"}  {"type": "reset_ok"}  {"type": "pong"}

Voice ws://host/ws/agents/{name}/voice?input_format=webm&output_format=mp3&voice=alloy&language=en&trace=false
    client -> binary frames            audio of one utterance (any chunking)
              {"type": "end"}          utterance finished -> transcribe, run agent, speak
              {"type": "text", "text"} skip speech-to-text, still get spoken reply
              {"type": "cancel"}       drop buffered audio
              {"type": "reset"}        drop audio + conversation history
    server -> {"type": "ready", ...}
              {"type": "transcript", "text"}
              {"type": "event", ...}   (trace=true)
              {"type": "response", "text", ...}
              {"type": "audio_start", "format"} <binary audio chunks> {"type": "audio_end", "bytes"}
              {"type": "error", "message"}
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .auth import UserStore, authenticate_connection
from .llm import Message
from .runtime import MultiAgentSystem, RunResult, TraceEvent
from .speech import INPUT_MIME, VOICE_STYLE, SpeechToText, TextToSpeech, speakable

log = logging.getLogger("multiagent.ws")

MAX_AUDIO_BYTES = 25 * 1024 * 1024  # OpenAI transcription upload limit
OUTPUT_FORMATS = {"mp3", "opus", "aac", "flac", "wav", "pcm"}
CLOSE_UNKNOWN_AGENT = 4404
CLOSE_BAD_PARAMS = 4400
CLOSE_UNAUTHORIZED = 4401


def _parse(text: str) -> dict[str, Any]:
    """JSON control message, or plain text treated as a chat message."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"type": "message", "text": text}
    if isinstance(data, dict):
        return data
    return {"type": "message", "text": text}


async def _error(ws: WebSocket, message: str) -> None:
    await ws.send_json({"type": "error", "message": message})


async def _run_turn(
    ws: WebSocket, system: MultiAgentSystem, agent: str, text: str,
    history: list[Message], stream_events: bool, system_suffix: str | None = None,
) -> RunResult | None:
    """Run the agent, forwarding trace events live. Returns None (and reports) on failure."""
    queue: asyncio.Queue[TraceEvent | None] = asyncio.Queue()
    on_event = queue.put_nowait if stream_events else None
    task = asyncio.create_task(system.run(agent, text, history=history, on_event=on_event,
                                                 system_suffix=system_suffix))
    task.add_done_callback(lambda _: queue.put_nowait(None))
    try:
        while (event := await queue.get()) is not None:
            data = event.model_dump(mode="json")
            data["event"] = data.pop("type")  # llm_response | tool_call | tool_result | error
            await ws.send_json({"type": "event", **data})
        result = await task
    except WebSocketDisconnect:
        raise
    except Exception as e:  # provider/API errors: report and keep the socket open
        log.exception("Agent run failed")
        await _error(ws, f"{type(e).__name__}: {e}")
        return None
    finally:
        if not task.done():  # client went away mid-run
            task.cancel()

    await ws.send_json({
        "type": "response", "text": result.output, "stop_reason": result.stop_reason,
        "steps": result.steps, "usage": result.usage.model_dump(),
    })
    return result


async def _accept(ws: WebSocket, system: MultiAgentSystem, users: UserStore, name: str) -> bool:
    await ws.accept()
    if authenticate_connection(users, ws, allow_query_token=True) is None:
        await _error(ws, "Not authenticated. Log in first.")
        await ws.close(code=CLOSE_UNAUTHORIZED)
        return False
    if name not in system.store:
        await _error(ws, f"Unknown agent '{name}'")
        await ws.close(code=CLOSE_UNKNOWN_AGENT)
        return False
    return True


def register_ws_routes(app: FastAPI, system: MultiAgentSystem,
                       stt: SpeechToText, tts: TextToSpeech, users: UserStore) -> None:

    @app.websocket("/ws/agents/{name}/chat")
    async def chat_ws(ws: WebSocket, name: str, trace: bool = False):
        if not await _accept(ws, system, users, name):
            return
        history: list[Message] = []
        await ws.send_json({"type": "ready", "agent": name, "mode": "text", "trace": trace})
        try:
            while True:
                msg = _parse(await ws.receive_text())
                kind = msg.get("type")
                if kind == "ping":
                    await ws.send_json({"type": "pong"})
                elif kind == "reset":
                    history = []
                    await ws.send_json({"type": "reset_ok"})
                elif kind == "message":
                    text = msg.get("text")
                    if not isinstance(text, str) or not text.strip():
                        await _error(ws, "'text' must be a non-empty string")
                        continue
                    result = await _run_turn(ws, system, name, text, history, trace)
                    if result is not None:
                        history = result.messages
                else:
                    await _error(ws, f"Unknown message type '{kind}'")
        except WebSocketDisconnect:
            pass

    @app.websocket("/ws/agents/{name}/voice")
    async def voice_ws(
        ws: WebSocket, name: str,
        input_format: str = "webm", output_format: str = "mp3",
        voice: str | None = None, language: str | None = None, trace: bool = False,
    ):
        if not await _accept(ws, system, users, name):
            return
        if input_format not in INPUT_MIME or output_format not in OUTPUT_FORMATS:
            await _error(ws, f"input_format must be one of {sorted(INPUT_MIME)}, "
                             f"output_format one of {sorted(OUTPUT_FORMATS)}")
            await ws.close(code=CLOSE_BAD_PARAMS)
            return

        history: list[Message] = []
        audio = bytearray()
        await ws.send_json({"type": "ready", "agent": name, "mode": "voice", "input_format": input_format,
                            "output_format": output_format, "trace": trace})

        async def reply(text: str) -> None:
            nonlocal history
            result = await _run_turn(ws, system, name, text, history, trace, VOICE_STYLE)
            if result is None:
                return
            history = result.messages
            spoken = speakable(result.output)
            if not spoken:
                return
            await ws.send_json({"type": "audio_start", "format": output_format})
            sent = 0
            try:
                async for chunk in tts.synthesize(spoken, voice=voice, fmt=output_format):
                    await ws.send_bytes(chunk)
                    sent += len(chunk)
            except WebSocketDisconnect:
                raise
            except Exception as e:
                log.exception("Text-to-speech failed")
                await _error(ws, f"Text-to-speech failed: {type(e).__name__}: {e}")
            await ws.send_json({"type": "audio_end", "bytes": sent})

        try:
            while True:
                frame = await ws.receive()
                if frame["type"] == "websocket.disconnect":
                    break
                if frame.get("bytes") is not None:
                    if len(audio) + len(frame["bytes"]) > MAX_AUDIO_BYTES:
                        audio.clear()
                        await _error(ws, f"Utterance larger than {MAX_AUDIO_BYTES} bytes; buffer cleared")
                    else:
                        audio.extend(frame["bytes"])
                    continue

                msg = _parse(frame.get("text") or "")
                kind = msg.get("type")
                if kind == "end":
                    if not audio:
                        await _error(ws, "No audio received before 'end'")
                        continue
                    data, audio = bytes(audio), bytearray()
                    try:
                        transcript = (await stt.transcribe(data, fmt=input_format, language=language)).strip()
                    except Exception as e:
                        log.exception("Speech-to-text failed")
                        await _error(ws, f"Speech-to-text failed: {type(e).__name__}: {e}")
                        continue
                    await ws.send_json({"type": "transcript", "text": transcript})
                    if not transcript:
                        await _error(ws, "No speech detected")
                        continue
                    await reply(transcript)
                elif kind in ("text", "message"):
                    text = msg.get("text")
                    if not isinstance(text, str) or not text.strip():
                        await _error(ws, "'text' must be a non-empty string")
                        continue
                    await reply(text)
                elif kind == "cancel":
                    audio.clear()
                    await ws.send_json({"type": "cancel_ok"})
                elif kind == "reset":
                    audio.clear()
                    history = []
                    await ws.send_json({"type": "reset_ok"})
                elif kind == "ping":
                    await ws.send_json({"type": "pong"})
                else:
                    await _error(ws, f"Unknown message type '{kind}'")
        except WebSocketDisconnect:
            pass
