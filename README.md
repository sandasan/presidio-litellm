# presidio-litellm

Docker-стек для запуска агента Hermes с безопасным доступом к бесплатным ИИ-моделям:

```
Hermes Agent ──► LiteLLM Proxy ──► Presidio (анонимизация PII) ──► OmniRoute ──► провайдер ИИ
                     ▲                                          │
                     └── при старте: опрос провайдеров + генерация config.yaml
                                                                 └ маршрут auto: выбор/фолбэк между 350+ провайдерами
```

Все исходящие запросы к облачным моделям проходят через **Presidio**: персональные данные
(email, имена, телефоны, API-ключи, строки подключения к БД, внутренние IP) маскируются
до отправки на сторону провайдера.

У роутинга два пути (оба — за слоем анонимизации):

| Маршрут               | Как работает                                                                  |
|-----------------------|-------------------------------------------------------------------------------|
| `cloud-sanitized`     | Классический: LiteLLM сам shuffle-ит между OpenRouter/Gemini/Groq/Mistral       |
| `cloud-sanitized-auto`| Через OmniRoute: тот выбирает целевую модель сам и сам фолбэчится между квотами |

## Состав

| Сервис       | Назначение                                                                   |
|--------------|------------------------------------------------------------------------------|
| `presidio`   | Анализатор и анонимизатор PII (своя FastAPI-обёртка + кастомные распознаватели) |
| `omniroute`  | Умный роутер по ИИ-моделям (350+ провайдеров, quota-aware фолбэк, лимиты бесплатных задач) |
| `litellm`    | Прокси к провайдерам ИИ. При старте генерирует `litellm-proxy/config.yaml`     |
| `hermes-agent`| Агент Hermes (Nous Research), работает через LiteLLM как `custom`-провайдер     |

## Быстрый старт

1. **Заполните ключи** в `.env` (копия `.env.example`):

   ```bash
   cp .env.example .env
   # впишите свои ключи:
   # OPENROUTER_API_KEY, GEMINI_API_KEY, GROQ_API_KEY, MISTRAL_API_KEY
   # (строку OMNIROUTE_API_KEY можно оставить как есть — gateway стартует без ключей)
   ```

2. **Запуск одной командой** (поднимет весь стек, включая `hermes-agent`, и сразу
   откроет чат Hermes **с маршрутизацией через OmniRoute**):

   ```bash
   ./update_models_and_run.sh
   ```

   Классическая маршрутизация (`cloud-sanitized`, shuffle по прямым провайдерам):

   ```bash
   MODEL=cloud-sanitized ./update_models_and_run.sh
   ```

   Или по шагам (с провижинингом OmniRoute):

   ```bash
   docker compose up -d omniroute          # сначала только gateway
   ./provision_omniroute.sh                # ключи из .env + комбо cloud-auto
   docker compose up -d --build            # остальной стек
   docker exec -e CUSTOM_BASE_URL=http://litellm:4000/v1 -e CUSTOM_API_KEY=sk-dummy \
     hermes-agent hermes chat --provider custom -m cloud-sanitized-auto
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
4. Дополнительно всегда добавляется статичный маршрут `cloud-sanitized-auto`
   → OmniRoute (комбо `cloud-auto`) — он не зависит от облачных ключей и жив, даже
   если ни один из провайдеров не откликнулся.
5. Итог пишется в `litellm-proxy/config.yaml` — тот самый конфиг, который грузит прокси.

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

## Маршрутизация через OmniRoute

[OmniRoute](https://github.com/diegosouzapw/OmniRoute) — универсальный gateway
с одной OpenAI-совместимой точкой `/v1` и встроенным каталогом **350+ провайдеров**
(включая free-тиры). Контейнер `omniroute` поднимается в том же стеке и размещается
**между LiteLLM и провайдерами** — так как мимо неё, от клиентов, трафик не ходит,
ничего не минует Presidio.

```
Hermes / curl ──► LiteLLM (Presidio) ──► omniroute:20128 ──► провайдер
```

- Комбо `cloud-auto` (стратегия `auto`) создаётся автоматически при старте
  стека скриптом `provision_omniroute.sh`: него входят только бесплатные модели,
  а OmniRoute делает реалтайм-скоринг (здоровье, квота, латентность, цена) и
  фолбэчится между ними при лимитах.
- Провижининг (листинг выше п.4) подключает облачные ключи из `.env`
  (`OPENROUTER_/GEMINI_/GROQ_/MISTRAL_API_KEY`) в OmniRoute автоматически и
  идемпотентен (повторные запуски ничего не дублируют).
- Пароль управления OmniRoute задаётся через `INITIAL_PASSWORD` (из
  `OMNIROUTE_INITIAL_PASSWORD` в `.env`, по умолчанию `omniro2026!`) на первом
  старте контейнера; тем же паролем логинится `provision_omniroute.sh`. Сменили
  пароль в дашборде (**http://127.0.0.1:20128**) — обновите и `.env`.
- Ключи и комбо хранятся в томе `omniroute-data` (сохраняются между
  пересозданиями контейнера).
- Контекст окна: OmniRoute репортит комбо `cloud-auto` как 32K (контекст самой
  маленькой бесплатной модели), но Hermes-агенту нужно ≥64K. LiteLLM заявляет
  для маршрута `cloud-sanitized-auto` `max_input_tokens: 131072` (см.
  `generate_config.py`), а OmniRoute на запрос сам выбирает модель по
  вместимости контекста (context-fit).
- Запросы LiteLLM → OmniRoute ходят уже деидентифицированными (вызывается callback
  `presidio` из `litellm_settings`), поэтому PII до провайдеров не доходит.
- Ловим фолбэк и наоборот: OmniRoute умеет «выжимать» квоты бесплатных тиров
  и ретраить на другом провайдере, а если все квоты пусты — вернуть 429,
  который LiteLLM уже ретрает по `cloud-sanitized-auto` (или выберите `cloud-sanitized`
  для классического shuffle).
- Для ручных экспериментов у OmniRoute есть и алиасы на основе живого пула:
  `auto`, `auto/coding`, `auto/fast`, `auto/cheap`, `auto/offline` и т.п.
  (через `http://127.0.0.1:20128/v1` с ключом `OMNIROUTE_API_KEY`).

