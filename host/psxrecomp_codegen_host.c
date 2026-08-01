/* Portable generate → rebuild (--no-pgo from setup) → relaunch host. */

#include "psxrecomp_codegen_host.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#if defined(_WIN32)
#  include <windows.h>
#else
#  include <dirent.h>
#  include <errno.h>
#  include <fcntl.h>
#  include <spawn.h>
#  include <sys/stat.h>
#  include <sys/wait.h>
#  include <unistd.h>
extern char** environ;
#endif

static const PsxrecompCodegenHostConfig* g_cfg;
static char g_project_root[1024];
static char g_cli_path[1100];
static char g_game_toml[1100];
static char g_python[512];
static char g_cmake[512];
static char g_build_dir[1100];
static char g_exe_path[1100];
static char g_helper_path[1100];
static char g_cmake_target[256];
static char g_exe_basename[256];
static char g_display[128];
static char g_toolchain_bin[1100];
static int g_ready;
static int g_relaunch_is_helper;

static const char* cfg_or(const char* v, const char* d) {
    return (v && v[0]) ? v : d;
}

static int path_is_file(const char* path) {
#if defined(_WIN32)
    DWORD attr = GetFileAttributesA(path);
    return attr != INVALID_FILE_ATTRIBUTES &&
           (attr & FILE_ATTRIBUTE_DIRECTORY) == 0;
#else
    struct stat st;
    return stat(path, &st) == 0 && S_ISREG(st.st_mode);
#endif
}

static int path_is_dir(const char* path) {
#if defined(_WIN32)
    DWORD attr = GetFileAttributesA(path);
    return attr != INVALID_FILE_ATTRIBUTES &&
           (attr & FILE_ATTRIBUTE_DIRECTORY) != 0;
#else
    struct stat st;
    return stat(path, &st) == 0 && S_ISDIR(st.st_mode);
#endif
}

static int join_path(char* out, size_t cap, const char* a, const char* b) {
    size_t na = strlen(a);
    int need_slash = na > 0 && a[na - 1] != '/' && a[na - 1] != '\\';
    int n = snprintf(out, cap, "%s%s%s", a, need_slash ? "/" : "", b);
    return n > 0 && (size_t)n < cap;
}

static int dirname_copy(char* out, size_t cap, const char* path) {
    size_t n = strlen(path);
    while (n > 0 && (path[n - 1] == '/' || path[n - 1] == '\\'))
        --n;
    while (n > 0 && path[n - 1] != '/' && path[n - 1] != '\\')
        --n;
    while (n > 0 && (path[n - 1] == '/' || path[n - 1] == '\\'))
        --n;
    if (n == 0) {
        if (cap < 2) return 0;
        out[0] = '.';
        out[1] = '\0';
        return 1;
    }
    if (n >= cap) return 0;
    memcpy(out, path, n);
    out[n] = '\0';
    return 1;
}

static int resolve_cli_path(const char* root, char* out, size_t cap) {
    const char* candidates[] = {
        cfg_or(g_cfg->psxrecomp_cli_relpath, "psxrecomp/psxrecomp_cli.py"),
        "psxrecomp/psxrecomp_cli.py",
        "psxrecomp-sdk/psxrecomp_cli.py",
    };
    for (size_t i = 0; i < sizeof(candidates) / sizeof(candidates[0]); ++i) {
        if (!candidates[i] || !candidates[i][0])
            continue;
        if (!join_path(out, cap, root, candidates[i]))
            continue;
        if (path_is_file(out))
            return 1;
    }
    return 0;
}

static int looks_like_project_root(const char* root) {
    char cli[1100], toml[1100];
    if (!join_path(toml, sizeof(toml), root,
                   cfg_or(g_cfg->seed_cfg_relpath, "game.toml")))
        return 0;
    if (!path_is_file(toml))
        return 0;
    return resolve_cli_path(root, cli, sizeof(cli));
}

static int find_on_path(const char* name, char* out, size_t cap) {
#if defined(_WIN32)
    char cmd[640];
    snprintf(cmd, sizeof(cmd), "where %s >nul 2>nul", name);
    if (system(cmd) == 0) {
        snprintf(out, cap, "%s", name);
        return 1;
    }
#else
    char cmd[640];
    snprintf(cmd, sizeof(cmd), "command -v %s >/dev/null 2>&1", name);
    if (system(cmd) == 0) {
        snprintf(out, cap, "%s", name);
        return 1;
    }
#endif
    return 0;
}

