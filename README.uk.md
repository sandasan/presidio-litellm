# presidio-litellm

[Русский](README.ru.md) | **Українська** | [English](README.en.md)

Docker-стек для запуску агента Hermes із захищеним доступом до безкоштовних
моделей ШІ:

```
Hermes Agent -> LiteLLM Proxy -> Presidio (анонімізація PII) -> OmniRoute -> провайдер ШІ
Open WebUI ->
```

Усі запити до хмарних моделей проходять через **Presidio**. Персональні дані,
зокрема адреси електронної пошти, імена, телефони, API-ключі, рядки підключення
до баз даних і внутрішні IP-адреси, маскуються до надсилання провайдеру.

## Швидкий старт

1. Створіть файл середовища та додайте ключі провайдерів:

   ```bash
   cp .env.example .env
   ```

2. Запустіть увесь стек:

   ```bash
   ./update_models_and_run.sh
   ```

   Скрипт запускає OmniRoute, підключає провайдерів, збирає сервіси та запускає
   Hermes через LiteLLM і Presidio.

3. Відкрийте Open WebUI за адресою <http://localhost:3000> або запустіть Hermes
   у терміналі:

   ```bash
   docker exec -it hermes-agent hermes-chat chat \
     --provider custom -m cloud-sanitized-auto
   ```

## Запуск Hermes для певного проєкту

Контейнер монтує каталог `PROJECTS_DIR` на хості як `/workspace`
(`PROJECTS_DIR` за замовчуванням дорівнює `/home/alexander/projects`). Вкажіть
його у `.env`, якщо проєкти зберігаються в іншому місці.
Значення `HERMES_GRANTS` є назвою каталогу безпосередньо всередині цього
каталогу на хості.

Для `/home/alexander/projects/my-app` надайте доступ лише до цього проєкту:

```bash
# Запустіть стек із кореня цього репозиторію
./update_models_and_run.sh

# В іншому терміналі
docker exec -it \
  -e HERMES_GRANTS=my-app \
  hermes-agent hermes-chat chat --provider custom -m cloud-sanitized-auto
```

У контейнері проєкт доступний як `/workspace/my-app`. Доступ до кількох
проєктів можна надати списком через кому:

```bash
docker exec -it \
  -e HERMES_GRANTS=my-app,another-app \
  hermes-agent hermes-chat chat --provider custom -m cloud-sanitized-auto
```

Використовуйте обгортку `hermes-chat`, а не запускайте `hermes` безпосередньо.
Вона створює список заборонених шляхів для всіх ненаданих каталогів, застосовує
файли ігнорування (`.gitignore`, `.aiignore` тощо) і вмикає файловий захист
`filegate`. Файли поза дозволеним проєктом повертають `Permission denied`.

Для неінтерактивної задачі вкажіть грант явно:

```bash
docker exec -e HERMES_GRANTS=my-app hermes-agent hermes-chat \
  -z "Inspect the project and summarize the failing tests"
```

Якщо проєкт зберігається в іншому місці, додайте його каталог на хості до
секції `volumes` сервісу `hermes-agent` у `docker-compose.yml`, а потім
використовуйте назву змонтованого каталогу в `HERMES_GRANTS`.

Для проєкту в будь-якому місці можна безпосередньо змонтувати його кореневий
каталог:

```bash
./hermes-acp.sh "$(basename "$PWD")" "$PWD"
```

У цьому режимі проєкт доступний як `/workspace`, grant дорівнює `.`, а під час
перемикання на іншу папку пересоздається лише контейнер Hermes. Завдання VS
Code вже використовує цей режим через `${workspaceFolder}`. Для звичайного
чату вкажіть у `.env` `PROJECTS_DIR=/абсолютний/шлях/до/проєкту` і використайте
`HERMES_GRANTS=.`.

## Hermes у VS Code: ACP

Hermes підтримує Agent Client Protocol (ACP) для інтеграції з редакторами.
Для розширень `Poppywu124.hermes-chat` і `joaompfp.hermes-ai-agent` один раз
встановіть переносиму обгортку:

```bash
./install-hermes-vscode.sh
```

Скрипт створює `~/.local/bin/hermes-acp` як символічне посилання на обгортку
репозиторію. За потреби додайте `~/.local/bin` до `PATH`, а потім перезапустіть
VS Code. Обидва розширення використовують команду `hermes-acp`, тому користувачу
не потрібно змінювати особистий абсолютний шлях. У репозиторії вже є такі
налаштування:

```json
{
  "hermes.path": "hermes",
  "hermes-chat.hermesPath": "hermes-acp",
  "hermes-chat.autoApproveTools": false
}
```

Коли папка відкрита, розширення запускають `hermes-acp acp`. Обгортка бере
поточний VS Code workspace, монтує його як `/workspace`, за потреби запускає
стек і пересоздає лише контейнер Hermes.

Після запуску стека та встановлення ACP-залежностей в образі Hermes перевірте
встановлення:

```bash
docker exec hermes-agent hermes acp --check
```

Запустіть готову ACP-обгортку з кореня репозиторію:

```bash
HERMES_GRANTS=my-app ./hermes-acp.sh
```

Розширення самостійно запускає `hermes-acp acp` і використовує поточну
відкриту папку. Не запускайте одночасно окреме ACP-завдання: воно створить
друге ACP-з'єднання та може пересоздати контейнер Hermes під час роботи
розширення.

Обгортка запускає Hermes через захищений маршрут LiteLLM і передає
`HERMES_GRANTS` у контейнер. Цю саму команду можна налаштувати як команду
stdio у розширенні VS Code із підтримкою ACP. У репозиторії також є завдання
VS Code `Hermes ACP`.

Агент має бути обмежений проєктом, який ви редагуєте:

```bash
export HERMES_GRANTS=my-app
./hermes-acp.sh
```

## Маршрутизація та приватність

Маршрут моделі має назву `cloud-sanitized-auto`:

```
Hermes / Open WebUI -> LiteLLM (Presidio) -> OmniRoute -> провайдер
```

OmniRoute вибирає доступну безкоштовну модель і може перемикатися між
провайдерами після rate limit, тайм-ауту або помилки сервера. LiteLLM застосовує
захист Presidio перед передаванням запиту. Відповіді деанонімізуються локально,
тому Hermes знову показує оригінальні значення.

У контейнері агента працює блокування вихідного трафіку. Доступ до внутрішніх
сервісів стека дозволений, а довільні зовнішні HTTP(S)-запити агента
блокуються. Тому браузер і прямий веб-серфінг навмисно не працюють.

Не вважайте маскування PII загальною конфіденційністю. Код, архітектура,
бізнес-логіка та внутрішні назви не стають приватними автоматично, якщо вони
не відповідають налаштованому розпізнавачу або не додані до
`litellm-proxy/secrets_map.json`.

## Літеральні секрети

Значення, які не розпізнаються як PII, можна додати до
`litellm-proxy/secrets_map.json` (як шаблон використовуйте
`secrets_map.example.json`). Перед надсиланням запиту вони замінюються, а в
локальній відповіді відновлюються. Використовуйте унікальні рядки з високою
ентропією: короткі поширені слова можуть погіршити запити й відповіді.

Після зміни карти пересоздайте LiteLLM:

```bash
docker compose up -d --force-recreate litellm
```

## Корисні перевірки

```bash
curl http://localhost:5001/health
docker exec hermes-agent hermes acp --check
docker compose ps
```

Повна російська документація містить детальний опис сервісів, провайдерів,
захисту файлів і усунення несправностей:
[README.ru.md](README.ru.md).
