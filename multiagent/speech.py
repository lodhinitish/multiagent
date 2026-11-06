"""Speech-to-text and text-to-speech used by the voice WebSocket.

Both are small interfaces so you can swap in another vendor (Deepgram,
ElevenLabs, a local Whisper...) by subclassing and passing instances to
`create_app(stt=..., tts=...)`. The defaults use OpenAI and OPENAI_API_KEY.

Env:
    STT_MODEL   default gpt-4o-mini-transcribe
    TTS_MODEL   default gpt-4o-mini-tts
    TTS_VOICE   default alloy
    TTS_INSTRUCTIONS  how the voice should sound (gpt-4o-mini-tts and newer; ignored by tts-1)
"""
from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Literal

AudioFormat = Literal["mp3", "opus", "aac", "flac", "wav", "pcm"]

# MIME types for the audio a client may upload, keyed by file extension.
INPUT_MIME = {
    "webm": "audio/webm", "wav": "audio/wav", "mp3": "audio/mpeg", "mp4": "audio/mp4",
    "m4a": "audio/mp4", "ogg": "audio/ogg", "flac": "audio/flac", "mpeg": "audio/mpeg",
}


# Added to the agent's system prompt for voice conversations, so the reply works when heard.
VOICE_STYLE = """You are in a live voice conversation. Your reply is converted to speech, so:
- Answer in short, natural spoken sentences. No markdown, tables, bullet points, emojis or headings.
- Give at most three or four key facts per reply, then offer to share more.
- Say amounts in words the way a person would, e.g. "three lakh rupees", "eleven thousand two hundred rupees".
- Say dates naturally, e.g. "the fifth of December".
- Spell out IDs and codes in groups, e.g. "L N, one zero zero, zero zero one".
- When you need details from the customer, ask for one or two at a time.
- If something you heard is unclear, such as a number, repeat it back and ask them to confirm."""

DEFAULT_TTS_INSTRUCTIONS = "Speak clearly, warmly and at a calm pace, like a helpful customer service agent."


def speakable(text: str) -> str:
    """Strip markdown the model may still produce, so TTS doesn't read symbols aloud."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"!?\[([^\]]+)\]\([^)]*\)", r"\1", text)          # links/images -> label
    text = re.sub(r"^[ \t]*\|?[ \t]*:?-{3,}.*$", "", text, flags=re.M)  # table separator rows
    text = re.sub(r"^[ \t]*\|(.*)$", lambda m: ", ".join(c.strip() for c in m.group(1).split("|") if c.strip()),
                  text, flags=re.M)                                     # table rows -> "a, b, c"
    text = re.sub(r"^\s{0,3}(#{1,6}|>|[-*+]|\d+[.)])\s+", "", text, flags=re.M)  # headings, quotes, bullets
    text = re.sub(r"(\*\*|__|\*|_|`|~~)", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip(" ,\n")


class SpeechToText(ABC):
    @abstractmethod
    async def transcribe(self, audio: bytes, *, fmt: str = "webm", language: str | None = None) -> str:
        """Turn one complete utterance (an encoded audio file) into text."""


class TextToSpeech(ABC):
    @abstractmethod
    def synthesize(self, text: str, *, voice: str | None = None,
                   fmt: AudioFormat = "mp3") -> AsyncIterator[bytes]:
        """Yield encoded audio chunks as soon as they are produced."""


def _openai_client(client: Any | None) -> Any:
    if client is not None:
        return client
    from openai import AsyncOpenAI  # lazy: optional dependency

    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set (voice uses OpenAI speech). "
                           "Add it to the .env file and restart the server.")
    return AsyncOpenAI(api_key=key, base_url=os.getenv("OPENAI_BASE_URL"))


class OpenAISpeechToText(SpeechToText):
    def __init__(self, model: str | None = None, client: Any | None = None) -> None:
        self.model = model or os.getenv("STT_MODEL", "gpt-4o-mini-transcribe")
        self._client = client

    async def transcribe(self, audio: bytes, *, fmt: str = "webm", language: str | None = None) -> str:
        self._client = _openai_client(self._client)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "file": (f"audio.{fmt}", audio, INPUT_MIME.get(fmt, "application/octet-stream")),
        }
        if language:
            kwargs["language"] = language
        result = await self._client.audio.transcriptions.create(**kwargs)
        return (getattr(result, "text", None) or "").strip()


class OpenAITextToSpeech(TextToSpeech):
    def __init__(self, model: str | None = None, voice: str | None = None,
                 client: Any | None = None, chunk_size: int = 4096) -> None:
        self.model = model or os.getenv("TTS_MODEL", "gpt-4o-mini-tts")
        self.voice = voice or os.getenv("TTS_VOICE", "alloy")
        self.instructions = os.getenv("TTS_INSTRUCTIONS", DEFAULT_TTS_INSTRUCTIONS)
        self.chunk_size = chunk_size
        self._client = client

    async def synthesize(self, text: str, *, voice: str | None = None,
                         fmt: AudioFormat = "mp3") -> AsyncIterator[bytes]:
        self._client = _openai_client(self._client)
        kwargs: dict[str, Any] = {"model": self.model, "voice": voice or self.voice,
                                   "input": text, "response_format": fmt}
        if self.instructions and not self.model.startswith("tts-1"):  # tts-1 doesn't accept instructions
            kwargs["instructions"] = self.instructions
        async with self._client.audio.speech.with_streaming_response.create(**kwargs) as resp:
            async for chunk in resp.iter_bytes(self.chunk_size):
                if chunk:
                    yield chunk