static int find_python(char* out, size_t cap) {
    const char* env = getenv("PYTHON");
    if (env && env[0] && path_is_file(env)) {
        snprintf(out, cap, "%s", env);
        return 1;
    }
#if defined(_WIN32)
    const char* candidates[] = {"python.exe", "python3.exe", "py.exe"};
#else
    const char* candidates[] = {"python3", "python"};
#endif
    for (size_t i = 0; i < sizeof(candidates) / sizeof(candidates[0]); ++i) {
        if (find_on_path(candidates[i], out, cap))
            return 1;
    }
    return 0;
}

static int resolve_toolchain_bin(char* out, size_t cap) {
    char cand[1100], cmake[1200];
    if (!g_project_root[0])
        return 0;
    if (!join_path(cand, sizeof(cand), g_project_root, "toolchain/bin"))
        return 0;
#if defined(_WIN32)
    if (join_path(cmake, sizeof(cmake), cand, "cmake.exe") && path_is_file(cmake)) {
        snprintf(out, cap, "%s", cand);
        return 1;
    }
#else
    if (join_path(cmake, sizeof(cmake), cand, "cmake") && path_is_file(cmake)) {
        snprintf(out, cap, "%s", cand);
        return 1;
    }
#endif
    /* Nested pack root: toolchain/<name>/bin */
    char wrap[1100];
    if (!join_path(wrap, sizeof(wrap), g_project_root, "toolchain"))
        return 0;
    if (!path_is_dir(wrap))
        return 0;
#if defined(_WIN32)
    WIN32_FIND_DATAA fd;
    char pattern[1200];
    snprintf(pattern, sizeof(pattern), "%s\\*", wrap);
    HANDLE h = FindFirstFileA(pattern, &fd);
    if (h == INVALID_HANDLE_VALUE)
        return 0;
    int found = 0;
    do {
        if (!(fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY))
            continue;
        if (fd.cFileName[0] == '.')
            continue;
        char nested[1100], nbin[1100];
        if (!join_path(nested, sizeof(nested), wrap, fd.cFileName))
            continue;
        if (!join_path(nbin, sizeof(nbin), nested, "bin"))
            continue;
        if (join_path(cmake, sizeof(cmake), nbin, "cmake.exe") && path_is_file(cmake)) {
            snprintf(out, cap, "%s", nbin);
            found = 1;
            break;
        }
    } while (FindNextFileA(h, &fd));
    FindClose(h);
    return found;
#else
    DIR* dir = opendir(wrap);
    if (!dir)
        return 0;
    int found = 0;
    struct dirent* ent;
    while ((ent = readdir(dir)) != NULL) {
        if (ent->d_name[0] == '.')
            continue;
        char nested[1100], nbin[1100];
        if (!join_path(nested, sizeof(nested), wrap, ent->d_name))
            continue;
        if (!path_is_dir(nested))
            continue;
        if (!join_path(nbin, sizeof(nbin), nested, "bin"))
            continue;
        if (join_path(cmake, sizeof(cmake), nbin, "cmake") && path_is_file(cmake)) {
            snprintf(out, cap, "%s", nbin);
            found = 1;
            break;
        }
    }
    closedir(dir);
    return found;
#endif
}

static void activate_toolchain_path(void) {
    g_toolchain_bin[0] = '\0';
    if (!resolve_toolchain_bin(g_toolchain_bin, sizeof(g_toolchain_bin)))
        return;
    const char* old = getenv("PATH");
#if defined(_WIN32)
    char neu[8192];
    snprintf(neu, sizeof(neu), "%s%s%s", g_toolchain_bin, old ? ";" : "",
             old ? old : "");
    _putenv_s("PATH", neu);
#else
    char neu[8192];
    snprintf(neu, sizeof(neu), "%s%s%s", g_toolchain_bin, old ? ":" : "",
             old ? old : "");
    setenv("PATH", neu, 1);
#endif
}

static int find_cmake(char* out, size_t cap) {
    const char* env = getenv("CMAKE");
    if (env && env[0] && path_is_file(env)) {
        snprintf(out, cap, "%s", env);
        return 1;
    }
    char tc[1100], cand[1200];
    if (resolve_toolchain_bin(tc, sizeof(tc))) {
#if defined(_WIN32)
        if (join_path(cand, sizeof(cand), tc, "cmake.exe") && path_is_file(cand)) {
            snprintf(out, cap, "%s", cand);
            return 1;
        }
#else
        if (join_path(cand, sizeof(cand), tc, "cmake") && path_is_file(cand)) {
            snprintf(out, cap, "%s", cand);
            return 1;
        }
#endif
    }
#if defined(_WIN32)
    return find_on_path("cmake.exe", out, cap);
#else
    return find_on_path("cmake", out, cap);
#endif
}

