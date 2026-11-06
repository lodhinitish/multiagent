from __future__ import annotations

from fastapi.testclient import TestClient

from multiagent import ToolCall
from multiagent.api import create_app
from multiagent.speech import VOICE_STYLE, SpeechToText, TextToSpeech, speakable

from test_system import authed_client, cfg, make_system, resp


class FakeSTT(SpeechToText):
    def __init__(self):
        self.received: list[tuple[bytes, str]] = []

    async def transcribe(self, audio, *, fmt="webm", language=None):
        self.received.append((audio, fmt))
        return audio.decode()  # the "audio" in tests is just utf-8 text


class FakeTTS(TextToSpeech):
    async def synthesize(self, text, *, voice=None, fmt="mp3"):
        data = f"{voice}:{fmt}:{text}".encode()
        yield data[:4]
        yield data[4:]


def client(tmp_path, scripts, stt=None):
    system, provider = make_system(tmp_path, scripts)
    system.add_agent("bot", cfg("bot", tools=["add"]))
    return authed_client(system, stt=stt or FakeSTT(), tts=FakeTTS()), provider


def test_text_ws_streams_events_and_keeps_history(tmp_path):
    c, provider = client(tmp_path, {"bot": [
        resp(calls=[ToolCall(id="1", name="add", arguments={"a": 2, "b": 3})]), resp("5"),
        resp("10"),
    ]})
    with c.websocket_connect("/ws/agents/bot/chat?trace=true") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "message", "text": "2+3?"})
        events = []
        while (m := ws.receive_json())["type"] == "event":
            events.append((m["event"], m["agent"], m["data"].get("name")))
        assert events == [("llm_response", "bot", None), ("tool_call", "bot", "add"),
                          ("tool_result", "bot", "add"), ("llm_response", "bot", None)]
        assert m == {"type": "response", "text": "5", "stop_reason": "completed", "steps": 2,
                     "usage": {"input_tokens": 2, "output_tokens": 2}}

        ws.send_text("double it")  # plain text works too
        while (m := ws.receive_json())["type"] == "event":
            pass
        assert m["text"] == "10"
    # second turn saw the first turn's conversation
    second = provider.calls[-1]["messages"]
    assert [m.role for m in second] == ["user", "assistant", "tool", "assistant", "user"]


def test_text_ws_reset_errors_and_unknown_agent(tmp_path):
    c, provider = client(tmp_path, {"bot": [resp("a"), resp("b")]})
    with c.websocket_connect("/ws/agents/bot/chat") as ws:
        ws.receive_json()
        ws.send_json({"type": "message", "text": "one"})
        assert ws.receive_json()["text"] == "a"
        ws.send_json({"type": "reset"})
        assert ws.receive_json()["type"] == "reset_ok"
        ws.send_json({"type": "message", "text": " "})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "bogus"})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "message", "text": "two"})
        assert ws.receive_json()["text"] == "b"
    assert len(provider.calls[-1]["messages"]) == 1  # history was cleared

    with c.websocket_connect("/ws/agents/ghost/chat") as ws:
        assert "Unknown agent" in ws.receive_json()["message"]


def test_text_ws_reports_provider_failure_and_stays_open(tmp_path):
    c, _ = client(tmp_path, {"bot": [resp("ok")]})
    with c.websocket_connect("/ws/agents/bot/chat") as ws:
        ws.receive_json()
        ws.send_json({"type": "message", "text": "x"})
        assert ws.receive_json()["text"] == "ok"
        ws.send_json({"type": "message", "text": "y"})  # script exhausted -> IndexError
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_voice_ws_audio_round_trip(tmp_path):
    stt = FakeSTT()
    c, provider = client(tmp_path, {"bot": [resp("**hello** there"), resp("typed reply")]}, stt=stt)
    with c.websocket_connect("/ws/agents/bot/voice?input_format=wav&output_format=opus&voice=nova") as ws:
        assert ws.receive_json()["output_format"] == "opus"
        ws.send_bytes(b"hi ")
        ws.send_bytes(b"bot")
        ws.send_json({"type": "end"})
        assert ws.receive_json() == {"type": "transcript", "text": "hi bot"}
        assert ws.receive_json()["text"] == "**hello** there"  # transcript shows the raw reply
        assert ws.receive_json() == {"type": "audio_start", "format": "opus"}
        audio = ws.receive_bytes() + ws.receive_bytes()
        assert audio == b"nova:opus:hello there"
        assert ws.receive_json() == {"type": "audio_end", "bytes": len(audio)}

        ws.send_json({"type": "text", "text": "typed"})  # text in, speech out
        assert ws.receive_json()["text"] == "typed reply"
        assert ws.receive_json()["type"] == "audio_start"
    assert stt.received == [(b"hi bot", "wav")]
    assert all(VOICE_STYLE in call["system"] for call in provider.calls)  # voice-mode instructions added


def test_voice_ws_validation(tmp_path):
    c, _ = client(tmp_path, {"bot": []})
    with c.websocket_connect("/ws/agents/bot/voice") as ws:
        ws.receive_json()
        ws.send_json({"type": "end"})
        assert "No audio" in ws.receive_json()["message"]
        ws.send_bytes(b"   ")
        ws.send_json({"type": "end"})
        assert ws.receive_json() == {"type": "transcript", "text": ""}
        assert ws.receive_json()["message"] == "No speech detected"
        ws.send_bytes(b"abc")
        ws.send_json({"type": "cancel"})
        assert ws.receive_json()["type"] == "cancel_ok"
    with c.websocket_connect("/ws/agents/bot/voice?output_format=ogg") as ws:
        assert "output_format" in ws.receive_json()["message"]


def test_text_chat_has_no_voice_instructions(tmp_path):
    c, provider = client(tmp_path, {"bot": [resp("ok")]})
    with c.websocket_connect("/ws/agents/bot/chat") as ws:
        ws.receive_json()
        ws.send_text("hi")
        ws.receive_json()
    assert provider.calls[0]["system"] == "bot"


def test_speakable_strips_markdown():
    md = ("## Your loans\n| Loan ID | EMI |\n|---|---|\n| **LN100001** | 10,180 |\n"
          "- Next due: `2026-12-05`\n1. See [details](http://x).")
    assert speakable(md) == "Your loans\nLoan ID, EMI\nLN100001, 10,180\nNext due: 2026-12-05\nSee details."
