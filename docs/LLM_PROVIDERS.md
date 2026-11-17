# Модели и API-ключи

Эта версия бота работает с **любым OpenAI-совместимым API**: DeepSeek, OpenAI,
OpenRouter, Qwen (DashScope), Groq, Together, Mistral, локальные Ollama / vLLM /
LM Studio. Google Cloud не нужен.

Все настройки — переменные `LLM_*` в `.env`. Код бота не меняется.

## Минимальная настройка

```env
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-...
LLM_SMART_MODEL=deepseek-chat
```

С этим работают вопросы по законам, ролевой помощник, жалобы, документы,
уголовные дела, анализатор пробелов в законах. Чтобы включить остальное, задайте
роли ниже.

## Роли

У каждой роли может быть свой поставщик и ключ: `LLM_<РОЛЬ>_BASE_URL`,
`LLM_<РОЛЬ>_API_KEY`, `LLM_<РОЛЬ>_MODEL`. Пустые адрес и ключ берутся из общих
`LLM_BASE_URL` / `LLM_API_KEY`.

| Роль | Что делает | Если не задана |
|---|---|---|
| `SMART` | Ответы, помощник, документы, анализ жалоб | **обязательна** |
| `FAST` | Роутеры законов и прецедентов, оптимизатор вопроса, сводки | берётся `SMART` |
| `VISION` | Картинки: OCR паспорта, скриншоты из судебных тем | берётся `SMART` (модель должна понимать картинки) |
| `EMBED` | Векторы для RAG: советы оверлея, Q&A v2, ролевой помощник | эти функции не работают |
| `STT` | Голосовые вопросы в Telegram, голосовой оверлей | голос выключен |

Дешёвая быстрая модель на `FAST` заметно ускоряет ответ: роутеры вызываются перед
каждым основным ответом.

## Готовые варианты

Имена моделей и адреса — примеры; актуальные смотрите у поставщика.

### DeepSeek (основной текст) + OpenAI (картинки, векторы, голос)

DeepSeek дешёвый и хорошо пишет по-русски, но не принимает картинки и не делает
векторы и распознавание речи. Эти роли закрываем другим поставщиком.

```env
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-deepseek...
LLM_SMART_MODEL=deepseek-chat
LLM_FAST_MODEL=deepseek-chat

LLM_VISION_BASE_URL=https://api.openai.com/v1
LLM_VISION_API_KEY=sk-openai...
LLM_VISION_MODEL=gpt-4o-mini

LLM_EMBED_BASE_URL=https://api.openai.com/v1
LLM_EMBED_API_KEY=sk-openai...
LLM_EMBED_MODEL=text-embedding-3-large

LLM_STT_BASE_URL=https://api.openai.com/v1
LLM_STT_API_KEY=sk-openai...
LLM_STT_MODEL=whisper-1
```

### Только OpenAI

```env
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-...
LLM_SMART_MODEL=gpt-4o
LLM_FAST_MODEL=gpt-4o-mini
LLM_EMBED_MODEL=text-embedding-3-large
LLM_STT_MODEL=whisper-1
```

### OpenRouter (один ключ — сотни моделей)

```env
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=sk-or-...
LLM_SMART_MODEL=deepseek/deepseek-chat
LLM_FAST_MODEL=google/gemini-flash-1.5
LLM_VISION_MODEL=openai/gpt-4o-mini
LLM_HTTP_REFERER=https://your.site
LLM_APP_TITLE=TurgovBot
```

Векторы и распознавание речи у OpenRouter, насколько известно, не предоставляются —
для `EMBED` и `STT` задайте другого поставщика.

### Голос через Groq

```env
LLM_STT_BASE_URL=https://api.groq.com/openai/v1
LLM_STT_API_KEY=gsk_...
LLM_STT_MODEL=whisper-large-v3-turbo
```

### Локально (Ollama)

```env
LLM_BASE_URL=http://host.docker.internal:11434/v1
LLM_API_KEY=ollama
LLM_SMART_MODEL=qwen2.5:14b
LLM_EMBED_MODEL=bge-m3
LLM_MAX_INPUT_CHARS=60000
```

Окно локальных моделей обычно небольшое — уменьшите `LLM_MAX_INPUT_CHARS`.

## Что важно знать

**Объём законов.** Gemini принимал до ~1 млн токенов, у большинства других моделей
окно 32–200 тыс. Если документы не влезают в `LLM_MAX_INPUT_CHARS` (по умолчанию
300 000 символов, около 100–120 тыс. токенов), адаптер отбрасывает последние
документы и пишет предупреждение в лог. Законы, выбранные роутером, идут первыми,
поэтому обычно отбрасываются наименее нужные.

**Векторы.** Столбцы в БД рассчитаны на 3072 измерения. Модели с меньшей
размерностью (например, 1536) дополняются нулями — поиск работает так же. Модель
с большей размерностью не подойдёт, если она не умеет сокращать векторы
(`LLM_EMBED_SEND_DIMENSIONS=true` для `text-embedding-3-*`). **После смены
модели векторов** старые векторы несовместимы с новыми — законы нужно
переиндексировать (повторный синк базы законов).

**JSON-ответы.** Часть функций (анализ жалоб, паспорт, план) просит у модели JSON.
По умолчанию используется `response_format: json_object`. Если поставщик его не
поддерживает и отвечает ошибкой 400, поставьте `LLM_JSON_MODE=prompt`.

**«Размышления».** Уровни размышлений Gemini другим API по умолчанию не передаются
(многие отвечают 400 на неизвестное поле). Для OpenAI o-серии и gpt-5 включите
`LLM_SEND_REASONING_EFFORT=true`. Теги `<think>…</think>` reasoning-моделей
(DeepSeek-R1, QwQ) из ответов вырезаются автоматически.

**Лимиты.** Встроенный лимитер считает запросы по ролям; лимит в минуту задаётся
`LLM_<РОЛЬ>_QPM`. Ошибки 429 и 5xx повторяются автоматически.

**Данные пользователей.** В запросы попадают тексты жалоб, ники и паспортные данные
(OCR). Выбирая поставщика, учитывайте, где и как он хранит запросы.

## Проверка

В админ-панели бота (суперадмин) кнопка **«🔑 Модели и ключи»** показывает, какая
модель стоит на каждой роли и настроена ли она.

## Как это устроено

`common/llm/compat.py` — клиент с тем же интерфейсом, что у google-genai
(`client.aio.models.generate_content` и т. д.), поэтому более 60 мест вызова модели,
все системные промпты и тесты остались без изменений. Адаптер переводит запросы в
формат `/chat/completions`, подставляет текст законов вместо ссылок на файлы,
отправляет картинки в роль `VISION`, переводит вызовы функций (поиск статей в Q&A v2)
и ошибки HTTP.
