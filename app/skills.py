"""Скиллы — правила владельца в .md-файлах, подгружаются в system prompt.

Позволяют менять поведение ассистента БЕЗ правки кода: положи/отредактируй
skills/<имя>.md и задеплой. Файлы читаются при каждом вызове чата (директория
маленькая — дёшево), лимит символов защищает промпт от раздувания.
"""
import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_DIR = os.environ.get("SKILLS_DIR", "skills")
MAX_TOTAL = 3000  # символов суммарно — хватает на несколько правил
MIN_USEFUL = 80  # блок меньше этого — считаем пустым


def load_skills(skills_dir: str | None = None) -> str:
    """Читает skills/*.md и склеивает в одну строку для system prompt.

    Возвращает "" если директории нет или правил не найдено.
    """
    d = skills_dir or DEFAULT_DIR
    if not os.path.isdir(d):
        return ""
    blocks: list[str] = []
    total = 0
    for name in sorted(os.listdir(d)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(d, name)
        try:
            with open(path, encoding="utf-8") as f:
                body = f.read().strip()
        except OSError as e:
            logger.warning(f"Не прочитал скилл {path}: {e}")
            continue
        if len(body) < MIN_USEFUL:
            continue
        title = os.path.splitext(name)[0]
        block = f"### {title}\n{body}"
        if total + len(block) > MAX_TOTAL:
            remain = MAX_TOTAL - total
            if remain > 120:
                blocks.append(block[:remain])
            break
        blocks.append(block)
        total += len(block)
    if not blocks:
        return ""
    return "Правила владельца (скиллы) — соблюдай всегда:\n\n" + "\n\n".join(blocks)
