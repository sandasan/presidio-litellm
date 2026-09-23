#!/usr/bin/env python3
"""Вычисляет блок-лист HERMES_BLOCK для LD_PRELOAD-шима filegate.so.

Блокируется:
  1. всё, что находится вне выданных грантов (другие каталоги /workspace);
  2. файлы/подкаталоги, подпадающие под ignore-правила в выданных грантах:
     .gitignore, .aiignore, .cursorignore, .ignore, .npmignore, .dockerignore,
     .git/info/exclude, а также разумный дефолт (node_modules, __pycache__,
     виртуальные окружения, .env*).

Каталоги записываются с завершающим '/' (блокируется весь подкаталог,
включая создаваемое в нём позже содержимое), файлы — целиком. Правила
используют gitignore-семантику: порядок, «!»-возвраты, «dir/» (только
каталоги), ведущий «/» (якорь к корню гранта), «**», «*», «?». Изложенная
реализация намеренно упрощена и не гонится за 100% граничными случаями git.

Выводит блок-лист в stdout строкой на путь (или ничего, если пуст).
"""

import argparse
import fnmatch
import os
import re
import sys

DEFAULT_IGNORES = [
    "node_modules/",
    "__pycache__/",
    ".venv/",
    "venv/",
    ".env*",
]

IGNORE_FILES = [
    ".gitignore",
    ".aiignore",
    ".cursorignore",
    ".ignore",
    ".npmignore",
    ".dockerignore",
]

EXCLUDE_FILE = os.path.join(".git", "info", "exclude")


class IgnoreRules:
    """Набор ignore-правил одного гранта (якорь — корень гранта)."""

    def __init__(self):
        self.rules = []  # (regex, negated)

    def add_pattern(self, line):
        line = line.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            return
        negated = line.startswith("!")
        if negated:
            pat = line[1:]
        else:
            pat = line
        if not pat.strip():
            return
        dir_only = pat.endswith("/")
        if dir_only:
            pat = pat.rstrip("/")
        anchored = pat.startswith("/")
        if anchored:
            pat = pat.lstrip("/")
        has_slash = "/" in pat

        regex = self._translate(pat)
        if not has_slash and not anchored:
            regex = r"(?:^|/)" + regex
        regex = "^" + regex + ("/?" if dir_only else "") + "$"
        try:
            self.rules.append((re.compile(regex), negated, dir_only))
        except re.error:
            pass

    @staticmethod
    def _translate(pat):
        out = []
        i = 0
        n = len(pat)
        while i < n:
            c = pat[i]
            if c == "*":
                if i + 1 < n and pat[i + 1] == "*":
                    out.append(".*")
                    i += 2
                    continue
                out.append("[^/]*")
            elif c == "?":
                out.append("[^/]")
            else:
                out.append(re.escape(c))
            i += 1
        return "".join(out)

    def ignored(self, relpath, is_dir):
        """True, если relpath (без ведущего '/') игнорируется по gitignore-семантике."""
        res = None
        neg_res = None
        for rx, negated, dir_only in reversed(self.rules):
            if dir_only and not is_dir:
                continue
            if rx.match(relpath) or rx.match(relpath + "/"):
                res = not negated
                neg_res = negated
                break
        return res if res is not None else False

    def has_negations(self):
        return any(r[1] for r in self.rules)

    def load_file(self, path):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    self.add_pattern(line)
        except OSError:
            pass


def build_blocks(workspace, grants):
    workspace = os.path.realpath(workspace)
    blocks = []

    # 1. Невыданные каталоги верхнего уровня.
    try:
        top = sorted(os.listdir(workspace))
    except OSError:
        top = []
    single_workspace = any(g in (".", "/") for g in grants)
    grant_set = {os.path.basename(g.rstrip("/")) for g in grants if g and g not in (".", "/")}
    for name in top:
        full = os.path.join(workspace, name)
        if single_workspace or name in grant_set:
            continue
        if os.path.isdir(full) or os.path.islink(full):
            blocks.append(os.path.realpath(full) + "/")

    # 2. Ignore-правила внутри каждого гранта.
    for grant in grants:
        grant = grant.strip("/")
        if grant == ".":
            grant = ""
        if not grant and not single_workspace:
            continue
        root = os.path.realpath(os.path.join(workspace, grant))
        if not root.startswith(workspace.rstrip("/") + "/") and root != workspace:
            continue
        rules = IgnoreRules()
        for fname in IGNORE_FILES:
            rules.load_file(os.path.join(root, fname))
        rules.load_file(os.path.join(root, EXCLUDE_FILE))
        for d in DEFAULT_IGNORES:
            rules.add_pattern(d)
        if not rules.rules:
            continue
        need_neg = rules.has_negations()
        for dirpath, _dirs, files in os.walk(root, topdown=True):
            rel_base = os.path.relpath(dirpath, root)
            dirs = list(_dirs)
            pruned = []
            for d in dirs:
                rel = d if rel_base == "." else os.path.join(rel_base, d)
                if rules.ignored(rel, is_dir=True):
                    blocks.append(os.path.realpath(os.path.join(dirpath, d)) + "/")
                    if need_neg and rules.ignored(rel, is_dir=True):
                        pass
                    else:
                        continue  # не углубляемся в игнорируемый каталог
                pruned.append(d)
            _dirs[:] = [d for d in dirs if d in pruned]
            for f in files:
                rel = f if rel_base == "." else os.path.join(rel_base, f)
                if rules.ignored(rel, is_dir=False):
                    blocks.append(os.path.realpath(os.path.join(dirpath, f)))

    # Убираем дубликаты (без разрушения порядка на скорость).
    seen = set()
    uniq = []
    for b in blocks:
        if b not in seen:
            seen.add(b)
            uniq.append(b)
    return uniq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default="/workspace")
    ap.add_argument("--grants", default="")
    args = ap.parse_args()
    grants = [g.strip() for g in args.grants.split(",") if g.strip()]
    blocks = build_blocks(args.workspace, grants)
    sys.stdout.write("\n".join(blocks))


if __name__ == "__main__":
    main()