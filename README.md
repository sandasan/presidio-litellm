# presidio-litellm

Docker-стек для запуска агента Hermes с безопасным доступом к бесплатным ИИ-моделям:

```
Hermes Agent ──► LiteLLM Proxy ──► Presidio (анонимизация PII) ──► провайдер ИИ
                     ▲
                     └── при старте: опрос провайдеров + генерация config.yaml
```

Все исходящие запросы к облачным моделям проходят через **Presidio**: персональные данные
(email, имена, телефоны, API-ключи, строки подключения к БД, внутренние IP) маскируются
до отправки на сторону провайдера.

## Состав

| Сервис       | Назначение                                                                   |
|--------------|------------------------------------------------------------------------------|
| `presidio`   | Анализатор и анонимизатор PII (своя FastAPI-обёртка + кастомные распознаватели) |
| `litellm`    | Прокси к провайдерам ИИ. При старте генерирует `litellm-proxy/config.yaml`     |
| `hermes-agent`| Агент Hermes (Nous Research), работает через LiteLLM как `custom`-провайдер     |

## Быстрый старт

1. **Заполните ключи** в `.env` (копия `.env.example`):

   ```bash
   cp .env.example .env
   # впишите свои ключи:
   # OPENROUTER_API_KEY, GEMINI_API_KEY, GROQ_API_KEY, MISTRAL_API_KEY
   ```

2. **Запуск через скрипт** (пересоберёт `presidio`, пересоздаст контейнеры и запустит чат):

   ```bash
   ./update_models_and_run.sh
   ```

   Или по шагам:

   ```bash
   docker compose up -d --build
   docker exec -e CUSTOM_BASE_URL=http://litellm:4000/v1 -e CUSTOM_API_KEY=sk-dummy \
     hermes-agent hermes chat --provider custom -m cloud-sanitized
   ```

## Как работает выбор моделей

При каждом старте контейнера `litellm` выполняется `litellm-proxy/generate_config.py`:

1. Опрашиваются провайдеры, у которых задан API-ключ: **OpenRouter**, **Gemini**,
   **Groq**, **Mistral**. (opencode/zen не используется: его free-модели доступны
   только изнутри opencode и возвращают 403 при вызове извне.)
2. Для каждого выбирается доступная **бесплатная** модель с поддержкой tool-use
   (агенту нужны инструменты).
3. Все маршруты попадают в `model_list` под именем `cloud-sanitized` — LiteLLM
   распределяет запросы между ними (стратегия `simple-shuffle`, см. ниже).
4. Итог пишется в `litellm-proxy/config.yaml` — тот самый конфиг, который грузит прокси.

Генератор можно запустить и вручную, чтобы обновить конфиг без перезапуска стека:

```bash
python3 litellm-proxy/generate_config.py
```

## Автоматическое переключение при лимитах (429 / лимит токенов)

В `litellm-proxy/generate_config.py` в `router_settings` зашита следующая логика
(`routing_strategy: simple-shuffle`, `num_retries: 4`, `cooldown_time: 60`,
`retry_policy.BadRequestErrorRetries: 1`):

- стратегия `simple-shuffle` — каждый запрос почти случайно выбирает одного из
  живых провайдеров: если один исчерпал лимит, следующие запросы на нём не
  концентрируются (в отличие от `usage-based-routing`, который «прилипал» к
  сломанному провайдеру);
- при ошибке `429 Rate Limit` провайдер немедленно уходит в cooldown (`cooldown_time`),
  а запрос автоматически повторяется на **другом** провайдере до `num_retries` раз;
- ошибка лимита токенов (`400 context_length_exceeded` / `Request too large`)
  один раз ретраится на другом провайдере из-за `BadRequestErrorRetries: 1`;
- если все бесплатные провайдеры одновременно исчерпали свои дневные/минутные
  квоты, запрос вернёт `429` — это ожидаемо, пул полностью занят (выбирать не из чего).

Эмпирически проверено на тестовом «фейке лимита»: модель, отдающая 429, получала
ровно 1 попадание на запрос, остальные ретраи уходили на живого провайдера.

## Анонимизация

- **Президио** (`presidio-server/app.py`) загружает кастомные распознаватели из
  `presidio_config.yaml` (сущности `SECRET_KEY`, `DB_CONNECTION`, `INTERNAL_IP`
  поверх стандартных `PERSON`, `EMAIL_ADDRESS`, `PHONE_NUMBER`, `CREDIT_CARD` и т.д.).
- LiteLLM подключает Presidio через callback `callbacks: ["presidio"]` и env-переменные
  `PRESIDIO_ANALYZER_API_BASE` / `PRESIDIO_ANONYMIZER_API_BASE`.
- Проверка: `curl http://localhost:5001/health` → `{"status":"ok"}`.

## Полезные проверки

```bash
# здоровье сервисов
curl http://localhost:5001/health          # presidio
curl http://localhost:4000/health/liveliness  # litellm

# какие маршруты попали в конфиг
grep 'model:' litellm-proxy/config.yaml

# чат через прокси напрямую (PII обязательно маскируется)
curl http://localhost:4000/v1/chat/completions \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer sk-dummy' \
  -d '{"model":"cloud-sanitized","messages":[{"role":"user","content":"Мой email vasya@example.com, назови столицу Франции"}]}'
```

## Примечания

- В `.env` хранятся живые ключи — **не коммитьте его в git**.
- Некоторые бесплатные модели имеют общий пул лимитов и могут временно отдавать
  `429` — LiteLLM автоматически переключится на другого провайдера из списка.
- Кастомный callback `litellm-proxy/custom_presidio.py` не используется — работает
  встроенный callback `"presidio"`.