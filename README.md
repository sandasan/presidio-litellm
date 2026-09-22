# presidio-litellm

Docker-стек для запуска агента Hermes с безопасным доступом к бесплатным ИИ-моделям:

```
Hermes Agent ──► LiteLLM Proxy ──► Presidio (анонимизация PII) ──► OmniRoute ──► провайдер ИИ
```

Все исходящие запросы к облачным моделям проходят через **Presidio**: персональные данные
(email, имена, телефоны, API-ключи, строки подключения к БД, внутренние IP) маскируются
до отправки на сторону провайдера.

Маршрутизация — единая, через OmniRoute:

| Маршрут               | Как работает                                                                  |
|-----------------------|-------------------------------------------------------------------------------|
| `cloud-sanitized-auto`| LiteLLM → OmniRoute: тот выбирает целевую модель сам и сам фолбэчится между квотами |

## Состав

| Сервис       | Назначение                                                                   |
|--------------|------------------------------------------------------------------------------|
| `presidio`   | Анализатор и анонимизатор PII (своя FastAPI-обёртка + кастомные распознаватели) |
| `omniroute`  | Единый роутер по ИИ-моделям (350+ провайдеров, quota-aware фолбэк, лимиты бесплатных задач) |
| `litellm`    | Прокси к OmniRoute. Применяет анонимизацию Presidio; при старте генерирует `litellm-proxy/config.yaml` |
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
   откроет чат Hermes с маршрутизацией через OmniRoute):

   ```bash
   ./update_models_and_run.sh
   ```

   Или по шагам (с провижинингом OmniRoute):

   ```bash
   docker compose up -d omniroute          # сначала только gateway
   ./provision_omniroute.sh                # ключи из .env + комбо cloud-auto
   docker compose up -d --build            # остальной стек
   docker exec -e CUSTOM_BASE_URL=http://litellm:4000/v1 -e CUSTOM_API_KEY=sk-dummy \
     hermes-agent hermes chat --provider custom -m cloud-sanitized-auto
   ```

## Как устроен маршрут

При каждом старте контейнера `litellm` выполняется `litellm-proxy/generate_config.py`,
который пишет в `litellm-proxy/config.yaml` один статичный маршрут
`cloud-sanitized-auto` → OmniRoute (комбо `cloud-auto`). Модели выбирает OmniRoute:

1. Провижининг `provision_omniroute.sh` подключает в OmniRoute провайдеров
   **OpenRouter**, **Gemini**, **Groq**, **Mistral** из `.env-ключей` (или OmniRoute
   сам регистрирует их по env-паттерну `{PROVIDER_ID}_API_KEY`) и создаёт комбо
   `cloud-auto`.
2. В комбо входят только бесплатные модели; OmniRoute реалтайм-скорит их
   (здоровье, квота, латентность, цена) и выбирает целевую модель на запрос.
3. LiteLLM применяет к запросу анонимизацию Presidio и передаёт его в OmniRoute —
   **все** исходящие запросы идут через этот единственный путь, PII до
   провайдеров не доходит.

Генератор можно запустить и вручную, чтобы переписать конфиг без перезапуска стека:

```bash
python3 litellm-proxy/generate_config.py
```

## Фолбэк при лимитах (429 / квоты)

Вся квота-aware логика живёт в OmniRoute (комбо `cloud-auto`):

- при ошибке запроса (429/5xx/timeout) OmniRoute сам переключается на другой
  live-провайдер из комбо и ретраит, выжимая квоты бесплатных тиров;
- если **все** бесплатные модели одновременно исчерпали лимиты — OmniRoute вернёт
  `429`, а LiteLLM делает скромный ретрай по тому же маршруту (`num_retries: 2`,
  `cooldown_time: 60`) в расчёте на освобождение квоты;
- контекст-максимум подбирается тоже в OmniRoute (context-fit): большие контексты
  уводит на модели с достаточным окном (gemini-flash и т.п.).

