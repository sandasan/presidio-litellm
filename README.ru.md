# presidio-litellm

[Русский](README.ru.md) | [Українська](README.uk.md) | [English](README.en.md)

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
     hermes-agent hermes-chat chat --provider custom -m cloud-sanitized-auto
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
  `presidio_config.yaml` поверх стандартных сущностей
  (`PERSON`, `EMAIL_ADDRESS`, `PHONE_NUMBER`, `CREDIT_CARD` и т.д.):
  - `SECRET_KEY` — API-ключи/токены (`sk-*`, `AIza*`, `xai-*`, `gsk_*`,
    Bearer, `PRIVATE KEY`), JWT, `ghp_*/github_pat_*`, `npm_*`, `xox*` (Slack),
    `sk_/pk_` (Stripe), `AKIA` (AWS), telegram, basic-auth в URL;
  - `DB_CONNECTION` — строки подключения (postgres/mysql/mongodb/redis/…,
    а также amqp/kafka/nats/clickhouse/snowflake/presto/trino);
  - `INTERNAL_IP` — приватные IPv4 (`10.x`, `172.16–31.x`, `192.168.x`),
    CGNAT `100.64–127.x`, loopback, link-local, приватные IPv6 (`fd00::/8`,
    `fe80::`); 
  - `INTERNAL_HOST` — внутренние домены и URL: `.internal`, `.corp`, `.local`,
    `.lan`, `.intranet`, `.home`, `.docker`;
  - `INTERNAL_PATH` — внутренние пути: `file://`, `smb://`, `nfs://`,
    `cifs://`, `storage://` и UNC (`\\server\share`).
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
- **Литеральные секреты** (`custom_callbacks.py`, класс `LiteralSecretMasker`):
  точные значения, которые Presidio не распознаёт как PII-паттерн (внутренние
  имена сервисов, кодовые слова, хвосты ключей), можно занести в
  `litellm-proxy/secrets_map.json` — они будут детерминированно заменяться на
  `<PLACEHOLDER>` до отправки провайдеру и восстанавливаться в ответе, как и
  токены Presidio. Подробнее в «Литеральные секреты».
- Проверка: `curl http://localhost:5001/health` → `{"status":"ok"}`.

## Границы защиты

Важно честно понимать, что именно даёт этот стек. Анонимизация — это
**снижение риска по распознаваемым PII**, а не общая конфиденциальность.

**Что защищено надёжно**

- Распознаваемые **PII**: email, имена, телефоны, номера карт.
- **Секреты из наших кастомных распознавателей** (`presidio_config.yaml`):
  API-ключи и токены (`sk-*`, `AIza*`, Bearer, `PRIVATE KEY`), строки
  подключения к БД, приватные IPv4 (`10.x`, `172.16–31.x`, `192.168.x`).
- **Литеральные секреты** из `litellm-proxy/secrets_map.json`: точные значения,
  не поддающиеся паттерну (имена сервисов, кодовые слова и т.п.) — заменяются
  на `<PLACEHOLDER>` до отправки провайдеру (см. «Литеральные секреты»).
- Всё исходящее из контейнера агента **заперто**: egress lock закрывает
  любые внешние HTTP(S)-запросы Hermes (см. «Защита контура агента»), так что
  данных при prompt-инъекции наружу не уйдёт.
- Файловая видимость агента **контролируется грантами**: без разрешения он
  не читает чужие каталоги, а внутри разрешённого каталога — файлы из
  `.gitignore`/`.aiignore` и "подобных" (см. «Разрешения на каталоги»).
- Весь трафик идёт единственным путём → мимо анонимизации ничего не уходит;
  OmniRoute и провайдер получают **уже замаскированный** текст,
  де-анонимизация (таблица соответствий) живёт локально в памяти LiteLLM.
- Проверено практикой (см. «Полезные проверки»): наружу уходит
  `<EMAIL_ADDRESS_1>` вместо email, ответ восстанавливается.

**Что НЕ защищено (границы)**

