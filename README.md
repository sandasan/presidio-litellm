# presidio-litellm

Docker-стек для запуска агента Hermes с безопасным доступом к бесплатным ИИ-моделям:

```
Hermes Agent ──► LiteLLM Proxy ──► Presidio (анонимизация PII) ──► OmniRoute ──► провайдер ИИ
Open WebUI ────►
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
| `open-webui` | Веб-чат в браузере (http://localhost:3000) поверх LiteLLM: тот же маршрут, та же анонимизация |

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
Hermes / Open WebUI / curl ──► LiteLLM (Presidio) ──► omniroute:20128 ──► провайдер
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

## Чат в браузере (Open WebUI)

Вместо консольного Hermes можно общаться с теми же моделями через веб-чат
**Open WebUI** — он поднимается в стеке автоматически.

- Адрес: **http://localhost:3000** (внутри сети контейнеры ходят по
  `http://open-webui:8080`).
- Подключение к LiteLLM уже настроено через `OPENAI_API_BASE_URL`
  (`http://litellm:4000/v1`) — все сообщения идут через тот же стек:
  Presidio-гардрейл маскирует PII до отправки провайдеру, а ответ
  де-анонимизируется к оригиналу (см. «Анонимизация»).
- **Первый вход / регистрация**: откройте http://localhost:3000 — при первом
  запуске Open WebUI покажет форму **Sign Up** (регистрация). На неё:

  1. **Email** — любой на вид валидный, например `admin@example.com`.
     Обратите внимание: Open WebUI отклоняет непохожие на настоящий e-mail
     адреса (например `admin@local` вернёт ошибку про формат).
     Письма никуда не отправляются, почта не проверяется.
  2. **Name** — имя, отображаемое в интерфейсе (например, `admin`).
  3. **Password** — придумайте пароль (и **Confirm Password** — повторите его).

  После создания аккаунт **автоматически становится админом** (первый
  пользователь всегда админ). Логин и пароль хранятся локально в томе
  `open-webui-data`, в облако не уходят. На следующих запусках входите через
  **Sign In** (страница логина) — форма регистрации больше показываться не будет.
- В выпадашке модели выберите **`cloud-sanitized-auto`** — модели подтягиваются
  из `/v1/models` LiteLLM.
- История чатов и настройки лежат в томе `open-webui-data` (переживают
  `docker compose up` / пересоздание контейнера).

Важно: Open WebUI — это **чат-интерфейс, а не агент Hermes**. У него нет
агентского цикла и инструментов Hermes (работа с файлами, шелл, сессии);
это просто общение с моделями маршрута `cloud-sanitized-auto` через наш стек.

## Hermes в VSCode

Использовать нашего агента Hermes для помощи в программировании из VSCode
можно тремя способами.

### 1. Встроенный терминал VSCode (работает сразу)

Контейнер `hermes-agent` монтирует `/home/alexander/projects` (ваши проекты)
в `/workspace` и уже настроен на маршрут `cloud-sanitized-auto` через LiteLLM
(поэтому анонимизация и де-анонимизация работают как в консоли). Откройте
терминал в VSCode и запустите:

```bash
docker exec -it hermes-agent hermes chat --provider custom -m cloud-sanitized-auto
```

- Интерактивный чат с полным агентским циклом (Hermes сам читает и правит файлы
  в `/workspace`).
- Сессии: `--continue` возобновляет последнюю, `-r <session_id>` — конкретную.
- Разовые задачи без интерактива:
  ```bash
  docker exec hermes-agent hermes -z "Задача..."
  ```
  или `hermes chat -q "Задача..."`.

### 2. Полноценная редакторная интеграция — ACP (VS Code / Zed / JetBrains)

У Hermes есть нативный режим для редакторов — **Agent Client Protocol**
(`hermes acp --help` → «editor integration (VS Code, Zed, JetBrains)»). Он
даёт работу как у IDE-ассистента: видение файлов, diff, применение правок.

