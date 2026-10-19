"""Бесплатный веб-поиск (DuckDuckGo, пакет ddgs) для агента. Без API-ключей.

ddgs синхронный и блокирующий — вызываем через asyncio.to_thread,
чтобы не держать event loop бота.
"""
import asyncio
import logging

from ddgs import DDGS

logger = logging.getLogger(__name__)

MAX_RESULTS = 5
TIMEOUT = 12


def _search_sync(query: str, max_results: int) -> list[dict]:
    try:
        with DDGS() as ddgs:
            rows = list(ddgs.text(query, max_results=max_results, region="ru-ru"))
        return [
            {
                "title": str(r.get("title") or "").strip(),
                "url": str(r.get("href") or r.get("url") or "").strip(),
                "snippet": str(r.get("body") or "").strip(),
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"Ошибка веб-поиска: {e}")
        return []


async def web_search(query: str, max_results: int = MAX_RESULTS) -> str:
    """Ищет в интернете и возвращает текстовый блок для передачи в LLM.

    Пустой результат → вежливая строка, чтобы агент знал: интернет молчит.
    """
    query = (query or "").strip()
    if not query:
        return "Веб-поиск: пустой запрос."
    try:
        rows = await asyncio.wait_for(
            asyncio.to_thread(_search_sync, query, max_results), timeout=TIMEOUT
        )
    except asyncio.TimeoutError:
        logger.error(f"Веб-поиск таймаут: {query}")
        return f"Веб-поиск по «{query}» не успел ответить (таймаут)."

    if not rows:
        return f"Веб-поиск по «{query}» ничего не нашёл."

    lines = [f"Результаты поиска по «{query}»:"]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r['title']}\n   {r['snippet']}\n   {r['url']}")
    return "\n".join(lines)
