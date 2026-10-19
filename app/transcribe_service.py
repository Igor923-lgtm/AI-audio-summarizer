import os
import asyncio
import logging
from groq import AsyncGroq
from app.config import settings

logger = logging.getLogger(__name__)

class TranscribeService:
    """Сервіс для транскрибування аудіо за допомогою Groq Whisper API."""

    def __init__(self):
        # Ініціалізуємо асинхронного клієнта Groq
        self.client = AsyncGroq(api_key=settings.groq_api_key.get_secret_value())
        # Використовуємо найкращу модель розпізнавання мови від Groq
        self.model = "whisper-large-v3"

    async def transcribe_file(self, file_path: str) -> str:
        """Транскрибує один аудіофайл."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Файл {file_path} не знайдено.")

        logger.info(f"Відправляємо {file_path} на Groq Whisper...")
        
        try:
            with open(file_path, "rb") as file:
                transcription = await self.client.audio.transcriptions.create(
                    file=(os.path.basename(file_path), file.read()),
                    model=self.model,
                    # Подсказка улучшает распознавание русской речи и личных заметок
                    prompt="Личная голосовая заметка: планы, идеи, задачи, мысли на русском языке.",
                    response_format="text"
                )
            
            # transcription повертається як рядок тексту у форматі "text"
            text = str(transcription).strip()
            logger.info(f"Транскрипція для {file_path} успішно отримана ({len(text)} символів)")
            return text
            
        except Exception as e:
            logger.error(f"Помилка при транскрибуванні {file_path}: {e}")
            raise e

    async def transcribe_chunks(self, chunk_paths: list[str]) -> str:
        """
        Транскрибує кілька чанків послідовно (або паралельно) 
        та об'єднує їх в один великий текст.
        """
        results = []
        for i, path in enumerate(chunk_paths):
            logger.info(f"Обробка чанка {i+1} з {len(chunk_paths)}")
            
            # Робимо невелику паузу між запитами, щоб не отримати Rate Limit від Groq
            if i > 0:
                await asyncio.sleep(2)
                
            try:
                text = await self.transcribe_file(path)
                results.append(text)
                
                # Видаляємо тимчасовий чанк з диску після успішного розпізнавання
                if os.path.exists(path):
                    os.remove(path)
            except Exception as e:
                logger.error(f"Не вдалося транскрибувати чанк {path}: {e}")
                results.append(f"\n[Помилка транскрипції частини {i+1}]\n")
                
        # Поєднуємо всі частини через пробіл
        full_transcript = "\n\n".join(results)
        return full_transcript
