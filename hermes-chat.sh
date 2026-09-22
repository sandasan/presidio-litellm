#!/usr/bin/env bash
# hermes-chat — запуск Hermes-агента в контейнере с грантами на каталоги.
#
# Порядок работы:
#   1. Определяет, к каким каталогам /workspace агент допущен (гранты):
#      - экспорт HERMES_GRANTS="proj1,proj2" (приоритет), либо
#      - сохранённые в ~/.hermes/grants.json, либо
#      - интерактивный выбор (docker exec -it hermes-agent hermes-chat).
#      Если ничего не задано и stdin не интерактивен — гранты пусты,
#      читать агенту будет нечего (только /workspace сам по себе).
#   2. Собирает блок-лист HERMES_BLOCK (негрантованные каталоги + ignore-файлы
#      внутри грантов) и включает LD_PRELOAD-перехватчик filegate.so.
#   3. Запускает hermes из корня гранта (или /workspace, если грантов несколько).
set -euo pipefail

WORKSPACE="${HERMES_WORKSPACE:-/workspace}"
HERMES_BIN="${HERMES_AGENT_BIN:-/opt/venv/bin/hermes}"
GRANTS_FILE="${HERMES_GRANTS_FILE:-$HOME/.hermes/grants.json}"
FILEGATE_PY=/usr/local/bin/hermes-filegate.py
GATE_SO=/usr/local/lib/filegate.so

pick_grants_interactive() {
    local dirs=()
    while IFS= read -r d; do dirs+=("$d"); done < <(find "$WORKSPACE" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort)
    if [[ ${#dirs[@]} -eq 0 ]]; then
        echo "hermes-chat: в $WORKSPACE нет каталогов для грантов." >&2
        return 1
    fi
    echo "hermes-chat: каким каталогом(ами) разрешить работать агенту?"
    local i
    for i in "${!dirs[@]}"; do
        printf '  %d) %s\n' "$((i + 1))" "${dirs[$i]}"
    done
    printf '  a) все   n) ни одним\n> '
    IFS= read -r -a ans
    local chosen=()
    local tok
    for tok in "${ans[@]}"; do
        case "$tok" in
            a | all | A | ALL) chosen=("${dirs[@]}") ;;
            n | none | N | NONE) chosen=() ;;
            *)
                if [[ "$tok" =~ ^[0-9]+$ ]] && ((tok >= 1 && tok <= ${#dirs[@]})); then
                    chosen+=("${dirs[$((tok - 1))]}")
                fi
                ;;
        esac
    done
    # Склеиваем в JSON и сохраняем.
    {
        printf '['
        local first=1 c
        for c in "${chosen[@]}"; do
            [[ $first -eq 0 ]] && printf ','
            printf '"%s"' "$c"
            first=0
        done
        printf ']\n'
    } | python3 -c 'import json,sys; p=sys.argv[1]; import os; os.makedirs(os.path.dirname(p) or ".", exist_ok=True); json.dump(json.load(sys.stdin), open(p,"w"))' "$GRANTS_FILE"
    echo "hermes-chat: сохранил гранты в $GRANTS_FILE" >&2
    IFS=',' eval 'chosen_str="${chosen[*]}"'
    echo "$chosen_str"
}

# 1. Гранты.
GRANTS="${HERMES_GRANTS:-}"
if [[ -z "$GRANTS" && -f "$GRANTS_FILE" ]]; then
    GRANTS="$(python3 -c 'import json,sys
try:
    d=json.load(open(sys.argv[1]))
    print(",".join(d) if isinstance(d,list) else "")
except Exception:
    print("")' "$GRANTS_FILE" || true)"
fi
if [[ -z "$GRANTS" ]]; then
    if [[ -t 0 && -t 1 ]]; then
        GRANTS="$(pick_grants_interactive)" || true
    else
        echo "hermes-chat: гранты не заданы (HERMES_GRANTS или $GRANTS_FILE); агенту ничего не разрешено читать." >&2
    fi
fi

# 2. Блок-лист + перехватчик.
BLOCK_FILE="$(mktemp)"
python3 "$FILEGATE_PY" --workspace "$WORKSPACE" --grants "$GRANTS" >"$BLOCK_FILE"
HERMES_BLOCK="$(tr '\n' ',' <"$BLOCK_FILE" | sed 's/,$//')"
rm -f "$BLOCK_FILE"
export HERMES_BLOCK
if [[ -f "$GATE_SO" ]]; then
    export LD_PRELOAD="${LD_PRELOAD:+$LD_PRELOAD:}$GATE_SO"
fi
echo "hermes-chat: гранты=[${GRANTS:-}] заблокировано записей=$([[ -n "$HERMES_BLOCK" ]] && echo "${HERMES_BLOCK//,/ }" | wc -w || echo 0)" >&2

# 3. cwd = корень гранта, если он один.
GRANT_ROOT="$WORKSPACE"
if [[ -n "$GRANTS" && "$GRANTS" != *","* ]]; then
    GRANT_ROOT="$WORKSPACE/${GRANTS#/}"
    mkdir -p "$GRANT_ROOT"
fi
cd "$GRANT_ROOT"
exec "$HERMES_BIN" "$@"