| Риск | Что происходит |
|------|----------------|
| Не-PII конфиденциальные данные | Анонимизация распознаёт **шаблоны**, а не семантику. Код, архитектура, бизнес-логика, внутренние названия — уходят провайдеру в открытом виде (если не занесены в `secrets_map.json`) |
| Облачные провайдеры | Модели могут быть «учителем» или хранить запросы по своим политикам; это не внутри нашего контроля. Стек предпочитает no-training провайдеров (см. «Провайдерская приватность»), но это заявленные ими политики, а не наша гарантия |
| Vision/MCP-подзадачи Hermes | Мультимодальные задачи (vision/mcp) не переведены на наш канал (см. «Примечания»); egress lock отключает обращение к дефолтным эндпоинтам openrouter/nous |
| Метаданные | Внешний IP хоста, тайминги и размеры запросов провайдеру видны в любом случае |
| Симлинки внутри гранта | `filegate` пускает реально существующие пути и не «разглядывает» симлинки, ведущие за пределы гранта вне `/workspace` (в системные каталоги). Бэкап разрешённой директории не должен содержать ссылок на данные вне неё |
| Открытый FTP и прочие не-HTTP | Egress lock закрывает HTTP(S)-соединения; трафик иных протоколов контейнеру не нужен, но отдельным firewall-аппетайдом он не перекрыт |

**Как этим пользоваться**: стек делает комфортной работу с персональными
данными и секретами (маскируются до отправки). Произвольные проприетарные
данные (закрытый код под NDA, продуктовые тайны) облаку отдавать не стоит —
для них нужна локальная модель или осознанно доверенный провайдер.

## Защита контура агента

Три дополнения поверх анонимизации, которые закрывают основные дыры из
«Границы защиты»: секреты-литералы, исход трафика из контейнера и файловая
видимость агента.

### Литеральные секреты

Presidio распознаёт только **паттерны**. Если в коде встречается внутреннее
имя сервиса, кодовое слово или хвост ключа, которые шаблоном не поймать, —
занесите их в `litellm-proxy/secrets_map.json` (рядом лежит шаблон
`secrets_map.example.json`):

```json
{
  "MY_INTERNAL_SERVICE_NAME": "некое-внутреннее-имя-сервиса",
  "MY_PROD_DB_PASSWORD": "hunter2"
}
```

Файл исключён из git (см. `.gitignore`) — не коммитьте его. После изменения
перезапустите `litellm`: `docker compose up -d --force-recreate litellm`.

Как работает: callback `LiteralSecretMasker` в `litellm-proxy/custom_callbacks.py`
в `async_pre_call_hook` рекурсивно заменяет **точные вхождения** значений на
`<PLACEHOLDER>` во всех строках запроса (system/user/tool, вложенные поля), а
`async_post_call_success_hook` и `async_post_call_streaming_iterator_hook`
восстанавливают оригинал в ответе — и в обычном, и в стриминговом режиме.
Провайдер видит только `<NAME>`, клиент — исходное значение. Если файла нет —
маскер просто выключен, стек работает как раньше.

⚠️ Подставляйте только уникальные **высокоэнтропийные** литералы (например
`BananaIceCream80324Ops` или имя сервиса передавайте вместе с уникальным
суффиксом, который в тексте не встречается). Короткие и частые строки (имена,
слова) будут заменяться повсюду и ломать качество чата.

Проверка: из чата попросите модель дословно повторить литерал — в ответе
вернётся оригинал, а в reasoning модели будет виден только `<PLACEHOLDER>`.

### Запирание исходящего трафика (egress lock)

Контейнеру `hermes-agent` заданы прокси-переменные (см. `docker-compose.yml`):

```
HTTP_PROXY / HTTPS_PROXY (и http_proxy / https_proxy) = http://127.0.0.1:65533
NO_PROXY / no_proxy = litellm,presidio,omniroute,open-webui,localhost,127.0.0.1,.local
```