⚠️ В нашем образе ACP-зависимости пока не установлены — проверить:
`docker exec hermes-agent hermes acp --check`. Чтобы включить:

1. В `Dockerfile.hermes` замените установку Hermes на
   `pip install --no-cache-dir 'hermes-agent[acp]'`.
2. Пересоберите образ и пересоздайте контейнер:
   ```bash
   docker compose up -d --build hermes-agent
   ```
3. В VSCode установите расширение с поддержкой ACP (например, расширение
   Claude Code или Continue) и укажите запуск агента через
   `docker exec -i hermes-agent hermes acp`.

После этого Hermes работает как ассистент прямо в редакторе поверх того же
анонимизированного маршрута.

### 3. Копайлот-расширения на маршрут LiteLLM (не Hermes)

Любое OpenAI-совместимое расширение **Cline / Roo Code / Continue** можно
подключить напрямую к прокси:

- Base URL: `http://localhost:4000/v1`
- Model: `cloud-sanitized-auto`
- API key: `sk-dummy`

Подсказки и правки в редакторе пойдут через тот же стек (Presidio +
де-анонимизация), но это **не** агент Hermes — агентского цикла, сессий и
рабочих инструментов у этого варианта нет.

## Полезные проверки

```bash
# здоровье сервисов
curl http://localhost:5001/health          # presidio
curl http://localhost:4000/health/liveliness  # litellm
curl http://127.0.0.1:20128/v1/models     # omniroute (дашборд: http://127.0.0.1:20128)
curl http://localhost:3000                # open-webui (чат)

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

## Как собрать такой проект с нуля

Пошаговая сборка стека «Hermes + Presidio + LiteLLM + OmniRoute + Open WebUI».
Здесь описано, из каких частей состоит репозиторий и в каком порядке они
создаются. Итоговая структура:

```
.
├── .env.example                 # шаблон ключей (копировать в .env)
├── docker-compose.yml           # сервисы: presidio, omniroute, litellm, hermes-agent, open-webui
├── update_models_and_run.sh     # запуск всего стека одной командой
├── provision_omniroute.sh       # идемпотентный провижининг OmniRoute
├── hermes_config.yaml           # Hermes: модель + aux-задачи через LiteLLM
├── presidio_config.yaml         # кастомные распознаватели PII
├── presidio-server/             # FastAPI-обёртка над Presidio
│   ├── Dockerfile
│   └── app.py                   # /analyze, /anonymize, /health
├── litellm-proxy/
│   ├── generate_config.py       # пишет config.yaml (маршрут + guardrails presidio)
│   ├── custom_callbacks.py      # clamp max_tokens
│   └── custom_presidio.py       # не используется (работает встроенный гардрейл)
├── Dockerfile.hermes            # образ агента Hermes
└── README.md
```

### 1. Каркас, ключи и `.gitignore`

1. Создайте каталог проекта и `.env.example`:
   ```bash
   OPENROUTER_API_KEY=   GEMINI_API_KEY=   GROQ_API_KEY=   MISTRAL_API_KEY=
   OMNIROUTE_API_KEY=sk-omniroute
   OMNIROUTE_INITIAL_PASSWORD=omniro2026!
   OMNIROUTE_MEMORY_MB=1024
   ```
   Скопируйте в `.env` и впишите свои ключи. В `.gitignore` обязательно
   исключите `.env`, `__pycache__/`, `.idea/`, `.vscode/` и сгенерированный
   `litellm-proxy/config.yaml`.

### 2. Presidio-сервер

- `presidio-server/Dockerfile`: образ Python, ставит `presidio-analyzer`,
  `presidio-anonymizer`, `spacy`, `fastapi`, `uvicorn`, запекает модель
  `en_core_web_lg`, запускает `uvicorn app:app --port 5001`.
- `presidio-server/app.py`: FastAPI-обёртка — `POST /analyze`, `POST /anonymize`,
  `GET /health`.
- `presidio_config.yaml`: кастомные распознаватели поверх стандартных сущностей
  Presidio — `SECRET_KEY` (API-ключи/токены), `DB_CONNECTION` (строки
  подключения), `INTERNAL_IP` (приватные IPv4). Лимит: у каждого
  распознавателя обязательно `supported_entity`, иначе Analyzer их не отдаст.

### 3. OmniRoute и провижининг

- Сервис `omniroute` в `docker-compose.yml`: образ `diegosouzapw/omniroute:latest`,
  порт `127.0.0.1:20128`, том `omniroute-data`, env:
  - `INITIAL_PASSWORD` — пароль bootstrap (первый старт фиксирует его);
  - `OMNIROUTE_API_KEY` — passthrough-ключ для `LiteLLM → OmniRoute`;
  - `{PROVIDER_ID}_API_KEY` — облачные ключи (`OPENROUTER_`, `GEMINI_`, ...);
  - `OMNIROUTE_MEMORY_MB` — размер V8-кучи.
- `provision_omniroute.sh`: идемпотентно логинится в management API, подключает
  провайдеров openrouter/gemini/groq/mistral из `.env` (когда коннекшна ещё нет)
  и создаёт комбо `cloud-auto` (стратегия `auto`, только бесплатные модели).
  Повторный запуск безопасен (пропускает уже созданное).

### 4. LiteLLM-прокси и анонимизация

- Каталог `litellm-proxy/`: `generate_config.py` при старте контейнера пишет
  `config.yaml` с **одним** маршрутом `cloud-sanitized-auto` →
  `openai/cloud-auto` @ `http://omniroute:20128/v1`.