static int discover_project_root(char* out, size_t cap) {
    const char* env_name =
        cfg_or(g_cfg->project_root_env, "PSXRECOMP_PROJECT_ROOT");
    const char* env = getenv(env_name);
    if (env && env[0] && looks_like_project_root(env)) {
        snprintf(out, cap, "%s", env);
        return 1;
    }

    char start[1024];
#if defined(_WIN32)
    if (!GetCurrentDirectoryA((DWORD)sizeof(start), start))
        start[0] = '\0';
#else
    if (!getcwd(start, sizeof(start)))
        start[0] = '\0';
#endif

    char cur[1024];
    snprintf(cur, sizeof(cur), "%s", start[0] ? start : ".");
    for (int i = 0; i < 10; ++i) {
        if (looks_like_project_root(cur)) {
            snprintf(out, cap, "%s", cur);
            return 1;
        }
        char parent[1024];
        if (!dirname_copy(parent, sizeof(parent), cur))
            break;
        if (strcmp(parent, cur) == 0)
            break;
        snprintf(cur, sizeof(cur), "%s", parent);
    }
    return 0;
}

static int resolve_build_paths(void) {
    const char* env_name =
        cfg_or(g_cfg->build_dir_env, "PSXRECOMP_BUILD_DIR");
    const char* env = getenv(env_name);
    if (env && env[0]) {
        snprintf(g_build_dir, sizeof(g_build_dir), "%s", env);
    } else {
        const char* names[] = {
            cfg_or(g_cfg->build_dir_name, "build"),
            "build-release",
            "build",
            "build-ci",
        };
        int found = 0;
        for (size_t i = 0; i < sizeof(names) / sizeof(names[0]); ++i) {
            if (!names[i] || !names[i][0])
                continue;
            char cand[1100];
            if (!join_path(cand, sizeof(cand), g_project_root, names[i]))
                continue;
            if (path_is_dir(cand)) {
                snprintf(g_build_dir, sizeof(g_build_dir), "%s", cand);
                found = 1;
                break;
            }
        }
        /* Setup zips ship without a pre-made build tree. Prefer the configured
         * name so rebuild can cmake -B it on first Generate & rebuild. */
        if (!found) {
            const char* prefer = cfg_or(g_cfg->build_dir_name, "build-release");
            if (!join_path(g_build_dir, sizeof(g_build_dir), g_project_root,
                           prefer))
                return 0;
        }
    }

    char exe_name[300];
#if defined(_WIN32)
    snprintf(exe_name, sizeof(exe_name), "%s.exe", g_exe_basename);
#else
    snprintf(exe_name, sizeof(exe_name), "%s", g_exe_basename);
#endif
    return join_path(g_exe_path, sizeof(g_exe_path), g_build_dir, exe_name);
}

static int bios_backends_missing(void) {
    char openbios[1100], scph[1100];
    if (!join_path(openbios, sizeof(openbios), g_project_root,
                   "psxrecomp/generated/OpenBIOS_dispatch.c"))
        return 1;
    if (!join_path(scph, sizeof(scph), g_project_root,
                   "psxrecomp/generated/SCPH1001_dispatch.c"))
        return 1;
    return !(path_is_file(openbios) || path_is_file(scph));
}

int psxrecomp_codegen_host_sources_missing(
    const PsxrecompCodegenHostConfig* cfg) {
    if (!cfg || !cfg->cmake_target || !cfg->exe_basename)
        return 0;
    g_cfg = cfg;
    if (!g_project_root[0] &&
        !discover_project_root(g_project_root, sizeof(g_project_root)))
        return 0;
    char marker[1100];
    if (!join_path(marker, sizeof(marker), g_project_root,
                   cfg_or(cfg->gen_marker_relpath,
                          "generated/SLUS_011.89_dispatch.c")))
        return 1;
    if (!path_is_file(marker))
        return 1;
    return bios_backends_missing();
}

static int read_line_file(const char* path, char* out, size_t cap) {
    FILE* f = fopen(path, "r");
    if (!f) return 0;
    if (!fgets(out, (int)cap, f)) {
        fclose(f);
        return 0;
    }
    fclose(f);
    size_t n = strlen(out);
    while (n && (out[n - 1] == '\n' || out[n - 1] == '\r'))
        out[--n] = '\0';
    return n > 0;
}

