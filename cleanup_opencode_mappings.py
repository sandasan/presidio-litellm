import os
import sqlite3
import argparse
from pathlib import Path

# --- КОНФИГУРАЦИЯ ПУТЕЙ ПО УМОЛЧАНИЮ НА ХОСТ-МАШИНЕ ---
HOST_PROJECTS_DIR = Path("/home/alexander/projects")

def get_default_db_path():
    """Пытается автоматически определить стандартный путь к базе в Docker Volume."""
    try:
        current_dir_name = Path(os.getcwd()).name
        return Path("/var/lib/docker/volumes") / f"{current_dir_name}_opencode-bridge-data" / "_data" / "anonymizer.db"
    except Exception:
        return Path("/var/lib/docker/volumes/opencode-bridge-data/_data/anonymizer.db")

def parse_arguments():
    """Обрабатывает аргументы командной строки."""
    parser = argparse.ArgumentParser(
        description="Скрипт очистки SQLite БД анонимайзера от устаревших маппингов удаленных проектов."
    )
    parser.add_argument(
        "-d", "--db",
        type=str,
        help="Прямой путь к файлу базы данных SQLite (anonymizer.db)"
    )
    return parser.parse_args()

def get_valid_db_path():
    """Определяет финальный путь к БД: из аргументов, дефолтов или интерактивного ввода."""
    args = parse_arguments()

    # 1. Проверяем, передан ли путь через аргументы командной строки
    if args.db:
        db_path = Path(args.db)
        if db_path.exists():
            return db_path
        print(f"[-] Указанный в аргументах файл не найден: {db_path}")

    # 2. Проверяем стандартный путь по умолчанию
    default_path = get_default_db_path()
    if default_path.exists():
        print(f"[+] Автоматически обнаружена база данных: {default_path}")
        return default_path

    # 3. Интерактивный ввод в bash, если предыдущие шаги не дали результата
    print("[-] Не удалось автоматически найти файл базы данных anonymizer.db.")
    while True:
        user_input = input(" Укажите путь к файлу базы данных SQLite (или 'q' для выхода): ").strip()
        if user_input.lower() == 'q':
            print("[*] Выход из скрипта.")
            return None

        chosen_path = Path(user_input).expanduser().resolve()
        if chosen_path.exists() and chosen_path.is_file():
            return chosen_path
        else:
            print(f"[-] Файл не найден по пути: {chosen_path}. Попробуйте еще раз.")

def cleanup_orphaned_mappings():
    db_path = get_valid_db_path()
    if not db_path:
        return

    print(f"[+] Работаем с базой данных: {db_path}")
    conn = sqlite3.connect(str(db_path))

    # Включаем поддержку Foreign Keys для каскадного удаления связанных сессий и сообщений
    conn.execute("PRAGMA foreign_keys = ON;")
    cursor = conn.cursor()

    try:
        print(f"[+] Сканирование путей в проектах. Корень поиска: {HOST_PROJECTS_DIR}")

        # 1. Проверяем таблицу `project` (колонка worktree)
        cursor.execute("SELECT id, worktree, name FROM project")
        projects = cursor.fetchall()
        projects_to_delete = []

        for p_id, worktree, name in projects:
            if not worktree:
                continue

            # Конвертируем путь из контейнера (/workspace/...) в реальный путь на хосте
            path_in_container = Path(worktree)
            if "workspace" in path_in_container.parts:
                # Отрезаем /workspace (первые два элемента структуры путей) и соединяем с корнем на хосте
                relative_part = Path(*path_in_container.parts[2:])
                host_path = HOST_PROJECTS_DIR / relative_part
            else:
                host_path = Path(worktree)

            # Проверяем физическое существование директории проекта на диске хоста
            if not host_path.exists():
                print(f"[!] Проект '{name or p_id}' удален с диска (путь: {host_path}). Очистка маппингов...")
                projects_to_delete.append((p_id,))

        if projects_to_delete:
            cursor.executemany("DELETE FROM project WHERE id = ?", projects_to_delete)
            print(f"[V] Удалено проектов из БД: {len(projects_to_delete)} (связанные данные вычищены каскадно)")
        else:
            print("[+] Все зарегистрированные проекты существуют на диске.")

        # 2. Проверяем таблицу `workspace` (колонка directory)
        cursor.execute("SELECT id, directory, name FROM workspace")
        workspaces = cursor.fetchall()
        workspaces_to_delete = []

        for w_id, directory, name in workspaces:
            if not directory:
                continue

            path_in_container = Path(directory)
            if "workspace" in path_in_container.parts:
                relative_part = Path(*path_in_container.parts[2:])
                host_path = HOST_PROJECTS_DIR / relative_part
            else:
                host_path = Path(directory)

            if not host_path.exists():
                print(f"[!] Воркспейс '{name or w_id}' не найден на диске ({host_path}). Удаление...")
                workspaces_to_delete.append((w_id,))

        if workspaces_to_delete:
            cursor.executemany("DELETE FROM workspace WHERE id = ?", workspaces_to_delete)
            print(f"[V] Удалено изолированных воркспейсов: {len(workspaces_to_delete)}")

        # Применяем изменения
        conn.commit()

        # Выполняем дефрагментацию и уменьшение размера файла БД на диске
        print("[+] Запуск дефрагментации файла базы данных (VACUUM)...")
        cursor.execute("VACUUM")
        conn.commit()
        print("[+] База данных успешно очищена и оптимизирована.")

    except sqlite3.Error as e:
        print(f"[-] Произошла ошибка при работе с SQLite: {e}")
        conn.rollback()
    finally:
        conn.close()

if __name__ == "__main__":
    cleanup_orphaned_mappings()