Пример запроса (тот же ключ, что у LiteLLM):

```bash
curl http://localhost:4000/v1/chat/completions \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer sk-dummy' \
  -d '{"model":"cloud-sanitized-auto","messages":[{"role":"user","content":"Мой email vasya@example.com, назови столицу Франции"}]}'
```

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
curl http://127.0.0.1:20128/v1/models     # omniroute (дашборд: http://127.0.0.1:20128)

# какие маршруты попали в конфиг
grep 'model:' litellm-proxy/config.yaml

# чат через прокси напрямую (PII обязательно маскируется)
curl http://localhost:4000/v1/chat/completions \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer sk-dummy' \
  -d '{"model":"cloud-sanitized","messages":[{"role":"user","content":"Мой email vasya@example.com, назови столицу Франции"}]}'

# то же, но с роутингом через OmniRoute
curl http://localhost:4000/v1/chat/completions \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer sk-dummy' \
  -d '{"model":"cloud-sanitized-auto","messages":[{"role":"user","content":"Мой email vasya@example.com, назови столицу Франции"}]}'
```

## Примечания

- В `.env` хранятся живые ключи — **не коммитьте его в git**.
- Некоторые бесплатные модели имеют общий пул лимитов и могут временно отдавать
  `429` — LiteLLM автоматически переключится на другого провайдера из списка.
- OmniRoute подписывается тегом `:latest` (следит за стабильными релизами).
  Данные gateway лежат в томе `omniroute-data` (`docker volume inspect` / бэкап через
  `docker run --rm -v omniroute-data:/app/data -v $PWD:/backup alpine cp -r /app/data /backup`).
- Кастомный callback `litellm-proxy/custom_presidio.py` не используется — работает
  встроенный callback `"presidio"`.
- Вспомогательные вызовы Hermes (заголовки сессий, сжатие контекста, извлечение
  с веба и т.п.) по умолчанию ходят на чужие эндпоинты (`gpt-4o-mini`,
  openrouter/nous) — их нет в этом стеке. Файл `hermes_config.yaml`
  (монтируется в контейнер как `/root/.hermes/config.yaml`) переводит текстовые
  aux-задачи на наш канал `cloud-sanitized-auto` (LiteLLM → Presidio → OmniRoute);
  мультимодальные задачи (vision/mcp) не трогаются.