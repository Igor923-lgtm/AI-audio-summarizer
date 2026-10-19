"""Еженедельная рефлексия — ассистент разбирает свои ответы за неделю.

Раз в неделю (см. main.send_reflection_if_needed) собирает историю диалога и
факты, просит LLM честно указать: новые факты, слабые ответы, одно правило
(скилл). Ничего не пишет в код/файлы сам — только присылает владельцу
предложение. Вступает в силу через «запомни ...» (факт) или правку skills/*.md.
"""
import logging

logger = logging.getLogger(__name__)

MIN_TURNS = 6  # мало данных — не навязываемся


async def build_reflection(ai, chat_id: int, history_limit: int = 40, facts_limit: int = 50) -> str:
    """Собирает данные памяти и возвращает текст разбора ("" — если нечего)."""
    from app import memory

    hist = memory.get_history(chat_id, history_limit)
    if len(hist) < MIN_TURNS:
        return ""
    facts = memory.list_facts(chat_id, facts_limit)
    turns = "\n".join(f"{r['role']}: {r['text'][:200]}" for r in hist)
    fact_list = "\n".join(f"- {f}" for f in facts) or "(фактов пока нет)"
    try:
        return await ai.self_review(turns, fact_list)
    except Exception as e:
        logger.error(f"Ошибка рефлексии: {e}")
        return ""
