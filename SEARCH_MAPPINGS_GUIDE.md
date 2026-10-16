# Руководство по поиску сопоставлений в базе данных opencode-bridge

## Общая информация

База данных SQLite хранится в Docker volume `opencode-bridge-data` внутри контейнера `opencode-bridge` по пути:
```
/var/lib/opencode-bridge/mappings.sqlite3
```

## Структура таблицы `mappings`

| Колонка | Тип | Описание |
|---------|-----|----------|
| session_id | TEXT | ID сессии (PRIMARY KEY) |
| placeholder | TEXT | Токен-заглушка (PRIMARY KEY), например `<EMAIL_ADDRESS_S1a2b3c4d5e6f_1>` |
| entity_type | TEXT | Тип сущности (EMAIL_ADDRESS, PERSON, URL и т.д.) |
| original_value | TEXT | Оригинальное значение (реальный email, имя и т.д.) |
| created_at | INTEGER | Время создания (Unix timestamp) |

## Способ 1: Использование скрипта `search_mappings.py` (рекомендуется)

### Установка
Скрипт уже находится в директории проекта: `/home/alexander/projects/presidio-litellm/search_mappings.py`

### Примеры использования

```bash
# Показать последние 20 сопоставлений
python3 search_mappings.py --all

# Показать последние 10 сопоставлений
python3 search_mappings.py --all --limit 10

# Поиск по токену (placeholder)
python3 search_mappings.py --token "<PERSON_Sf66b4607f39d_50>"

# Поиск по оригинальному значению (частичное совпадение)
python3 search_mappings.py --value "example.com"
python3 search_mappings.py --value "AuthToken"

# Поиск по типу сущности
python3 search_mappings.py --type "EMAIL_ADDRESS" --limit 10
python3 search_mappings.py --type "PERSON" --limit 10

# Поиск по ID сессии
python3 search_mappings.py --session "f66b4607f39d5460286caa4250563d2ad9d39ac39d4bbac682bc3e7aa1580f19"

# Использовать другой контейнер (если изменили имя)
python3 search_mappings.py --all --container my-opencode-bridge
```

## Способ 2: Прямые команды через Docker exec

### Подсчёт записей
```bash
docker exec opencode-bridge python3 -c "
import sqlite3
conn = sqlite3.connect('/var/lib/opencode-bridge/mappings.sqlite3')
cursor = conn.cursor()
cursor.execute('SELECT COUNT(*) FROM mappings')
print('Total mappings:', cursor.fetchone()[0])
conn.close()
"
```

### Показать последние N записей
```bash
docker exec opencode-bridge python3 -c "
import sqlite3
from datetime import datetime
conn = sqlite3.connect('/var/lib/opencode-bridge/mappings.sqlite3')
cursor = conn.cursor()
cursor.execute('SELECT * FROM mappings ORDER BY created_at DESC LIMIT 10')
for row in cursor.fetchall():
    session_id, placeholder, entity_type, original_value, created_at = row
    time_str = datetime.fromtimestamp(created_at).strftime('%Y-%m-%d %H:%M:%S')
    print(f'{placeholder} | {entity_type} | {original_value[:50]} | {time_str}')
conn.close()
"
```

### Поиск по токену
```bash
docker exec opencode-bridge python3 -c "
import sqlite3
from datetime import datetime
conn = sqlite3.connect('/var/lib/opencode-bridge/mappings.sqlite3')
cursor = conn.cursor()
cursor.execute('SELECT * FROM mappings WHERE placeholder = ?', ['<EMAIL_ADDRESS_S1a2b3c4d5e6f_1>'])
row = cursor.fetchone()
if row:
    session_id, placeholder, entity_type, original_value, created_at = row
    time_str = datetime.fromtimestamp(created_at).strftime('%Y-%m-%d %H:%M:%S')
    print(f'Placeholder: {placeholder}')
    print(f'Type: {entity_type}')
    print(f'Original: {original_value}')
    print(f'Created: {time_str}')
    print(f'Session: {session_id}')
else:
    print('Not found')
conn.close()
"
```