Прямой shuffle в LiteLLM между провайдерами убран как избыточный: он дублировал
роутинг OmniRoute, а лишние облачные ключи больше не передаются контейнеру LiteLLM.

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
  стека скриптом `provision_omniroute.sh`: в него входят только бесплатные модели,
  а OmniRoute делает реалтайм-скоринг (здоровье, квота, латентность, цена) и
  фолбэчится между ними при лимитах.
- Провижининг подключает облачные ключи из `.env`
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
- Запросы LiteLLM → OmniRoute ходят уже деидентифицированными (гардрейл
  `presidio` из секции `guardrails`), поэтому PII до провайдеров не доходит.
- Ловим фолбэк и наоборот: OmniRoute умеет «выжимать» квоты бесплатных тиров
  и ретраить на другом провайдере, а если все квоты пусты — вернуть 429,
  который LiteLLM уже доретрает по тому же маршруту `cloud-sanitized-auto`.
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
- LiteLLM подключает Presidio через гардрейл `guardrails` в `config.yaml`
  (`guardrail: presidio`, `default_on: true`) и env-переменные
  `PRESIDIO_ANALYZER_API_BASE` / `PRESIDIO_ANONYMIZER_API_BASE` — срабатывает на
  **все** запросы, в т.ч. исходящие к OmniRoute.
- **Де-анонимизация ответов**: включена через `output_parse_pii: true` +
  `presidio_filter_scope: input`. Входящий запрос маскируется нумерованными
  токенами (`<EMAIL_ADDRESS_1>`, `<PERSON_1>` и т.п.), а ответ модели (включая
  аргументы tool-call'ов) восстанавливается к оригинальным значениям, чтобы
  Hermes и пользователь локально видели реальные данные. В облако при этом
  уходят только заглушки. Маскируется только исходящее (`presidio_filter_scope:
  input`) — ответ назад не пере-маскируется.
- Проверка: `curl http://localhost:5001/health` → `{"status":"ok"}`.

## Полезные проверки

```bash
# здоровье сервисов
curl http://localhost:5001/health          # presidio
curl http://localhost:4000/health/liveliness  # litellm
curl http://127.0.0.1:20128/v1/models     # omniroute (дашборд: http://127.0.0.1:20128)

# какие маршруты попали в конфиг
grep 'model:' litellm-proxy/config.yaml

# чат через прокси (PII обязательно маскируется)
curl http://localhost:4000/v1/chat/completions \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer sk-dummy' \
  -d '{"model":"cloud-sanitized-auto","messages":[{"role":"user","content":"Мой email vasya@example.com, назови столицу Франции"}]}'
```

## Примечания

- В `.env` хранятся живые ключи — **не коммитьте его в git**.
- Некоторые бесплатные модели имеют общий пул лимитов и могут временно отдавать
  `429` — OmniRoute переключится на другого провайдера из комбо, а LiteLLM
  доретрает по `cloud-sanitized-auto`.
- OmniRoute подписывается тегом `:latest` (следит за стабильными релизами).
  Данные gateway лежат в томе `omniroute-data` (`docker volume inspect` / бэкап через
  `docker run --rm -v omniroute-data:/app/data -v $PWD:/backup alpine cp -r /app/data /backup`).
- Кастомный callback `litellm-proxy/custom_presidio.py` не используется — работает
  встроенный Presidio-гардрейл (`guardrail: presidio`).
- Вспомогательные вызовы Hermes (заголовки сессий, сжатие контекста, извлечение
  с веба и т.п.) по умолчанию ходят на чужие эндпоинты (`gpt-4o-mini`,
  openrouter/nous) — их нет в этом стеке. Файл `hermes_config.yaml`
  (монтируется в контейнер как `/root/.hermes/config.yaml`) переводит текстовые
  aux-задачи на наш канал `cloud-sanitized-auto` (LiteLLM → Presidio → OmniRoute);
  мультимодальные задачи (vision/mcp) не трогаются.