/*
 * filegate.c — LD_PRELOAD-перехватчик файловых операций для hermes-agent.
 *
 * Контролирует, какие пути контейнеру реально доступны на чтение/запись,
 * независимо от того, какие инструменты (cat, редакторы, node, python, git)
 * пытаются их открыть. Блок-лист передаётся процессу через переменную
 * окружения HERMES_BLOCK (список абсолютных путей, разделённых ','):
 *   - если путь заканчивается на '/' — блокируется весь подкаталог;
 *   - иначе — блокируется только сам файл.
 *
 * Проверка делается по строго нормализованному абсолютному пути (коллапс
 * ".", "..", "//"), а затем по realpath — чтобы перехватить выходы через
 * символические ссылки. Возвращается EACCES, чтоб выглядело как проблема
 * прав, а не как мистический отказ ядра.
 *
 * Сборка в образе: gcc -shared -fPIC -O2 -o filegate.so filegate.c -ldl
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#define MAX_BLOCK 2048

static char *g_block[MAX_BLOCK];
static int g_block_n = -1; /* -1 = ещё не инициализирован */

static void gate_init(void) {
    const char *raw = getenv("HERMES_BLOCK");
    g_block_n = 0;
    if (raw == NULL) return;
    const char *p = raw;
    while (*p && g_block_n < MAX_BLOCK) {
        while (*p == ',' || *p == '\n' || *p == ' ') p++;
        if (!*p) break;
        const char *start = p;
        while (*p && *p != ',' && *p != '\n') p++;
        size_t len = (size_t)(p - start);
        if (len > 0 && len < PATH_MAX) {
            char *copy = strndup(start, len);
            if (copy) g_block[g_block_n++] = copy;
        }
    }
}

/* Нормализация: absolute path -> без '//', '/./', 'x/..' (лексически). */
static int normalize_abs(const char *in, char *out, size_t outsz) {
    char buf[PATH_MAX];
    if (in[0] != '/') {
        if (getcwd(buf, sizeof(buf)) == NULL) return -1;
        size_t l = strlen(buf);
        snprintf(buf + l, sizeof(buf) - l, "/%s", in);
        in = buf;
    }
    char tmp[PATH_MAX];
    snprintf(tmp, sizeof(tmp), "%s", in);
    out[0] = '\0';
    size_t w = 0;
    const char *tok = tmp;
    while (*tok) {
        while (*tok == '/') tok++;
        if (!*tok) break;
        const char *seg = tok;
        while (*tok && *tok != '/') tok++;
        size_t sl = (size_t)(tok - seg);
        if (sl == 1 && seg[0] == '.') continue;
        if (sl == 2 && seg[0] == '.' && seg[1] == '.') {
            char *last = out + w;
            while (last > out) { last--; if (*last == '/' && last > out) break; }
            size_t cut = (last > out && *last == '/') ? (size_t)(last - out) : 0;
            out[cut] = '\0';
            w = cut;
            continue;
        }
        size_t need = w + 1 + sl;
        if (need + 1 > outsz) return -1;
        out[w++] = '/';
        memcpy(out + w, seg, sl);
        w += sl;
        out[w] = '\0';
    }
    if (w == 0) { out[0] = '/'; out[1] = '\0'; }
    return 0;
}

static int path_blocked(const char *abs) {
    size_t pl = strlen(abs);
    for (int i = 0; i < g_block_n; i++) {
        const char *e = g_block[i];
        size_t el = strlen(e);
        if (el == 0) continue;
        if (pl < el) continue;
        if (strncmp(abs, e, el) != 0) continue;
        if (pl == el) return 1;
        if (e[el - 1] == '/') return 1;          /* подкаталог блокируется целиком */
        if (abs[el] == '/') return 1;            /* файл .log -> блокируем и *.log/... (не бывает) */
    }
    return 0;
}

static int gate_deny(const char *norm) {
    if (path_blocked(norm)) return 1;
    char rp[PATH_MAX];
    if (realpath(norm, rp) != NULL && strcmp(rp, norm) != 0) {
        if (path_blocked(rp)) return 1;
    }
    return 0;
}