### Поиск по оригинальному значению
```bash
docker exec opencode-bridge python3 -c "
import sqlite3
from datetime import datetime
conn = sqlite3.connect('/var/lib/opencode-bridge/mappings.sqlite3')
cursor = conn.cursor()
cursor.execute('SELECT * FROM mappings WHERE original_value LIKE ?', ['%example.com%'])
for row in cursor.fetchall():
    session_id, placeholder, entity_type, original_value, created_at = row
    time_str = datetime.fromtimestamp(created_at).strftime('%Y-%m-%d %H:%M:%S')
    print(f'{placeholder} | {entity_type} | {original_value} | {time_str}')
conn.close()
"
```

### Поиск по типу сущности
```bash
docker exec opencode-bridge python3 -c "
import sqlite3
from datetime import datetime
conn = sqlite3.connect('/var/lib/opencode-bridge/mappings.sqlite3')
cursor = conn.cursor()
cursor.execute('SELECT * FROM mappings WHERE entity_type = ? ORDER BY created_at DESC LIMIT 10', ['EMAIL_ADDRESS'])
for row in cursor.fetchall():
    session_id, placeholder, entity_type, original_value, created_at = row
    time_str = datetime.fromtimestamp(created_at).strftime('%Y-%m-%d %H:%M:%S')
    print(f'{placeholder} | {original_value} | {time_str}')
conn.close()
"
```

### Поиск по сессии
```bash
docker exec opencode-bridge python3 -c "
import sqlite3
from datetime import datetime
conn = sqlite3.connect('/var/lib/opencode-bridge/mappings.sqlite3')
cursor = conn.cursor()
cursor.execute('SELECT * FROM mappings WHERE session_id = ? ORDER BY created_at DESC LIMIT 10', ['your-session-id'])
for row in cursor.fetchall():
    session_id, placeholder, entity_type, original_value, created_at = row
    time_str = datetime.fromtimestamp(created_at).strftime('%Y-%m-%d %H:%M:%S')
    print(f'{placeholder} | {entity_type} | {original_value[:50]} | {time_str}')
conn.close()
"
```

## Дополнительные таблицы в базе данных

В базе данных также есть таблицы:
- `sessions` - информация о сессиях
- `placeholder_counters` - счётчики placeholder'ов
- `session_placeholder_counters` - счётчики placeholder'ов по сессиям

### Просмотр всех таблиц
```bash
docker exec opencode-bridge python3 -c "
import sqlite3
conn = sqlite3.connect('/var/lib/opencode-bridge/mappings.sqlite3')
cursor = conn.cursor()
cursor.execute('SELECT name FROM sqlite_master WHERE type=\"table\"')
print('Tables:', cursor.fetchall())
conn.close()
"
```

### Просмотр схемы таблицы
```bash
docker exec opencode-bridge python3 -c "
import sqlite3
conn = sqlite3.connect('/var/lib/opencode-bridge/mappings.sqlite3')
cursor = conn.cursor()
cursor.execute('PRAGMA table_info(mappings)')
print('Schema:', cursor.fetchall())
conn.close()
"
```

## Очистка старых сопоставлений

По умолчанию сопоставления не удаляются автоматически. Чтобы включить очистку неактивных сессий, задайте положительный срок в секундах через переменную окружения `OPENCODE_MAPPING_TTL_SECONDS` в `.env`:

```bash
# В .env
OPENCODE_MAPPING_TTL_SECONDS=86400  # Удалять записи старше 24 часов
```

После изменения переменной нужно перезапустить контейнер:
```bash
docker compose restart opencode-bridge
```

Для ручной очистки можно использовать скрипт `cleanup_opencode_mappings.py`:
```bash
python3 cleanup_opencode_mappings.py
```