static int resolve_bios_arg(char* out, size_t cap) {
    char cand[1100];
    if (join_path(cand, sizeof(cand), g_project_root, "bios.cfg") &&
        read_line_file(cand, out, cap) && path_is_file(out))
        return 1;
    if (read_line_file("bios.cfg", out, cap) && path_is_file(out))
        return 1;
    out[0] = '\0';
    return 0;
}

static int json_get_string(const char* line, const char* key, char* out,
                           size_t out_cap) {
    char pattern[96];
    snprintf(pattern, sizeof(pattern), "\"%s\":\"", key);
    const char* p = strstr(line, pattern);
    if (!p) return 0;
    p += strlen(pattern);
    size_t i = 0;
    while (*p && *p != '"' && i + 1 < out_cap) {
        if (*p == '\\' && p[1]) {
            ++p;
            out[i++] = *p++;
            continue;
        }
        out[i++] = *p++;
    }
    out[i] = '\0';
    return i > 0;
}

static int json_get_number(const char* line, const char* key, double* out) {
    char pattern[96];
    snprintf(pattern, sizeof(pattern), "\"%s\":", key);
    const char* p = strstr(line, pattern);
    if (!p) return 0;
    p += strlen(pattern);
    while (*p == ' ') ++p;
    char* end = NULL;
    double v = strtod(p, &end);
    if (end == p) return 0;
    *out = v;
    return 1;
}

static void handle_progress_line(const char* line,
                                 RecompLauncherCPrepareProgressFn on_progress,
                                 void* progress_ctx) {
    if (!line || line[0] != '{' || !on_progress) return;
    char event[64] = "";
    json_get_string(line, "event", event, sizeof(event));
    if (strcmp(event, "phase") == 0) {
        char message[240] = "";
        char phase[64] = "";
        double pct = -1.0;
        json_get_string(line, "message", message, sizeof(message));
        json_get_string(line, "phase", phase, sizeof(phase));
        if (!json_get_number(line, "pct", &pct))
            pct = -1.0;
        if (!message[0] && phase[0])
            snprintf(message, sizeof(message), "%s", phase);
        on_progress(progress_ctx, (float)pct, message[0] ? message : NULL);
    } else if (strcmp(event, "log") == 0 || strcmp(event, "error") == 0) {
        char message[240] = "";
        if (json_get_string(line, "message", message, sizeof(message)))
            on_progress(progress_ctx, -1.0f, message);
    }
}

