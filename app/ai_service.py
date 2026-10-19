import json
import logging
import re
from datetime import datetime, timedelta, timezone
from groq import AsyncGroq
from app.config import settings

logger = logging.getLogger(__name__)

TZ_MSK = timezone(timedelta(hours=3), name="Europe/Moscow")


class AIService:
    """Структурирование голосовых заметок в markdown (Groq, gpt-oss-120b)."""

    def __init__(self):
        self.client = AsyncGroq(api_key=settings.groq_api_key.get_secret_value())
        self.model = "openai/gpt-oss-120b"

    async def structure_note(self, transcript: str) -> str:
        """
        Превращает стенограмму голосовой заметки в структурированный Markdown.
        CoT через <scratchpad> — потом зачищается из ответа.
        """
        logger.info(f"Структурируем заметку ({len(transcript)} символов стенограммы)...")

        system_prompt = (
            "Ты — редактор личных голосовых заметок. Автор наговаривает заметки на ходу, с потоком мыслей. "
            "Ты чистишь речь, НЕ пересказываешь. Язык ответа — русский, живой, автора.\n\n"
            "## ГЛАВНОЕ ПРАВИЛО\n"
            "Тело заметки — дословный текст автора. Сохраняй его стиль, лексику, интонацию, короткие фразы. "
            "Категорически ЗАПРЕЩЕНО:\n"
            "- пересказывать своими словами и обобщать;\n"
            "- переводить в официальный/деловой/технический язык;\n"
            "- использовать слова «автор», «данный», «осуществить», «целесообразно», «в связи с», «данный вопрос» и подобный канцелярит;\n"
            "- добавлять оценки, советы и выводы, которых не было.\n\n"
            "## ЧТО ТЫ ДЕЛАЕШЬ С ТЕКСТОМ\n"
            "1. Убери слова-паразиты: «эээ», «ммм», «ну», «типа», «как бы», «короче», «значит», «вот это», «как там», «это самое».\n"
            "2. Убери ложные начала фраз и повторы (оставь одну лучшую формулировку).\n"
            "3. Разбей на абзацы по смыслу — это единственная правка структуры.\n"
            "4. Имена, цифры, детали — дословно.\n\n"
            "## ВЫВОД\n"
            "Сначала <scratchpad>: кратко определи тему, 3–5 самых важных тезисов словами автора, есть ли задачи. "
            "Проверь: не вкрался ли канцелярит? Не пересказал ли ты вместо того чтобы чистить?\n"
            "Потом сам ответ.\n\n"
            "## ФОРМАТ ОТВЕТА (строго)\n"
            "<scratchpad>\n"
            "[краткий анализ]\n"
            "</scratchpad>\n\n"
            "## Самое важное\n"
            "- тезис словами автора\n"
            "- тезис\n"
            "- тезис\n\n"
            "[тело заметки: дословный очищенный текст, абзацами. Без заголовков внутри тела]\n\n"
            "## Задачи\n"
            "- [ ] задача (только если автор реально назвал задачи; иначе блок не пиши)\n\n"
            "Блок «## Самое важное» тоже опусти, если в заметке нет содержательных тезисов (пустая болтовня)."
        )

        user_content = f"Стенограмма голосовой заметки:\n\n{transcript}"

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.1,
            )

            response_content = response.choices[0].message.content
            clean_content = re.sub(
                r"<scratchpad>.*?</scratchpad>", "", response_content, flags=re.DOTALL
            ).strip()

            logger.info("Заметка структурирована успешно.")
            return clean_content

        except Exception as e:
            logger.error(f"Ошибка LLM при структурировании: {e}")
            raise e

    async def parse_reminder(self, text: str, now_moscow: datetime) -> dict | None:
        """
        Ищет в тексте явную просьбу-напоминание. Возвращает dict
        {when: datetime (MSK), text: str} или None.
        """
        prompt = (
            "Ты парсер напоминаний. Из текста пользователя извлеки явную просьбу напомнить о чём-то "
            "и когда. Если просьбы-напоминания НЕТ — верни только {\"has\": false}.\n"
            "Правила времени:\n"
            "- «завтра в 15:00», «через 2 часа», «в пятницу утром», «сегодня вечером», «25.10 в 9» — понимай.\n"
            "- «утро» = 09:00, «день» = 13:00, «вечер» = 19:00, «ночь» = 22:00, если время не указано.\n"
            "- Все относительные даты считай ОТ «сейчас» ниже, это московское время (Europe/Moscow).\n"
            "- Результат — время в ISO формате YYYY-MM-DDTHH:MM (это московское время).\n"
            "- «text» — короткая суть: о чём напомнить (без «напомни мне»).\n"
            "- Повторы: «каждый день» → repeat=\"daily\"; «по будням» → \"weekdays\"; "
            "«каждую среду/понедельник...» → \"weekly:N\" (пн=0, вт=1, ср=2, чт=3, пт=4, сб=5, вс=6); "
            "«ежемесячно 15-го» → \"monthly:15\". Нет повтора → repeat=null.\n"
            "Ответ ТОЛЬКО JSON: {\"has\": true, \"when\": \"2026-10-20T15:00\", \"text\": \"...\", \"repeat\": null}"
        )
        user_content = f"Сейчас в Москве (Europe/Moscow): {now_moscow.strftime('%Y-%m-%d %H:%M, %A')}\n\nТекст: {text}"
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_content},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content.strip()
            data = json.loads(raw)
            if not data.get("has"):
                return None
            when = datetime.strptime(data["when"], "%Y-%m-%dT%H:%M").replace(tzinfo=TZ_MSK)
            repeat = data.get("repeat")
            repeat = str(repeat).strip() if repeat else None
            return {"when": when, "text": str(data.get("text", "")).strip(), "repeat": repeat}
        except Exception as e:
            logger.error(f"Ошибка парсинга напоминания: {e}")
            return None

    async def parse_task(self, text: str) -> str | None:
        """
        Ищет в тексте явную задачу («задача: ...», «не забыть сделать X», «todo: ...»).
        Возвращает текст задачи или None.
        """
        prompt = (
            "Ты парсер задач. Если в тексте есть явная задача («задача: ...», «сделай ...», "
            "«не забыть сделать ...», «todo: ...») — верни JSON {\"has\": true, \"text\": \"суть задачи\"}.\n"
            "Если задачи нет (обычная болтовня, вопрос) — {\"has\": false}.\n"
            "text — коротко, без «задача:», на русском. Ответ ТОЛЬКО JSON."
        )
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": text},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            data = json.loads(response.choices[0].message.content.strip())
            if not data.get("has"):
                return None
            t = str(data.get("text", "")).strip()
            return t or None
        except Exception as e:
            logger.error(f"Ошибка парсинга задачи: {e}")
            return None

    async def chat(
        self,
        user_text: str,
        context: str = "",
        execute_tool=None,
    ) -> str:
        """Свободная беседа. Если дан execute_tool — работает агент-цикл (tools).

        execute_tool: async (name: str, args: dict) -> str | список маркеров-инструментов.
        Без инструментов — обычный ответ (как раньше).
        """
        system_prompt = (
            "Ты — «Личный ИИ», ассистент Игоря в Telegram. Отвечай по-русски, живо и коротко "
            "(обычно 2-4), без канцелярита и без формальных заступов.\n"
            "Если в контексте есть его реальные дела/заметки/задачи — можешь опираться на них, "
            "но не выдумывай факты, которых нет.\n"
            "Не навязывай помощь, не перечисляй свои функции, если не просят. Просто отвечай по делу."
        )
        content = user_text
        if context:
            content = f"Контекст дел пользователя:\n{context}\n\nСообщение пользователя: {user_text}"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ]

        if execute_tool is None:
            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.3,
                )
                return response.choices[0].message.content.strip()
            except Exception as e:
                logger.error(f"Ошибка чата: {e}")
                raise e

        # --- агент-цикл с инструментами ---
        tools = self._agent_tools()
        system_prompt += (
            "\n\nУ тебя есть инструменты. Зови их, когда нужно свежее или личное:\n"
            "- web_search: новости, курсы, погода, что-то из интернета;\n"
            "- search_notes: его личные заметки (планы, идеи, встречи);\n"
            "- get_balance: его текущий финансовый баланс.\n"
            "Сначала подумай, нужен ли инструмент. Если можешь ответить из головы или из "
            "контекста дел — ответь сразу без вызова. После инструмента дай короткий вывод."
        )
        messages[0]["content"] = system_prompt

        try:
            for _ in range(4):  # максимум 4 итерации — против циклов
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    temperature=0.3,
                )
                msg = response.choices[0].message
                tool_calls = getattr(msg, "tool_calls", None) or []
                if not tool_calls:
                    return (msg.content or "").strip()

                messages.append(
                    {
                        "role": "assistant",
                        "content": msg.content or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments,
                                },
                            }
                            for tc in tool_calls
                        ],
                    }
                )
                for tc in tool_calls:
                    name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except Exception:
                        args = {}
                    try:
                        result = await execute_tool(name, args)
                    except Exception as e:
                        logger.error(f"Ошибка инструмента {name}: {e}")
                        result = f"Ошибка инструмента {name}: {e}"
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": str(result)}
                    )
            # выжали все итерации — просим финальный ответ без инструментов
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.3,
            )
            return (response.choices[0].message.content or "").strip()
        except Exception as e:
            logger.error(f"Ошибка чата-агента: {e}")
            raise e

    def _agent_tools(self) -> list[dict]:
        """Схемы инструментов агента (OpenAI function-calling формат)."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": (
                        "Поиск в интернете. Используй для свежих новостей, курсов валют, погоды, "
                        "спортрезультатов, дат, любой информации, которой нет в твоей голове."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Поисковый запрос"}
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_notes",
                    "description": (
                        "Поиск по личным заметкам Игоря (его голосовые заметки, планы, идеи, встречи). "
                        "Используй, когда вопрос о его делах."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Ключевые слова для поиска"}
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_balance",
                    "description": "Текущий финансовый баланс Игоря за этот месяц (доходы/расходы/остаток).",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]


    async def extract_fact(self, text: str) -> str | None:
        """
        Извлекает из реплики долговременный факт об Игоре (для памяти).
        Возвращает одно короткое утверждение («Игорь живёт в Москве») или None,
        если запоминать нечего (обычный вопрос/болтовня).
        """
        prompt = (
            "Ты извлекатель фактов для личной памяти ассистента. Из реплики пользователя "
            "выдели ОДИН долговременный факт о нём — короткое утверждение от третьего лица, "
            "в настоящем времени, на русском («Игорь живёт в Москве», "
            "«Игорь не любит кофе после 18:00», «У Игоря кот Барс»).\n"
            "НЕ выдумывай: опирайся только на сказанное. Если это вопрос, просьба о действии "
            "(«напомни…», «задача…»), вежливость или факт о ком-то другом — факта нет.\n"
            "Ответ ТОЛЬКО JSON: {\"fact\": \"...\"} или {\"fact\": null}."
        )
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": text},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            data = json.loads(response.choices[0].message.content.strip())
            fact = str(data.get("fact") or "").strip()
            return fact or None
        except Exception as e:
            logger.error(f"Ошибка извлечения факта: {e}")
            return None

    async def parse_finance(self, text: str, today_msk: str) -> dict | None:
        """
        Ищет в тексте операцию расхода/дохода с суммой. Возвращает dict
        {amount: float (знаковый: доход +, расход −), category, comment, date: YYYY-MM-DD}
        или None.
        """
        prompt = (
            "Ты парсер финансовых операций из голосовых заметок. Если в тексте НЕТ операции "
            "с конкретной суммой денег — верни только {\"is_finance\": false}.\n"
            "Правила:\n"
            "- «потратил 500 на такси», «заплатили 30 000 за аренду», «заплатил за бензин 2000», "
            "«получил 15000 зарплата», «купил продукты на 1200» — это операции.\n"
            "- amount — число со знаком: расход отрицательный (−500), доход положительный (+15000). Без валюты в number.\n"
            "- category — одно короткое слово: такси, продукты, аренда, бензин, зарплата, кафе, связь, ЖКХ, подарки, прочее.\n"
            "- comment — суть операции одним коротким фразисловами автора.\n"
            "- date — YYYY-MM-DD; если не сказано — сегодня (указан ниже).\n"
            "Ответ ТОЛЬКО JSON: {\"is_finance\": true, \"amount\": -500, \"category\": \"такси\", \"comment\": \"такси\", \"date\": \"2026-10-19\"}"
        )
        user_content = f"Сегодня в Москве: {today_msk}\n\nТекст: {text}"
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_content},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content.strip()
            data = json.loads(raw)
            # Модель может вернуть операцию и без явного is_finance — ключевое поле amount
            if not data.get("is_finance") and "amount" not in data:
                return None
            if data.get("is_finance") is False:
                return None
            amount = float(data["amount"])
            if amount == 0:
                return None
            return {
                "amount": amount,
                "category": str(data.get("category", "прочее")).strip() or "прочее",
                "comment": str(data.get("comment", "")).strip(),
                "date": str(data.get("date", today_msk)).strip()[:10],
            }
        except Exception as e:
            logger.error(f"Ошибка парсинга финансов: {e}")
            return None