Все HTTP(S)-запросы, кроме внутренних сервисов стека, уходят на
несуществующий порт и **молча падают**. Осознанные последствия:

- LLM-трафик агента (к `litellm`) не затронут — идёт напрямую по `NO_PROXY`;
- веб-сёрфинг агента (задача `web_extract`, прямые `curl` из bash) больше не
  работает;
- vision/MCP aux-вызовы на чужие эндпоинты (openrouter/nous) — закрыты;
- при prompt-инъекции агент физически не может достучаться наружу из контейнера.

Проверка:

```bash
docker exec hermes-agent curl -m 5 -sS -o /dev/null -w '%{http_code}\n' http://litellm:4000/health/liveliness  # 200
docker exec hermes-agent curl -m 5 -sS -o /dev/null https://example.com 2>&1            # connect refused
```

### Разрешения на каталоги (гранты + ignore-файлы)

Контейнер монтирует каталог из `PROJECTS_DIR` (по умолчанию
`/home/alexander/projects`) в `/workspace` целиком, но агент видит только то,
на что получил **грант**. Запускайте агента через обёртку
(не напрямую `hermes`):

```bash
docker exec -it hermes-agent hermes-chat chat --provider custom -m cloud-sanitized-auto
```

> Важно: если запускать Hermes без явно заданного `HERMES_GRANTS`, обёртка
> `hermes-chat` в интерактивном TTY может застрять на выборе каталога. Для
> интерактивного чата используйте `docker exec -it` вместе с `HERMES_GRANTS`;
> для автоматических команд, где stdin не является TTY, используйте
> неинтерактивный режим или явно задайте грант через env.

При первом запуске обёртка покажет список каталогов в `/workspace` и спросит,
каким разрешить работать (номера через запятую, `all` или `none`). Выбор
сохраняется в `hermes_grants.json` (монтируется в контейнер как
`/root/.hermes/grants.json`) и применяется при следующих запусках. Для
неинтерактивного сценария — `HERMES_GRANTS=proj1,proj2`:

```bash
docker exec -e HERMES_GRANTS=presidio-litellm hermes-agent hermes-chat chat --provider custom -m cloud-sanitized-auto
```

#### Запуск для нужного проекта

Проект должен находиться внутри каталога, который монтируется в контейнер
(`PROJECTS_DIR` на хосте, по умолчанию `/home/alexander/projects`). Например, для проекта
`/home/alexander/projects/my-app`:

```bash
# из корня этого репозитория
./update_models_and_run.sh

# в другом терминале: дать агенту доступ только к my-app
docker exec -it \
  -e HERMES_GRANTS=my-app \
  hermes-agent hermes-chat chat --provider custom -m cloud-sanitized-auto
```

Значение `HERMES_GRANTS` — это имя папки непосредственно внутри
`/home/alexander/projects`, а внутри контейнера она доступна как
`/workspace/my-app`. Для нескольких проектов укажите имена через запятую,
например `HERMES_GRANTS=my-app,another-app`. Если проект находится в другом
месте, добавьте его в секцию `volumes` сервиса `hermes-agent` в
`docker-compose.yml` и используйте соответствующее имя каталога в гранте.

Для проекта в любом месте можно смонтировать непосредственно его корень:

```bash
./hermes-acp.sh "$(basename "$PWD")" "$PWD"
```

В этом режиме проект доступен как `/workspace`, grant равен `.`, а при
переключении на другую папку пересоздаётся только контейнер Hermes. В задаче
VS Code этот режим уже используется автоматически через `${workspaceFolder}`.
Для обычного чата задайте путь в `.env` как `PROJECTS_DIR=/полный/путь/к/проекту`
и используйте `HERMES_GRANTS=.`.

Что делает `hermes-chat`:

1. Собирает список **запрещённых путей** на старте (`hermes-filegate.py`):
   - все каталоги `/workspace`, кроме грантованных;
   - внутри каждого гранта — правила из `.gitignore`, `.aiignore`,
     `.cursorignore`, `.ignore`, `.npmignore`, `.dockerignore`,
     `.git/info/exclude` плюс разумный дефолт (`node_modules/`,
     `__pycache__/`, виртуальные окружения, `.env*`).