#if defined(_WIN32)
static int run_cli_win(const char* cmdline,
                       RecompLauncherCPrepareProgressFn on_progress,
                       void* progress_ctx, char* err_msg, size_t err_cap,
                       const char* fail_label) {
    SECURITY_ATTRIBUTES sa;
    memset(&sa, 0, sizeof(sa));
    sa.nLength = sizeof(sa);
    sa.bInheritHandle = TRUE;
    HANDLE rd = NULL, wr = NULL;
    if (!CreatePipe(&rd, &wr, &sa, 0)) {
        snprintf(err_msg, err_cap, "CreatePipe failed.");
        return 0;
    }
    SetHandleInformation(rd, HANDLE_FLAG_INHERIT, 0);

    STARTUPINFOA si;
    PROCESS_INFORMATION pi;
    memset(&si, 0, sizeof(si));
    memset(&pi, 0, sizeof(pi));
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdOutput = wr;
    si.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    si.hStdInput = GetStdHandle(STD_INPUT_HANDLE);

    char mutable_cmd[4096];
    snprintf(mutable_cmd, sizeof(mutable_cmd), "%s", cmdline);
    if (!CreateProcessA(NULL, mutable_cmd, NULL, NULL, TRUE, 0, NULL,
                        g_project_root, &si, &pi)) {
        CloseHandle(rd);
        CloseHandle(wr);
        snprintf(err_msg, err_cap, "Failed to spawn %s.", fail_label);
        return 0;
    }
    CloseHandle(wr);

    char buf[512];
    char line[1024];
    size_t line_len = 0;
    DWORD n = 0;
    while (ReadFile(rd, buf, sizeof(buf), &n, NULL) && n > 0) {
        for (DWORD i = 0; i < n; ++i) {
            char c = buf[i];
            if (c == '\r') continue;
            if (c == '\n') {
                line[line_len] = '\0';
                handle_progress_line(line, on_progress, progress_ctx);
                line_len = 0;
                continue;
            }
            if (line_len + 1 < sizeof(line))
                line[line_len++] = c;
        }
    }
    if (line_len) {
        line[line_len] = '\0';
        handle_progress_line(line, on_progress, progress_ctx);
    }
    CloseHandle(rd);
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    if (code == 0) return 1;
    if (code == 3)
        snprintf(err_msg, err_cap, "Disc verification failed (wrong dump).");
    else
        snprintf(err_msg, err_cap, "%s failed (exit %lu).", fail_label,
                 (unsigned long)code);
    return 0;
}
#else
static int run_cli_posix(char* const argv[],
                         RecompLauncherCPrepareProgressFn on_progress,
                         void* progress_ctx, char* err_msg, size_t err_cap,
                         const char* fail_label) {
    int pipefd[2];
    if (pipe(pipefd) != 0) {
        snprintf(err_msg, err_cap, "pipe() failed: %s", strerror(errno));
        return 0;
    }

    posix_spawn_file_actions_t actions;
    posix_spawn_file_actions_init(&actions);
    posix_spawn_file_actions_addclose(&actions, pipefd[0]);
    posix_spawn_file_actions_adddup2(&actions, pipefd[1], STDOUT_FILENO);
    posix_spawn_file_actions_addclose(&actions, pipefd[1]);

    pid_t pid = 0;
    int rc = posix_spawnp(&pid, argv[0], &actions, NULL, argv, environ);
    posix_spawn_file_actions_destroy(&actions);
    close(pipefd[1]);
    if (rc != 0) {
        close(pipefd[0]);
        snprintf(err_msg, err_cap, "Failed to spawn %s: %s", fail_label,
                 strerror(rc));
        return 0;
    }

    FILE* out = fdopen(pipefd[0], "r");
    if (!out) {
        close(pipefd[0]);
        waitpid(pid, NULL, 0);
        snprintf(err_msg, err_cap, "fdopen failed.");
        return 0;
    }
    char line[1024];
    while (fgets(line, sizeof(line), out)) {
        size_t n = strlen(line);
        while (n && (line[n - 1] == '\n' || line[n - 1] == '\r'))
            line[--n] = '\0';
        handle_progress_line(line, on_progress, progress_ctx);
    }
    fclose(out);

    int status = 0;
    if (waitpid(pid, &status, 0) < 0) {
        snprintf(err_msg, err_cap, "waitpid failed: %s", strerror(errno));
        return 0;
    }
    int code = WIFEXITED(status) ? WEXITSTATUS(status) : 1;
    if (code == 0) return 1;
    if (code == 3)
        snprintf(err_msg, err_cap, "Disc verification failed (wrong dump).");
    else
        snprintf(err_msg, err_cap, "%s failed (exit %d).", fail_label, code);
    return 0;
}
#endif

static int host_prepare_generate(const char* source_path, char* out_path,
                                 size_t out_cap, char* err_msg, size_t err_cap,
                                 RecompLauncherCPrepareProgressFn on_progress,
                                 void* progress_ctx) {
    if (!g_ready) {
        snprintf(err_msg, err_cap, "Local codegen tools are not available.");
        return 0;
    }
    if (!source_path || !source_path[0]) {
        snprintf(err_msg, err_cap, "No disc selected.");
        return 0;
    }
    activate_toolchain_path();
    if (on_progress)
        on_progress(progress_ctx, 0.02f, "Starting psxrecomp generate…");

    char bios_path[1100];
    const int have_bios = resolve_bios_arg(bios_path, sizeof(bios_path));

#if defined(_WIN32)
    char cmdline[4096];
    if (have_bios) {
        snprintf(cmdline, sizeof(cmdline),
                 "\"%s\" \"%s\" generate --project-root \"%s\" --config \"%s\" "
                 "--disc \"%s\" --bios \"%s\" --json-progress",
                 g_python, g_cli_path, g_project_root, g_game_toml, source_path,
                 bios_path);
    } else {
        snprintf(cmdline, sizeof(cmdline),
                 "\"%s\" \"%s\" generate --project-root \"%s\" --config \"%s\" "
                 "--disc \"%s\" --json-progress",
                 g_python, g_cli_path, g_project_root, g_game_toml, source_path);
    }
    if (!run_cli_win(cmdline, on_progress, progress_ctx, err_msg, err_cap,
                     "psxrecomp generate"))
        return 0;
#else
    char* argv[16];
    int argc = 0;
    argv[argc++] = g_python;
    argv[argc++] = g_cli_path;
    argv[argc++] = "generate";
    argv[argc++] = "--project-root";
    argv[argc++] = g_project_root;
    argv[argc++] = "--config";
    argv[argc++] = g_game_toml;
    argv[argc++] = "--disc";
    argv[argc++] = (char*)source_path;
    if (have_bios) {
        argv[argc++] = "--bios";
        argv[argc++] = bios_path;
    }
    argv[argc++] = "--json-progress";
    argv[argc] = NULL;
    if (!run_cli_posix(argv, on_progress, progress_ctx, err_msg, err_cap,
                       "psxrecomp generate"))
        return 0;
#endif

    snprintf(out_path, out_cap, "%s", source_path);
    if (on_progress)
        on_progress(progress_ctx, 1.0f, "Generate complete");
    return 1;
}