static int gate_check(const char *path, int dirfd) {
    if (g_block_n < 0) gate_init();
    if (g_block_n == 0) return 0;
    char abs[PATH_MAX];
    if (normalize_abs(path, abs, sizeof(abs)) != 0) return 0;
    if (gate_deny(abs)) {
        return 1;
    }
    /* openat с dirfd: проверяем разрешение через реальную цель fd. */
    if (dirfd != AT_FDCWD && path[0] != '/') {
        char link[64];
        char target[PATH_MAX];
        snprintf(link, sizeof(link), "/proc/self/fd/%d", dirfd);
        ssize_t n = readlink(link, target, sizeof(target) - 1);
        if (n > 0) {
            target[n] = '\0';
            char abs2[PATH_MAX];
            snprintf(abs2, sizeof(abs2), "%s/%s", target, path);
            if (normalize_abs(abs2, abs2, sizeof(abs2)) == 0 && gate_deny(abs2)) return 1;
        }
    }
    return 0;
}

static int (*real_open)(const char *, int, ...) = NULL;
static int (*real_openat)(int, const char *, int, ...) = NULL;

int open(const char *path, int flags, ...) {
    if (gate_check(path, AT_FDCWD) != 0) { errno = EACCES; return -1; }
    if (real_open == NULL) real_open = (int (*)(const char *, int, ...))dlsym(RTLD_NEXT, "open");
    mode_t mode = 0;
    if ((flags & (O_CREAT | O_TMPFILE)) != 0) {
        va_list ap;
        va_start(ap, flags);
        mode = (mode_t)va_arg(ap, int);
        va_end(ap);
    }
    return real_open(path, flags, mode);
}

int open64(const char *path, int flags, ...) {
    if (gate_check(path, AT_FDCWD) != 0) { errno = EACCES; return -1; }
    if (real_open == NULL) real_open = (int (*)(const char *, int, ...))dlsym(RTLD_NEXT, "open64");
    mode_t mode = 0;
    if ((flags & (O_CREAT | O_TMPFILE)) != 0) {
        va_list ap;
        va_start(ap, flags);
        mode = (mode_t)va_arg(ap, int);
        va_end(ap);
    }
    return real_open(path, flags, mode);
}

int openat(int dirfd, const char *path, int flags, ...) {
    if (gate_check(path, dirfd) != 0) { errno = EACCES; return -1; }
    if (real_openat == NULL) real_openat = (int (*)(int, const char *, int, ...))dlsym(RTLD_NEXT, "openat");
    mode_t mode = 0;
    if ((flags & (O_CREAT | O_TMPFILE)) != 0) {
        va_list ap;
        va_start(ap, flags);
        mode = (mode_t)va_arg(ap, int);
        va_end(ap);
    }
    return real_openat(dirfd, path, flags, mode);
}

int openat64(int dirfd, const char *path, int flags, ...) {
    if (gate_check(path, dirfd) != 0) { errno = EACCES; return -1; }
    if (real_openat == NULL) real_openat = (int (*)(int, const char *, int, ...))dlsym(RTLD_NEXT, "openat64");
    mode_t mode = 0;
    if ((flags & (O_CREAT | O_TMPFILE)) != 0) {
        va_list ap;
        va_start(ap, flags);
        mode = (mode_t)va_arg(ap, int);
        va_end(ap);
    }
    return real_openat(dirfd, path, flags, mode);
}

int creat(const char *path, mode_t mode) {
    if (gate_check(path, AT_FDCWD) != 0) { errno = EACCES; return -1; }
    if (real_open == NULL) real_open = (int (*)(const char *, int, ...))dlsym(RTLD_NEXT, "creat");
    return real_open(path, O_WRONLY | O_CREAT | O_TRUNC, mode);
}

int creat64(const char *path, mode_t mode) {
    if (gate_check(path, AT_FDCWD) != 0) { errno = EACCES; return -1; }
    if (real_open == NULL) real_open = (int (*)(const char *, int, ...))dlsym(RTLD_NEXT, "creat64");
    return real_open(path, O_WRONLY | O_CREAT | O_TRUNC, mode);
}