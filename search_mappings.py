#!/usr/bin/env python3
"""
Скрипт для поиска сопоставлений в базе данных opencode-bridge.
Использование:
  python3 search_mappings.py --all                    # Все сопоставления
  python3 search_mappings.py --token <TOKEN>        # Поиск по токену
  python3 search_mappings.py --value <VALUE>         # Поиск по значению
  python3 search_mappings.py --type <TYPE>           # Поиск по типу сущности
  python3 search_mappings.py --session <SESSION_ID>  # Поиск по сессии
  python3 search_mappings.py --limit 20              # Ограничить количество
"""

import subprocess
import argparse
import sys

CONTAINER_NAME = "opencode-bridge"
DB_PATH = "/var/lib/opencode-bridge/mappings.sqlite3"


def run_python_in_container(code):
    """Выполнить Python код внутри контейнера"""
    cmd = ['docker', 'exec', CONTAINER_NAME, 'python3', '-c', code]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result


def main():
    global CONTAINER_NAME
    parser = argparse.ArgumentParser(description='Поиск в базе сопоставлений opencode-bridge')
    parser.add_argument('--all', action='store_true', help='Показать все сопоставления')
    parser.add_argument('--token', help='Поиск по токену (placeholder)')
    parser.add_argument('--value', help='Поиск по оригинальному значению')
    parser.add_argument('--type', help='Поиск по типу сущности')
    parser.add_argument('--session', help='Поиск по ID сессии')
    parser.add_argument('--limit', type=int, default=20, help='Ограничить количество результатов (по умолчанию 20)')
    parser.add_argument('--container', default=CONTAINER_NAME, help='Имя контейнера Docker')

    args = parser.parse_args()
    CONTAINER_NAME = args.container

    limit_clause = f" LIMIT {args.limit}" if args.limit else ""

    if args.all:
        code = f"""
import sqlite3
from datetime import datetime
conn = sqlite3.connect('{DB_PATH}')
cursor = conn.cursor()
cursor.execute('SELECT * FROM mappings ORDER BY created_at DESC{limit_clause}')
print(f'{{"Token":<45}} | {{"Type":<20}} | {{"Value":<40}} | {{"Time"}}')
print('-' * 130)
for row in cursor.fetchall():
    session_id, placeholder, entity_type, original_value, created_at = row
    time_str = datetime.fromtimestamp(created_at).strftime('%Y-%m-%d %H:%M:%S') if created_at else 'N/A'
    value_str = original_value[:37] + '...' if len(original_value) > 40 else original_value
    print(f'{{placeholder:<45}} | {{entity_type:<20}} | {{value_str:<40}} | {{time_str}}')
conn.close()
"""
    elif args.token:
        code = f"""
import sqlite3
from datetime import datetime
conn = sqlite3.connect('{DB_PATH}')
cursor = conn.cursor()
cursor.execute('SELECT * FROM mappings WHERE placeholder = ?', ['{args.token}'])
row = cursor.fetchone()
if row:
    session_id, placeholder, entity_type, original_value, created_at = row
    time_str = datetime.fromtimestamp(created_at).strftime('%Y-%m-%d %H:%M:%S') if created_at else 'N/A'
    print(f'Placeholder: {{placeholder}}')
    print(f'Type: {{entity_type}}')
    print(f'Original Value: {{original_value}}')
    print(f'Created: {{time_str}}')
    print(f'Session ID: {{session_id}}')
else:
    print('Токен не найден')
conn.close()
"""
    elif args.value:
        code = f"""
import sqlite3
from datetime import datetime
conn = sqlite3.connect('{DB_PATH}')
cursor = conn.cursor()
cursor.execute('SELECT * FROM mappings WHERE original_value LIKE ?', ['%{args.value}%']{limit_clause})
print(f'{{"Token":<45}} | {{"Type":<20}} | {{"Value":<40}} | {{"Time"}}')
print('-' * 130)
for row in cursor.fetchall():
    session_id, placeholder, entity_type, original_value, created_at = row
    time_str = datetime.fromtimestamp(created_at).strftime('%Y-%m-%d %H:%M:%S') if created_at else 'N/A'
    value_str = original_value[:37] + '...' if len(original_value) > 40 else original_value
    print(f'{{placeholder:<45}} | {{entity_type:<20}} | {{value_str:<40}} | {{time_str}}')
conn.close()
"""
    elif args.type:
        code = f"""
import sqlite3
from datetime import datetime
conn = sqlite3.connect('{DB_PATH}')
cursor = conn.cursor()
cursor.execute('SELECT * FROM mappings WHERE entity_type = ? ORDER BY created_at DESC{limit_clause}', ['{args.type}'])
print(f'{{"Token":<45}} | {{"Value":<40}} | {{"Time"}}')
print('-' * 100)
for row in cursor.fetchall():
    session_id, placeholder, entity_type, original_value, created_at = row
    time_str = datetime.fromtimestamp(created_at).strftime('%Y-%m-%d %H:%M:%S') if created_at else 'N/A'
    value_str = original_value[:37] + '...' if len(original_value) > 40 else original_value
    print(f'{{placeholder:<45}} | {{value_str:<40}} | {{time_str}}')
conn.close()
"""
    elif args.session:
        code = f"""
import sqlite3
from datetime import datetime
conn = sqlite3.connect('{DB_PATH}')
cursor = conn.cursor()
cursor.execute('SELECT * FROM mappings WHERE session_id = ? ORDER BY created_at DESC{limit_clause}', ['{args.session}'])
print(f'{{"Token":<45}} | {{"Type":<20}} | {{"Value":<40}} | {{"Time"}}')
print('-' * 130)
for row in cursor.fetchall():
    session_id, placeholder, entity_type, original_value, created_at = row
    time_str = datetime.fromtimestamp(created_at).strftime('%Y-%m-%d %H:%M:%S') if created_at else 'N/A'
    value_str = original_value[:37] + '...' if len(original_value) > 40 else original_value
    print(f'{{placeholder:<45}} | {{entity_type:<20}} | {{value_str:<40}} | {{time_str}}')
conn.close()
"""
    else:
        parser.print_help()
        sys.exit(1)

    result = run_python_in_container(code.strip())

    if result.returncode == 0:
        print(result.stdout)
    else:
        print(f"Ошибка: {result.stderr}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