#if defined(_WIN32)
static void bat_write_set(FILE* f, const char* name, const char* value) {
    fprintf(f, "set \"%s=", name);
    for (const char* p = value; *p; ++p) {
        if (*p == '%')
            fputc('%', f);
        fputc(*p, f);
    }
    fprintf(f, "\"\r\n");
}

static int write_windows_deferred_rebuild_helper(char* err_msg, size_t err_cap) {
    if (!join_path(g_helper_path, sizeof(g_helper_path), g_build_dir,
                   "recomp_deferred_rebuild.cmd")) {
        snprintf(err_msg, err_cap, "Failed to form helper path.");
        return 0;
    }
    FILE* f = fopen(g_helper_path, "wb");
    if (!f) {
        snprintf(err_msg, err_cap, "Failed to write rebuild helper: %s",
                 g_helper_path);
        return 0;
    }
    char pid_buf[32];
    snprintf(pid_buf, sizeof(pid_buf), "%lu",
             (unsigned long)GetCurrentProcessId());
    fprintf(f, "@echo off\r\n");
    fprintf(f, "setlocal EnableExtensions\r\n");
    fprintf(f, "title %s - rebuilding\r\n", g_display);
    bat_write_set(f, "PARENT_PID", pid_buf);
    bat_write_set(f, "PYTHON", g_python);
    bat_write_set(f, "CLI", g_cli_path);
    bat_write_set(f, "ROOT", g_project_root);
    bat_write_set(f, "CONFIG", g_game_toml);
    bat_write_set(f, "BUILD_DIR", g_build_dir);
    bat_write_set(f, "TARGET", g_cmake_target);
    bat_write_set(f, "EXE_BASE", g_exe_basename);
    bat_write_set(f, "EXE", g_exe_path);
    bat_write_set(f, "DISPLAY", g_display);
    if (g_toolchain_bin[0])
        bat_write_set(f, "TC_BIN", g_toolchain_bin);
    fprintf(f,
            "echo Waiting for %%DISPLAY%% to exit...\r\n"
            ":waitloop\r\n"
            "tasklist /FI \"PID eq %%PARENT_PID%%\" 2>NUL | "
            "findstr /I \"%%PARENT_PID%%\" >NUL\r\n"
            "if not errorlevel 1 (\r\n"
            "  ping -n 2 127.0.0.1 >NUL\r\n"
            "  goto waitloop\r\n"
            ")\r\n"
            "echo Building...\r\n"
            "cd /d \"%%ROOT%%\"\r\n"
            "if defined TC_BIN set \"PATH=%%TC_BIN%%;%%PATH%%\"\r\n"
            "\"%%PYTHON%%\" \"%%CLI%%\" rebuild --project-root \"%%ROOT%%\" "
            "--config \"%%CONFIG%%\" --build-dir \"%%BUILD_DIR%%\" "
            "--target \"%%TARGET%%\" --exe-basename \"%%EXE_BASE%%\" "
            "--no-pgo --prune-after toolchain,build-intermediates\r\n"
            "if errorlevel 1 (\r\n"
            "  echo.\r\n"
            "  echo Build failed. Fix the errors above, then rebuild manually.\r\n"
            "  pause\r\n"
            "  exit /b 1\r\n"
            ")\r\n"
            "echo Starting %%DISPLAY%%...\r\n"
            "start \"\" /D \"%%ROOT%%\" \"%%EXE%%\" --launcher\r\n"
            "endlocal\r\n");
    fclose(f);
    return 1;
}
#endif