- В `config.yaml` также секция `guardrails` — встроенный Presidio-гардрейл:
  ```yaml
  guardrails:
    - guardrail_name: presidio-anonymizer
      litellm_params:
        guardrail: presidio
        mode: pre_call
        default_on: true
        output_parse_pii: true
        presidio_filter_scope: input
  ```
  `output_parse_pii: true` + `presidio_filter_scope: input` дают маскирование
  запроса нумерованными токенами (`<EMAIL_ADDRESS_1>`) и де-анонимизацию ответа
  (см. «Анонимизация»). Base URL'ы Presidio подхватываются из env
  (`PRESIDIO_ANALYZER_API_BASE` / `PRESIDIO_ANONYMIZER_API_BASE`).
- `litellm-proxy/custom_callbacks.py`: кастомный коллбек `MaxTokensClamp` —
  режет исходящий `max_tokens` до `MAX_OUTPUT_TOKENS`, чтобы запросы агента
  вписывались в лимиты бесплатных моделей.

### 5. Агент Hermes

- `Dockerfile.hermes`: Python 3.11-slim, venv, `pip install hermes-agent`.
- Сервис монтирует `/home/alexander/projects` в `/workspace` и подключается к
  LiteLLM как `custom`-провайдер (`CUSTOM_BASE_URL=http://litellm:4000/v1`,
  `CUSTOM_API_KEY=sk-dummy`).
- `hermes_config.yaml` переводит aux-задачи Hermes (заголовки сессий, сжатие
  контекста, веб-экстракция, skills_hub, approval) на наш канал —
  `model: cloud-sanitized-auto`, `provider: custom`,
  `base_url: http://litellm:4000/v1`.

### 6. Чат-интерфейс Open WebUI

Сервис `open-webui` (`ghcr.io/open-webui/open-webui:main`), порт `3000:8080`,
том `open-webui-data`, `OPENAI_API_BASE_URL=http://litellm:4000/v1` —
весь чат-трафик идёт через те же анонимизацию и маршрут (см. «Чат в
браузере»).

### 7. `docker-compose.yml` и запуск

- Порядок зависимостей: `litellm` ждёт `presidio` и `omniroute` healthy;
  `hermes-agent` и `open-webui` зависят от `litellm`.
- `update_models_and_run.sh` выполняет порядок: `omniroute` отдельно →
  прогрев → `provision_omniroute.sh` → весь стек → готовность LiteLLM → чат
  Hermes. Запуск одной командой:
  ```bash
  ./update_models_and_run.sh
  ```