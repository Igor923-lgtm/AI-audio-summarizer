"""Синтез речи через edge-tts (Microsoft neural voices, бесплатно)."""
import io
import logging

import edge_tts

logger = logging.getLogger(__name__)

VOICES = {
    "dmitry": "ru-RU-DmitryNeural",
    "svetlana": "ru-RU-SvetlanaNeural",
}
DEFAULT_VOICE = "dmitry"
MAX_CHARS = 400


async def synthesize(text: str, voice_key: str = DEFAULT_VOICE) -> bytes | None:
    """Текст → MP3. None при ошибке (вызывающий отправляет только текст)."""
    text = (text or "").strip()
    if not text:
        return None
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "..."
    voice = VOICES.get(voice_key, VOICES[DEFAULT_VOICE])
    try:
        communicate = edge_tts.Communicate(text, voice)
        buf = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        data = buf.getvalue()
        if len(data) < 500:
            logger.error(f"TTS вернул слишком мало данных ({len(data)} байт)")
            return None
        return data
    except Exception as e:
        logger.error(f"TTS ошибка: {e}")
        return None