static int host_rebuild_game(const char* disc_path, char* out_exe_path,
                             size_t out_cap, char* err_msg, size_t err_cap,
                             RecompLauncherCPrepareProgressFn on_progress,
                             void* progress_ctx) {
    g_relaunch_is_helper = 0;
    if (!g_ready || !g_build_dir[0]) {
        snprintf(err_msg, err_cap, "CMake build environment is not available.");
        return 0;
    }

#if defined(_WIN32)
    (void)disc_path;
    (void)g_cmake;
    activate_toolchain_path();
    if (on_progress)
        on_progress(progress_ctx, 0.4f,
                    "Scheduling Windows rebuild after exit…");
    if (!write_windows_deferred_rebuild_helper(err_msg, err_cap))
        return 0;
    g_relaunch_is_helper = 1;
    snprintf(out_exe_path, out_cap, "%s", g_helper_path);
    if (on_progress)
        on_progress(progress_ctx, 1.0f,
                    "Exiting so Windows can rebuild safely…");
    return 1;
#else
    if (on_progress)
        on_progress(progress_ctx, 0.05f, "Starting rebuild (cmake)…");

    activate_toolchain_path();

    char disc_arg_storage[1100];
    char* argv[36];
    int argc = 0;
    argv[argc++] = g_python;
    argv[argc++] = g_cli_path;
    argv[argc++] = "rebuild";
    argv[argc++] = "--project-root";
    argv[argc++] = g_project_root;
    argv[argc++] = "--config";
    argv[argc++] = g_game_toml;
    argv[argc++] = "--build-dir";
    argv[argc++] = g_build_dir;
    argv[argc++] = "--target";
    argv[argc++] = g_cmake_target;
    argv[argc++] = "--exe-basename";
    argv[argc++] = g_exe_basename;
    if (disc_path && disc_path[0]) {
        snprintf(disc_arg_storage, sizeof(disc_arg_storage), "%s", disc_path);
        argv[argc++] = "--disc";
        argv[argc++] = disc_arg_storage;
    }
    argv[argc++] = "--no-pgo";
    argv[argc++] = "--prune-after";
    argv[argc++] = "toolchain,build-intermediates";
    argv[argc++] = "--json-progress";
    argv[argc] = NULL;

    if (!run_cli_posix(argv, on_progress, progress_ctx, err_msg, err_cap,
                       "psxrecomp rebuild"))
        return 0;
    if (!path_is_file(g_exe_path)) {
        snprintf(err_msg, err_cap, "Build succeeded but binary missing: %s",
                 g_exe_path);
        return 0;
    }
    snprintf(out_exe_path, out_cap, "%s", g_exe_path);
    if (on_progress)
        on_progress(progress_ctx, 1.0f, "Build complete");
    return 1;
#endif
}

void psxrecomp_codegen_host_relaunch_or_exit(const char* disc_path) {
    char exe[512];
    if (!recomp_launcher_relaunch_exe(exe, sizeof(exe)) || !exe[0]) {
        fprintf(stderr, "psxrecomp-codegen: relaunch requested but no path\n");
        exit(1);
    }
    if (disc_path && disc_path[0]) {
        FILE* rc = fopen("disc.cfg", "w");
        if (rc) {
            fprintf(rc, "%s\n", disc_path);
            fclose(rc);
        }
    }

#if defined(_WIN32)
    {
        STARTUPINFOA si;
        PROCESS_INFORMATION pi;
        char cmd[1536];
        DWORD flags = 0;
        memset(&si, 0, sizeof(si));
        memset(&pi, 0, sizeof(pi));
        si.cb = sizeof(si);
        if (g_relaunch_is_helper) {
            fprintf(stderr,
                    "psxrecomp-codegen: starting deferred rebuild helper\n");
            snprintf(cmd, sizeof(cmd), "cmd.exe /C \"%s\"", exe);
            flags = CREATE_NEW_CONSOLE;
        } else {
            fprintf(stderr, "psxrecomp-codegen: relaunching %s\n", exe);
            snprintf(cmd, sizeof(cmd), "\"%s\" --launcher", exe);
        }
        if (!CreateProcessA(NULL, cmd, NULL, NULL, FALSE, flags, NULL,
                            g_project_root, &si, &pi)) {
            fprintf(stderr, "psxrecomp-codegen: CreateProcess failed\n");
            exit(1);
        }
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        ExitProcess(0);
    }
#else
    {
        fprintf(stderr, "psxrecomp-codegen: relaunching %s\n", exe);
        char* args[] = {exe, "--launcher", NULL};
        execv(exe, args);
        perror("psxrecomp-codegen: execv failed");
        exit(1);
    }
#endif
}