2. Включает **LD_PRELOAD-перехватчик** `filegate.so` (компилируется в
   `Dockerfile.hermes`): `open`/`openat` для запрещённых путей возвращают
   `Permission denied`, кем бы ни шло чтение (cat, редактор, python, node).
   Закрыты и целые подкаталоги (созданное внутри них позже тоже не прочитать),
   и выход через симлинк в другой проект.
3. Стартует `hermes` из корня гранта (или `/workspace`, если грантов несколько).

Проверка:

```bash
docker exec -e HERMES_GRANTS=presidio-litellm hermes-agent hermes-chat --version
# внутри гранта .env читается только как Permission denied, README.ru.md доступен,
# файлы других проектов — Permission denied
```

## Провайдерская приватность (no-training)

Маскирование PII — наша работа, но **политика хранения и обучения** — работа
провайдера. Стек сознательно отдаёт предпочтение провайдерам, которые
**не используют запросы API для обучения**:

- **Mistral** — публичная политика no-training на данных API (самый
  надёжный для конфиденциальных запросов случай).
- **Gemini** и **Groq** — данные API-сервисов по умолчанию не идут в обучение
  моделей (это их официальные условия использования; условия могут меняться —
  сверяйтесь с актуальными).
- **OpenRouter** — это не модель, а роутер поверх апстримов: его провайдеры и
  они сами могут обучаться на трафике, пока в настройках дашборда не выключен
  флаг **Allow training**. В комбо `cloud-auto` модели OpenRouter имеют
  минимальный вес и выбираются не в первую очередь.

Что сделано в стеке:

- `provision_omniroute.sh` создаёт/обновляет комбо `cloud-auto` с весами
  приоритета: `mistral` 5, `gemini` 4, `groq` 3, `openrouter` 1. Порядок
  применяется идемпотентно при каждом запуске (в т.ч. по весам — через PUT к
  management API), так что уже существующее комбо тоже получит новые веса.