void psxrecomp_codegen_host_apply(RecompLauncherCGameInfo* gi,
                                  const PsxrecompCodegenHostConfig* cfg) {
    if (!gi || !cfg || !cfg->cmake_target || !cfg->exe_basename)
        return;

    g_cfg = cfg;
    g_ready = 0;
    g_relaunch_is_helper = 0;
    g_project_root[0] = '\0';
    g_cli_path[0] = '\0';
    g_game_toml[0] = '\0';
    g_python[0] = '\0';
    g_cmake[0] = '\0';
    g_build_dir[0] = '\0';
    g_exe_path[0] = '\0';
    g_helper_path[0] = '\0';
    g_toolchain_bin[0] = '\0';

    snprintf(g_display, sizeof(g_display), "%s",
             cfg_or(cfg->display_name, "Game"));
    snprintf(g_cmake_target, sizeof(g_cmake_target), "%s", cfg->cmake_target);
    snprintf(g_exe_basename, sizeof(g_exe_basename), "%s", cfg->exe_basename);

    const char* force_env =
        cfg_or(cfg->force_setup_env, "PSXRECOMP_FORCE_SETUP");
    const char* force = getenv(force_env);
    const int force_setup = force && force[0] && force[0] != '0';

    if (!discover_project_root(g_project_root, sizeof(g_project_root))) {
        /* Still force the wizard when generated/ is missing — discover may
         * fail if the process cwd is unrelated to the project tree. */
        if (force_setup) {
            gi->needs_setup = 1;
            gi->prepare_required_before_continue = 1;
        }
        return;
    }
    if (!resolve_cli_path(g_project_root, g_cli_path, sizeof(g_cli_path))) {
        if (psxrecomp_codegen_host_sources_missing(cfg) || force_setup) {
            gi->needs_setup = 1;
            gi->prepare_required_before_continue = 1;
        }
        return;
    }
    if (!join_path(g_game_toml, sizeof(g_game_toml), g_project_root,
                   cfg_or(cfg->game_toml_relpath, "game.toml")))
        return;
    if (!path_is_file(g_game_toml))
        return;
    if (!find_python(g_python, sizeof(g_python))) {
        if (psxrecomp_codegen_host_sources_missing(cfg) || force_setup) {
            gi->needs_setup = 1;
            gi->prepare_required_before_continue = 1;
        }
        return;
    }

    g_ready = 1;
    activate_toolchain_path();
    gi->prepare_with_progress = host_prepare_generate;
    gi->prepare_use_selected_rom = 1;
    /* Number prefix is applied in the setup UI (BIOS adds a step). */
    gi->prepare_section_title = "Generate BIOS + game C & rebuild";
    gi->prepare_busy_status = "Generating BIOS + game sources…";
    gi->prepare_success_status = "Sources ready — building…";

    const int can_rebuild = find_cmake(g_cmake, sizeof(g_cmake)) &&
                            resolve_build_paths();
    if (can_rebuild) {
        gi->prepare_disc_label = "Generate & rebuild…";
#if defined(_WIN32)
        gi->prepare_disc_note =
            cfg->prepare_note_windows
                ? cfg->prepare_note_windows
                : "Uses your disc with the local psxrecomp SDK to regenerate "
                  "generated/, then quits and rebuilds via a helper so the "
                  "running .exe is not locked.";
        gi->rebuild_busy_status = "Scheduling rebuild…";
        gi->rebuild_success_status =
            "Exiting for Windows rebuild — a console will finish the build…";
#else
        gi->prepare_disc_note =
            cfg->prepare_note
                ? cfg->prepare_note
                : "Uses your disc with the local psxrecomp SDK to regenerate "
                  "generated/, then runs cmake --build and restarts.";
        gi->rebuild_busy_status = "Building…";
        gi->rebuild_success_status = "Build complete — restarting…";
#endif
        gi->rebuild_with_progress = host_rebuild_game;
        gi->rebuild_after_prepare = 1;
        gi->relaunch_after_rebuild = 1;
    } else {
        gi->prepare_disc_label = "Generate sources…";
        gi->prepare_disc_note =
            cfg->prepare_note_no_cmake
                ? cfg->prepare_note_no_cmake
                : "Regenerates generated/ with the local psxrecomp SDK. "
                  "CMake/build dir not found — rebuild manually, then relaunch.";
        gi->prepare_success_status =
            "Sources generated. Rebuild manually, then relaunch.";
    }

    if (psxrecomp_codegen_host_sources_missing(cfg) || force_setup) {
        gi->needs_setup = 1;
        gi->prepare_required_before_continue = 1;
    }
}