- Переключите OpenRouter в режим без обучения вручную: на
  [openrouter.ai/settings/keys](https://openrouter.ai/settings/keys) либо в
  настройках использования/модели выключите **Allow training** для используемых
  моделей (если такой флаг доступен). Это влияет на то, как трактуются
  апстримы OpenRouter, и дополняет наш весовой приоритет.

Гарантии остаются **заявлениями провайдеров**, а не нашего кода: стек
снижает вероятность попадания данных к «более охотно учащимся» провайдерам,
но полная уверенность для строгих требований NDA — локальная модель
или провайдер с явным enterprise-контрактом (см. «Границы защиты»).

Проверка (веса комбо):

```bash
JAR=$(mktemp)
curl -sS -c "$JAR" -X POST http://127.0.0.1:20128/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"password":"<OMNIROUTE_INITIAL_PASSWORD>"}' >/dev/null
curl -sS -b "$JAR" http://127.0.0.1:20128/api/combos | python3 -c "
import sys, json
c = next(x for x in json.load(sys.stdin)['combos'] if x['name'] == 'cloud-auto')
print(*[(m['providerId'], m['weight']) for m in c['models']], sep='\n')"
rm -f "$JAR"
```

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
docker exec -it hermes-agent hermes-chat chat --provider custom -m cloud-sanitized-auto
```

- Интерактивный чат с полным агентским циклом (Hermes сам читает и правит файлы
  в `/workspace`). При первом запуске спросит, каким каталогам разрешить доступ
  (гранты, см. «Разрешения на каталоги»).
- Сессии: `--continue` возобновляет последнюю, `-r <session_id>` — конкретную.
- Разовые задачи без интерактива (грант задаётся явно, иначе каталоги закрыты):
  ```bash
  docker exec -e HERMES_GRANTS=presidio-litellm hermes-agent hermes-chat -z "Задача..."
  ```
  или `hermes-chat chat -q "Задача..."`.

### 2. Полноценная редакторная интеграция — ACP (VS Code / Zed / JetBrains)

У Hermes есть нативный режим для редакторов — **Agent Client Protocol**
(`hermes acp --help` → «editor integration (VS Code, Zed, JetBrains)»). Он
даёт работу как у IDE-ассистента: видение файлов, diff, применение правок.

Самый короткий рабочий вариант для этого проекта:

```bash
./hermes-acp.sh
```

Файл `hermes-acp.sh` уже содержит корректную команду запуска:

```bash
exec docker exec -it \
  -e HERMES_GRANTS="$GRANT" \
  -e HERMES_ACCEPT_HOOKS=1 \
  -e CUSTOM_BASE_URL=http://litellm:4000/v1 \
  -e CUSTOM_API_KEY=sk-dummy \
  hermes-agent hermes acp --accept-hooks
```

Или вручную:

```bash
docker exec -it \
  -e HERMES_GRANTS=presidio-litellm \
  -e HERMES_ACCEPT_HOOKS=1 \
  -e CUSTOM_BASE_URL=http://litellm:4000/v1 \
  -e CUSTOM_API_KEY=sk-dummy \
  hermes-agent hermes acp --accept-hooks
```

⚠️ В нашем образе ACP-зависимости нужно установить явно. Проверка:
`docker exec hermes-agent hermes acp --check`.

Чтобы включить полноценную редакторную интеграцию:

1. В `Dockerfile.hermes` установите ACP-опции Hermes:
   ```dockerfile
   RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
       pip install --no-cache-dir 'hermes-agent[acp]'
   ```
2. Пересоберите образ и пересоздайте контейнер:
   ```bash
   docker compose up -d --build hermes-agent
   ```
3. Проверьте, что ACP готов:
   ```bash
   docker exec hermes-agent hermes acp --check
   ```
4. В VSCode установите расширение с поддержкой ACP (например, Continue или
   совместимый клиент для Agent Client Protocol) и задайте команду запуска
   агента как:
   ```bash
   docker exec -i hermes-agent hermes acp
   ```

Если расширение поддерживает режим "agent/server" или "stdio", используйте
точно эту команду в качестве входа для локального ACP-сервера Hermes.

Для этого репозитория уже добавлены готовые файлы запуска:

- `hermes-acp.sh` — обёртка над `docker exec ... hermes acp --accept-hooks`
- `.vscode/tasks.json` — задача VS Code `Hermes ACP`

Запуск из VS Code:

```bash
# в терминале VS Code
./hermes-acp.sh "$(basename "$PWD")"
# или Terminal -> Run Task -> Hermes ACP
```

Задача `Hermes ACP` в `.vscode/tasks.json` передаёт имя текущей открытой папки
через `${workspaceFolderBasename}`. Перед запуском ACP она проверяет
`http://localhost:4000/health/liveliness`; если стек не запущен, выполняет
`docker compose up -d` и ждёт готовности LiteLLM. Поэтому задачу можно запускать
из открытого проекта, если его папка находится внутри `PROJECTS_DIR`.

Внутри контейнера используется тот же защищённый маршрут:
`HERMES_GRANTS=<project-name>` + `CUSTOM_BASE_URL=http://litellm:4000/v1` +
`CUSTOM_API_KEY=sk-dummy`.

После этого Hermes работает как ассистент прямо в редакторе поверх того же
анонимизированного маршрута.

#### Краткая памятка: как подключить Hermes ACP в VS Code

1. Убедитесь, что стек поднят:
   ```bash
   ./update_models_and_run.sh
   ```
2. Запустите один из вариантов:
   ```bash
   ./hermes-acp.sh
   ```
   или через VS Code task: `Terminal → Run Task → Hermes ACP`.
3. В клиенте ACP выберите сервер со стандартным вводом/выводом или командой:
   ```bash
   docker exec -i hermes-agent hermes acp
   ```
   Если используется режим с принятием хуков, запускайте обёртку
   `./hermes-acp.sh`, которая уже включает `--accept-hooks`.
4. Важно: агент должен видеть только нужный проект через `HERMES_GRANTS`.
   Для этого репозитория достаточно:
   ```bash
   export HERMES_GRANTS=presidio-litellm
   ```
5. Проверка готовности:
   ```bash
   docker exec hermes-agent hermes acp --check
   ```

Этот режим использует тот же защищённый маршрут: `cloud-sanitized-auto` →
LiteLLM → Presidio → OmniRoute. Все запросы идут через анонимизацию PII до
отправки на провайдер.

### 3. Continue / Cline / Roo Code на маршрут LiteLLM (не Hermes)

Любое OpenAI-совместимое расширение **Continue / Cline / Roo Code** можно
подключить напрямую к прокси:

- Base URL: `http://localhost:4000/v1`
- Model: `cloud-sanitized-auto`
- API key: `sk-dummy`

Подсказки и правки в редакторе пойдут через тот же стек (Presidio +
де-анонимизация), но это **не** агент Hermes — агентского цикла, сессий и
рабочих инструментов у этого варианта нет.

#### Quick start для Continue

1. Установите расширение Continue для VS Code.
2. В корне проекта создайте файл `.continue/config.yaml` (можно взять пример
   из `.continue/config.yaml.example`).
3. Добавьте модель в OpenAI-совместимом формате:

```yaml
models:
  - name: Hermes via LiteLLM
    provider: openai
    model: cloud-sanitized-auto
    apiBase: http://localhost:4000/v1
    apiKey: sk-dummy
    contextLength: 131072
```

4. После сохранения выберите эту модель в Continue и проверьте, что запрос идёт
   через `cloud-sanitized-auto`.

Готовый пример лежит в файле [.continue/config.yaml.example](.continue/config.yaml.example).

Если расширение запрашивает схему `apiBase` или `baseUrl`, используйте
`http://localhost:4000/v1` и ключ `sk-dummy` — это тот же маршрут, что и в
`hermes-agent`.

#### Quick start для Cline / Roo Code

В репозитории уже лежат готовые шаблоны для редакторных клиентов, которые
поддерживают OpenAI-совместимый API:

- `.clinerules` — глобальные инструкции для Cline
- `.roo/roomodes.json` — режим Roo Code с безопасной политикой
- `.roo/cline-config.json` — конфиг API для Roo/Cline
- `.roo/roo-code-settings.json` — готовый JSON для импорта настроек Roo Code
- `.roo/README.md` — краткое пояснение для ручной настройки

Минимальная настройка:

- API provider: `OpenAI` with a custom base URL
- Base URL: `http://localhost:4000/v1`
- Model: `cloud-sanitized-auto`
- API key: `sk-dummy`

В Cline/roo это можно задать в настройках модели или в файлах конфигурации
проектов. Для работы важна одна вещь: подключение идёт не напрямую к
провайдеру, а через локальный LiteLLM-прокси, который уже применяет Presidio
маскирование и делает де-анонимизацию ответа.

Для Roo Code используйте файл
[`.roo/roo-code-settings.json`](.roo/roo-code-settings.json) через действие
импорта настроек, если оно доступно в установленной версии расширения. Если
импорта JSON в интерфейсе нет, перенесите из файла те же значения в форму
OpenAI-compatible provider вручную.

Если расширение поддерживает `customInstructions` или `rules`, используйте
текст:

```text
Use the sanitized route cloud-sanitized-auto via LiteLLM.
Do not send raw secrets or PII to the upstream provider.
```

## Короткая шпаргалка: запуск и проверка редакторной интеграции

### 1) Поднять стек

```bash
cd /home/alexander/projects/presidio-litellm
./update_models_and_run.sh
```

Проверка: `curl http://localhost:4000/health/liveliness` должен вернуть HTTP 200,
`curl http://localhost:5001/health` — `{"status":"ok"}`.

### 2) Continue

Используйте этот набор настроек:

```yaml
models:
  - name: Hermes via LiteLLM
    provider: openai
    model: cloud-sanitized-auto
    apiBase: http://localhost:4000/v1
    apiKey: sk-dummy
    contextLength: 131072
```

Файл-образец уже есть в [.continue/config.yaml.example](.continue/config.yaml.example).

### 3) Cline / Roo Code

Базовый JSON-конфиг:

```json
{
  "apiProvider": "openai",
  "apiBaseUrl": "http://localhost:4000/v1",
  "apiModelId": "cloud-sanitized-auto",
  "apiKey": "sk-dummy"
}
```

Для проекта уже подготовлены шаблоны:

- [.clinerules](.clinerules)
- [.roo/roomodes.json](.roo/roomodes.json)
- [.roo/cline-config.json](.roo/cline-config.json)
- [.roo/README.md](.roo/README.md)

### 4) ACP / Hermes в VS Code

```bash
./hermes-acp.sh
# или
./.vscode/tasks.json -> Hermes ACP
```

Ожидаемое условие: `docker exec hermes-agent hermes acp --check` возвращает OK,
а редактор подключается к ACP-серверу через тот же безопасный маршрут:
`cloud-sanitized-auto` → LiteLLM → Presidio → OmniRoute.

### 5) Проверка маршрута

```bash
curl http://localhost:4000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer sk-dummy' \
  -d '{"model":"cloud-sanitized-auto","messages":[{"role":"user","content":"Мой email vasya@example.com, назови столицу Франции"}]}'
```

Важно: исходящий текст должен уходить в замаскированном виде, а ответ
восстанавливаться локально. Это означает, что запросы к внешним провайдерам
передаются через Presidio и только после маскирования PII.

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

# egress lock: внутри работает, наружу — refused
docker exec hermes-agent curl -m 5 -sS -o /dev/null http://litellm:4000/health/liveliness
docker exec hermes-agent curl -m 5 -sS -o /dev/null https://example.com 2>&1 | tail -1

# гранты: что попало в блок-лист и чем это подтверждается
docker exec hermes-agent python3 /usr/local/bin/hermes-filegate.py \
  --workspace /workspace --grants presidio-litellm
BLOCK=$(docker exec hermes-agent python3 /usr/local/bin/hermes-filegate.py \
  --workspace /workspace --grants presidio-litellm)
docker exec -e HERMES_BLOCK="$BLOCK" -e LD_PRELOAD=/usr/local/lib/filegate.so \
  hermes-agent cat /workspace/presidio-litellm/.env 2>&1 | tail -1   # Permission denied
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
  мультимодальные задачи (vision/mcp) не трогаются. Из-за egress lock реальный
  веб-сёрфинг (`web_extract`, прямые запросы из bash) внутри контейнера закрыт,
  а aux-задачи, завёрнутые на `litellm`, работают.
- Запуск агента — **через обёртку `hermes-chat`**, а не напрямую `hermes`: только
  так действуют гранты каталогов и ignore-файлы (см. «Разрешения на каталоги»).
  Это касается и VSCode-терминала, и команды `update_models_and_run.sh`.

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
├── hermes_grants.json           # выданные агенту гранты на каталоги (см. «Разрешения на каталоги»)
├── hermes-chat.sh               # обёртка запуска Hermes: гранты + blocklist + LD_PRELOAD
├── hermes-filegate.py           # вычисление блок-листа из грантов и ignore-файлов
├── filegate.c                   # LD_PRELOAD-перехватчик open/openat (собирается в Dockerfile.hermes)
├── presidio_config.yaml         # кастомные распознаватели PII
├── presidio-server/             # FastAPI-обёртка над Presidio
│   ├── Dockerfile
│   └── app.py                   # /analyze, /anonymize, /health
├── litellm-proxy/
│   ├── generate_config.py       # пишет config.yaml (маршрут + guardrails presidio)
│   ├── custom_callbacks.py      # MaxTokensClamp + LiteralSecretMasker
│   ├── secrets_map.example.json # шаблон словаря литеральных секретов (копировать в secrets_map.json)
│   └── custom_presidio.py       # не используется (работает встроенный гардрейл)
├── Dockerfile.hermes            # образ агента Hermes (собирает filegate.so)
├── README.ru.md                  # русская документация
├── README.uk.md                  # українська документація
└── README.en.md                  # English documentation
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
  и создаёт комбо `cloud-auto` (стратегия `auto`, только бесплатные модели) —
  с весами приоритета no-training провайдеров (mistral 5, gemini 4, groq 3,
  openrouter 1; см. «Провайдерская приватность»). Если комбо уже существует —
  обновляет веса через PUT. Повторный запуск безопасен.

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
- `litellm-proxy/custom_callbacks.py`: два кастомных коллбека:
  - `MaxTokensClamp` — режет исходящий `max_tokens` до `MAX_OUTPUT_TOKENS`,
    чтобы запросы агента вписывались в лимиты бесплатных моделей;
  - `LiteralSecretMasker` — маскирует точные значения из
    `litellm-proxy/secrets_map.json` (см. «Литеральные секреты») и
    восстанавливает их в ответе. Оба коллбека подключены через
    `litellm_settings.callbacks` в `config.yaml`.

### 5. Агент Hermes

- `Dockerfile.hermes`: Python 3.11-slim, venv, `pip install hermes-agent`;
  ставит `gcc libc6-dev` и компилирует `filegate.so` из `filegate.c`
  (LD_PRELOAD-перехватчик для «Разрешения на каталоги»).
- Сервис монтирует `/home/alexander/projects` в `/workspace` и подключается к
  LiteLLM как `custom`-провайдер (`CUSTOM_BASE_URL=http://litellm:4000/v1`,
  `CUSTOM_API_KEY=sk-dummy`).
- `hermes_config.yaml` переводит aux-задачи Hermes (заголовки сессий, сжатие
  контекста, веб-экстракция, skills_hub, approval) на наш канал —
  `model: cloud-sanitized-auto`, `provider: custom`,
  `base_url: http://litellm:4000/v1`.

### 6. Защита контура агента (egress lock + гранты)

- **Egress lock** в `docker-compose.yml` сервиса `hermes-agent`: прокси-переменные
  на несуществующий адрес + `NO_PROXY` с внутренними хостами. Весь внешний
  трафик из контейнера молча падает, LLM-запросы к `litellm` идут напрямую
  (см. «Запирание исходящего трафика»).
- **Гранты на каталоги**: `hermes-chat.sh` (запуск агента вместо `hermes`) +
  `hermes-filegate.py` (блок-лист из грантов и ignore-файлов) + `filegate.c`
  (LD_PRELOAD-перехватчик). Гранты хранятся в `hermes_grants.json`,
  монтируются как `/root/.hermes/grants.json` (см. «Разрешения на каталоги»).
- **Литеральные секреты**: `litellm-proxy/secrets_map.json` +
  `LiteralSecretMasker` (см. «Литеральные секреты»).

### 7. Чат-интерфейс Open WebUI

Сервис `open-webui` (`ghcr.io/open-webui/open-webui:main`), порт `3000:8080`,
том `open-webui-data`, `OPENAI_API_BASE_URL=http://litellm:4000/v1` —
весь чат-трафик идёт через те же анонимизацию и маршрут (см. «Чат в
браузере»).

### 8. `docker-compose.yml` и запуск

- Порядок зависимостей: `litellm` ждёт `presidio` и `omniroute` healthy;
  `hermes-agent` и `open-webui` зависят от `litellm`.
- `update_models_and_run.sh` выполняет порядок: `omniroute` отдельно →
  прогрев → `provision_omniroute.sh` → весь стек → готовность LiteLLM → чат
  Hermes. Запуск одной командой:
  ```bash
  ./update_models_and_run.sh
  ```