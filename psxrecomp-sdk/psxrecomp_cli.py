#!/usr/bin/env python3
"""Headless disc → generate / rebuild / optional PGO for psxrecomp games.

Commands:
  verify-disc   Hash-check a dump against game.toml [prepare_disc]
  generate      Prepare disc (if needed) + run psxrecomp-game → generated/
  rebuild       cmake --build; if [pgo] enabled, instrument → train → use
  pgo-train     Standalone PGO train (same as rebuild's PGO phase)

Exit codes: 0 ok · 1 runtime · 2 usage · 3 disc verify fail
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tools"))
from sdk_progress import ProgressReporter  # noqa: E402

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_VERIFY = 3


def clamp_future_mtimes(
    root: Path,
    *,
    skip: Optional[Path] = None,
    now: Optional[float] = None,
) -> int:
    """Clamp mtimes ahead of *now* so Ninja does not infinite-reconfigure.

    Release zips often preserve CI clocks that are slightly ahead of a user's
    clock (timezone / skew). Ninja then treats every source as newer than
    ``build.ninja``, re-runs CMake forever, and fails with
    ``manifest 'build.ninja' still dirty after 100 tries``.
    """
    if not root.is_dir():
        return 0
    stamp = time.time() if now is None else now
    skip_res: Optional[Path] = None
    if skip is not None:
        try:
            skip_res = skip.resolve()
        except OSError:
            skip_res = skip
    n = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dpath = Path(dirpath)
        try:
            d_res = dpath.resolve()
        except OSError:
            d_res = dpath
        # Skip the active build tree entirely (outputs are rewritten anyway).
        if skip_res is not None and (
            d_res == skip_res or skip_res in d_res.parents
        ):
            dirnames[:] = []
            continue
        # Never descend into VCS metadata; prune the build dir at the parent.
        pruned: list[str] = []
        for x in dirnames:
            if x == ".git":
                continue
            if skip_res is not None:
                try:
                    if (dpath / x).resolve() == skip_res:
                        continue
                except OSError:
                    pass
            pruned.append(x)
        dirnames[:] = pruned
        for name in filenames:
            p = dpath / name
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if mtime > stamp:
                try:
                    os.utime(p, (stamp, stamp), follow_symlinks=False)
                    n += 1
                except OSError:
                    pass
    return n


def _parse_array_items(inner: str) -> list[Any]:
    items: list[Any] = []
    if not inner.strip():
        return items
    for part in re.split(r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", inner):
        part = part.strip().rstrip(",").strip()
        if not part:
            continue
        if part.startswith('"') and part.endswith('"'):
            items.append(part[1:-1])
        else:
            try:
                items.append(int(part, 0))
            except ValueError:
                items.append(part)
    return items


def parse_toml_simple(text: str) -> dict[str, dict[str, Any]]:
    sections: dict[str, dict[str, Any]] = {"": {}}
    cur = ""
    array_key: Optional[str] = None
    array_buf: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if array_key is not None:
            array_buf.append(line)
            joined = " ".join(array_buf)
            if "]" in joined:
                inner = joined.split("]", 1)[0]
                if inner.startswith("["):
                    inner = inner[1:]
                sections[cur][array_key] = _parse_array_items(inner)
                array_key = None
                array_buf = []
            continue
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = line[1:-1].strip()
            sections.setdefault(cur, {})
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        if val.startswith("["):
            if val.endswith("]") and val.count("[") == val.count("]"):
                sections[cur][key] = _parse_array_items(val[1:-1])
            else:
                array_key = key
                array_buf = [val[1:]]
            continue
        if val.startswith('"') and val.endswith('"'):
            sections[cur][key] = val[1:-1]
        elif val.lower() in ("true", "false"):
            sections[cur][key] = val.lower() == "true"
        else:
            try:
                sections[cur][key] = int(val, 0)
            except ValueError:
                sections[cur][key] = val
    return sections


def file_hashes(path: Path) -> tuple[str, str, int]:
    h_md5, h_sha1 = hashlib.md5(), hashlib.sha1()
    size = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            h_md5.update(chunk)
            h_sha1.update(chunk)
    return h_md5.hexdigest(), h_sha1.hexdigest(), size


def resolve_cue_bin(cue_path: Path) -> Path:
    text = cue_path.read_text(encoding="utf-8", errors="replace")
    m = re.search(r'FILE\s+"([^"]+)"\s+BINARY', text, re.I)
    if not m:
        m = re.search(r"FILE\s+(\S+)\s+BINARY", text, re.I)
    if not m:
        raise ValueError(f"no BINARY FILE in cue: {cue_path}")
    cand = Path(m.group(1))
    if not cand.is_absolute():
        cand = cue_path.parent / cand
    if not cand.is_file():
        raise ValueError(f"cue references missing bin: {cand}")
    return cand


def _find_recompiler_tool(project_root: Path, basename: str, env_name: str) -> Path:
    env = os.environ.get(env_name, "").strip()
    if env:
        p = Path(env).expanduser()
        if p.is_file():
            return p.resolve()
    names = [basename, f"{basename}.exe"]
    search_dirs = [
        project_root / "psxrecomp" / "recompiler" / "build",
        project_root / "psxrecomp" / "recompiler" / "build" / "Release",
        project_root / "build-recompiler",
        ROOT / "recompiler" / "build",
        ROOT / "recompiler" / "build" / "Release",
    ]
    for d in search_dirs:
        for name in names:
            c = d / name
            if c.is_file():
                return c.resolve()
    which = shutil.which(basename)
    if which:
        return Path(which).resolve()
    raise FileNotFoundError(
        f"{basename} not found. Build it:\n"
        "  cmake -S psxrecomp/recompiler -B psxrecomp/recompiler/build -G Ninja\n"
        f"  cmake --build psxrecomp/recompiler/build --target {basename}\n"
        f"Or set {env_name}=/path/to/{basename}"
    )


def find_psxrecomp_game(project_root: Path) -> Path:
    return _find_recompiler_tool(project_root, "psxrecomp-game", "PSXRECOMP_GAME")


def find_psxrecomp_bios(project_root: Path) -> Path:
    return _find_recompiler_tool(project_root, "psxrecomp-bios", "PSXRECOMP_BIOS")


def framework_root(project_root: Path) -> Path:
    """Directory containing bios/*.toml and recompiler/ (usually …/psxrecomp)."""
    cand = project_root / "psxrecomp"
    if (cand / "bios" / "OpenBIOS.toml").is_file() or (
        (cand / "bios").is_dir() and (cand / "recompiler").is_dir()
    ):
        if (cand / "bios").is_dir():
            return cand
    if (ROOT / "bios").is_dir() and (ROOT / "recompiler").is_dir():
        return ROOT
    return cand


def _copy_missing(src: Path, dest: Path) -> None:
    if src.is_dir():
        dest.mkdir(parents=True, exist_ok=True)
        for child in src.iterdir():
            _copy_missing(child, dest / child.name)
        return
    if not src.is_file():
        return
    if dest.is_file():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


def ensure_framework(
    project_root: Path, *, progress: Optional[ProgressReporter] = None
) -> Path:
    """Ensure project_root/psxrecomp has BIOS profiles + seeds for local generate.

    GitHub zipballs omit git submodules, so RetComM source trees often lack
    psxrecomp/bios. Seed from the SDK pack that ships this CLI (ROOT).
    """
    fw = project_root / "psxrecomp"
    need_seed = not (fw / "bios" / "OpenBIOS.toml").is_file()
    if need_seed and (ROOT / "bios").is_dir():
        if progress:
            progress.log(
                f"Seeding psxrecomp BIOS profiles from SDK → {fw}"
            )
        fw.mkdir(parents=True, exist_ok=True)
        _copy_missing(ROOT / "bios", fw / "bios")
        seeds_src = ROOT / "recompiler" / "seeds"
        if seeds_src.is_dir():
            _copy_missing(seeds_src, fw / "recompiler" / "seeds")
        (fw / "recompiler" / "build").mkdir(parents=True, exist_ok=True)
    # psxrecomp-bios walks up from bios/*.toml looking for .gitignore/.git/
    # CMakeLists.txt to find the framework root. Zipball trees lack the
    # submodule .git — plant a marker so rom = "bios/openbios.bin" resolves.
    if (fw / "bios").is_dir() and not any(
        (fw / m).exists() for m in (".gitignore", ".git", "CMakeLists.txt")
    ):
        marker = fw / ".gitignore"
        if not marker.is_file():
            marker.write_text(
                "# RetComM SDK seed marker (project-root for psxrecomp-bios)\n",
                encoding="utf-8",
            )
    return framework_root(project_root)


def bios_backend_present(fw: Path, stem: str) -> bool:
    dispatch = fw / "generated" / f"{stem}_dispatch.c"
    full = fw / "generated" / f"{stem}_full.c"
    if not dispatch.is_file() or not full.is_file():
        return False
    try:
        text = dispatch.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return f"{stem}_psx_bios_backend" in text


def regen_bios_profile(
    project_root: Path,
    profile_rel: str,
    *,
    progress: ProgressReporter,
) -> None:
    fw = framework_root(project_root)
    profile = fw / profile_rel
    if not profile.is_file():
        raise FileNotFoundError(f"BIOS profile not found: {profile}")
    bios_tool = find_psxrecomp_bios(project_root)
    progress.log(f"regen BIOS via {bios_tool.name} --config {profile_rel}")
    (fw / "generated").mkdir(parents=True, exist_ok=True)
    # Pass a framework-relative config path and cwd=fw so toml paths like
    # rom = "bios/openbios.bin" resolve under the framework root (not bios/bios/).
    proc = subprocess.run(
        [str(bios_tool), "--config", profile_rel],
        cwd=str(fw),
        capture_output=True,
        text=True,
    )
    for stream in (proc.stdout, proc.stderr):
        if not stream:
            continue
        for line in stream.splitlines():
            if line.strip():
                progress.log(line)
    if proc.returncode != 0:
        raise RuntimeError(
            f"psxrecomp-bios failed for {profile_rel} (exit {proc.returncode})"
        )


def load_sections(config: Path) -> dict[str, dict[str, Any]]:
    return parse_toml_simple(config.read_text(encoding="utf-8"))


def verify_disc_path(
    disc: Path,
    prep: dict[str, Any],
    *,
    skip_hash: bool,
    progress: ProgressReporter,
) -> dict[str, Any]:
    path = disc.resolve()
    if path.suffix.lower() == ".cue":
        path = resolve_cue_bin(path)
    md5, sha1, size = file_hashes(path)
    identity = {
        "path": str(path),
        "md5": md5,
        "sha1": sha1,
        "size": size,
        "verified": False,
    }
    progress.event("disc", **identity)
    sizes = [int(s) for s in (prep.get("known_sizes") or [])]
    md5s = [str(x).lower() for x in (prep.get("known_md5") or [])]
    sha1s = [str(x).lower() for x in (prep.get("known_sha1") or [])]
    if not md5s and not sha1s and not sizes:
        identity["verified"] = True
        return identity
    if skip_hash:
        return identity
    ok = (md5 in md5s) or (sha1 in sha1s)
    if not ok and sizes and size in sizes and not md5s and not sha1s:
        ok = True
    if not ok:
        raise DiscVerifyError(
            f"disc digests not in prepare_disc.known_* "
            f"(size={size} md5={md5} sha1={sha1})"
        )
    identity["verified"] = True
    return identity


class DiscVerifyError(Exception):
    pass


def cmd_verify_disc(args: argparse.Namespace, progress: ProgressReporter) -> int:
    config = Path(args.config).expanduser().resolve()
    if not config.is_file():
        progress.error(f"config not found: {config}", code=EXIT_USAGE)
        return EXIT_USAGE
    project_root = (
        Path(args.project_root).expanduser().resolve()
        if args.project_root
        else config.parent
    )
    disc = Path(args.disc).expanduser()
    if not disc.is_absolute():
        disc = (project_root / disc).resolve()
    else:
        disc = disc.resolve()
    if not disc.is_file():
        progress.error(f"disc not found: {disc}", code=EXIT_USAGE)
        return EXIT_USAGE
    secs = load_sections(config)
    prep = secs.get("prepare_disc") or {}
    progress.phase("verify", pct=0.1, message=f"Verifying {disc.name}")
    try:
        identity = verify_disc_path(
            disc, prep, skip_hash=bool(args.skip_hash_check), progress=progress
        )
    except DiscVerifyError as exc:
        progress.error(str(exc), code=EXIT_VERIFY, verify_failed=True)
        return EXIT_VERIFY
    progress.phase("done", pct=1.0, message="Disc OK")
    progress.result(ok=True, **identity)
    return EXIT_OK


def run_prepare_disc(
    project_root: Path,
    config: Path,
    source: Path,
    progress: ProgressReporter,
) -> Path:
    script = ROOT / "tools" / "prepare_disc.py"
    if not script.is_file():
        raise RuntimeError(f"missing {script}")
    progress.phase("prepare_disc", pct=0.15, message="Normalizing disc image…")
    cmd = [
        sys.executable,
        str(script),
        "--config",
        str(config),
        "--project-root",
        str(project_root),
        str(source),
    ]
    proc = subprocess.run(cmd, cwd=str(project_root), capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    for line in out.splitlines():
        if line.strip():
            progress.log(line)
    if proc.returncode != 0:
        raise RuntimeError(f"prepare_disc failed (exit {proc.returncode})")
    marker = "RESULT_CUE="
    cue = None
    for line in out.splitlines():
        if line.startswith(marker):
            cue = Path(line[len(marker) :].strip())
    if not cue or not cue.is_file():
        raise RuntimeError("prepare_disc did not print RESULT_CUE=")
    return cue.resolve()


def cmd_generate(args: argparse.Namespace, progress: ProgressReporter) -> int:
    config = Path(args.config).expanduser().resolve()
    if not config.is_file():
        progress.error(f"config not found: {config}", code=EXIT_USAGE)
        return EXIT_USAGE
    project_root = (
        Path(args.project_root).expanduser().resolve()
        if args.project_root
        else config.parent
    )
    secs = load_sections(config)
    game = secs.get("game") or {}
    prep = secs.get("prepare_disc") or {}
    recomp = secs.get("recompiler") or {}
    runtime = secs.get("runtime") or {}
    openbios_allowed = bool(runtime.get("openbios", True))

    disc_arg = args.disc
    if not disc_arg:
        disc_arg = game.get("disc") or ""
    if not disc_arg:
        progress.error("no --disc and game.disc empty", code=EXIT_USAGE)
        return EXIT_USAGE
    disc = Path(str(disc_arg)).expanduser()
    if not disc.is_absolute():
        disc = (project_root / disc).resolve()
    else:
        disc = disc.resolve()

    progress.phase("verify", pct=0.05, message=f"Checking disc {disc.name}")
    try:
        if disc.is_file():
            verify_disc_path(
                disc, prep, skip_hash=bool(args.skip_hash_check), progress=progress
            )
    except DiscVerifyError as exc:
        progress.error(str(exc), code=EXIT_VERIFY, verify_failed=True)
        return EXIT_VERIFY

    boot = str(prep.get("boot_exe") or Path(str(game.get("exe") or "")).name)
    out_rel = str(prep.get("out_dir") or "prepared_disc")
    boot_path = project_root / out_rel / boot
    working_disc = disc
    # Normalize library dumps when boot EXE missing or source looks like ISO/raw.
    need_prep = (not boot_path.is_file()) or disc.suffix.lower() in (
        ".iso",
        ".ISO",
    )
    if need_prep or args.force_prepare:
        try:
            working_disc = run_prepare_disc(project_root, config, disc, progress)
        except Exception as exc:  # noqa: BLE001
            progress.error(str(exc), code=EXIT_ERROR)
            return EXIT_ERROR
    if not boot_path.is_file():
        progress.error(f"boot EXE missing after prepare: {boot_path}", code=EXIT_ERROR)
        return EXIT_ERROR

    # ---- BIOS backends (local only; CI ships none) ----
    fw = ensure_framework(project_root, progress=progress)
    bios_arg = (getattr(args, "bios", None) or "").strip()
    staged_retail = False
    if bios_arg:
        bios_path = Path(bios_arg).expanduser()
        if not bios_path.is_absolute():
            bios_path = (project_root / bios_path).resolve()
        else:
            bios_path = bios_path.resolve()
        if not bios_path.is_file():
            progress.error(f"BIOS not found: {bios_path}", code=EXIT_USAGE)
            return EXIT_USAGE
        dest = fw / "bios" / "SCPH1001.BIN"
        progress.phase("bios", pct=0.15, message="Staging retail BIOS dump…")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.resolve() != bios_path.resolve():
                shutil.copy2(bios_path, dest)
        except OSError as exc:
            progress.error(f"failed to stage BIOS: {exc}", code=EXIT_ERROR)
            return EXIT_ERROR
        try:
            progress.phase("bios", pct=0.2, message="Generating SCPH1001 BIOS C…")
            regen_bios_profile(project_root, "bios/SCPH1001.toml", progress=progress)
            staged_retail = True
        except Exception as exc:  # noqa: BLE001
            progress.error(str(exc), code=EXIT_ERROR)
            return EXIT_ERROR
    elif not openbios_allowed and not bios_backend_present(fw, "SCPH1001"):
        progress.error(
            "This title requires a retail BIOS dump. Pass --bios SCPH1001.BIN "
            "(or pick one in the setup wizard).",
            code=EXIT_USAGE,
        )
        return EXIT_USAGE

    if openbios_allowed and (
        args.force_bios or not bios_backend_present(fw, "OpenBIOS")
    ):
        try:
            progress.phase(
                "bios", pct=0.25, message="Generating OpenBIOS backend C…"
            )
            regen_bios_profile(project_root, "bios/OpenBIOS.toml", progress=progress)
        except Exception as exc:  # noqa: BLE001
            # Prefer retail when --bios was given; OpenBIOS is then best-effort.
            if staged_retail:
                progress.log(f"OpenBIOS regen skipped (retail BIOS ok): {exc}")
            else:
                progress.error(str(exc), code=EXIT_ERROR)
                return EXIT_ERROR

    try:
        game_tool = find_psxrecomp_game(project_root)
    except FileNotFoundError as exc:
        progress.error(str(exc), code=EXIT_ERROR)
        return EXIT_ERROR

    out_dir = project_root / str(recomp.get("out_dir") or "generated")
    progress.phase(
        "emit",
        pct=0.35,
        message=f"Running psxrecomp-game → {out_dir}",
    )
    cmd = [str(game_tool), "--config", str(config)]
    proc = subprocess.run(
        cmd,
        cwd=str(project_root),
        capture_output=True,
        text=True,
    )
    for stream in (proc.stdout, proc.stderr):
        if not stream:
            continue
        for line in stream.splitlines():
            if line.strip():
                progress.log(line)
    if proc.returncode != 0:
        progress.error(
            f"psxrecomp-game failed (exit {proc.returncode})", code=EXIT_ERROR
        )
        return EXIT_ERROR

    marker_name = args.gen_marker or f"{boot}_dispatch.c"
    marker = out_dir / marker_name
    if not marker.is_file():
        # Accept any *_dispatch.c
        hits = list(out_dir.glob("*_dispatch.c"))
        if not hits:
            progress.error(f"generate produced no dispatch under {out_dir}", code=EXIT_ERROR)
            return EXIT_ERROR
        marker = hits[0]

    progress.phase("done", pct=1.0, message="Generate complete")
    progress.result(
        ok=True,
        out_dir=str(out_dir),
        marker=str(marker),
        disc=str(working_disc),
        boot_exe=str(boot_path),
    )
    return EXIT_OK


def _cmake_configure(
    project_root: Path,
    build_dir: Path,
    *,
    pgo: str,
    extra: list[str],
    progress: ProgressReporter,
) -> None:
    build_dir.mkdir(parents=True, exist_ok=True)
    # Prefer Ninja when available; fall back so hosts without ninja still build.
    if not (build_dir / "CMakeCache.txt").is_file() and shutil.which("ninja"):
        gen = ["-G", "Ninja"]
    else:
        gen = []
    cmd = [
        "cmake",
        "-S",
        str(project_root),
        "-B",
        str(build_dir),
        *gen,
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DPSX_PGO={pgo}",
        *extra,
    ]
    progress.log(" ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(project_root), capture_output=True, text=True)
    for stream in (proc.stdout, proc.stderr):
        if stream:
            for line in stream.splitlines():
                if line.strip():
                    progress.log(line)
    if proc.returncode != 0:
        raise RuntimeError(f"cmake configure failed (exit {proc.returncode})")


def _cmake_build(
    build_dir: Path,
    target: str,
    progress: ProgressReporter,
) -> None:
    jobs = os.environ.get("CMAKE_BUILD_PARALLEL_LEVEL") or str(
        os.cpu_count() or 4
    )
    cmd = [
        "cmake",
        "--build",
        str(build_dir),
        "--parallel",
        jobs,
        "--target",
        target,
    ]
    progress.log(" ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    for stream in (proc.stdout, proc.stderr):
        if stream:
            for line in stream.splitlines():
                if line.strip():
                    progress.log(line)
    if proc.returncode != 0:
        err = f"cmake --build failed (exit {proc.returncode})"
        combined = "\n".join(
            s for s in (proc.stdout or "", proc.stderr or "") if s
        )
        if "still dirty after" in combined:
            err += (
                " — Ninja reconfigure loop (often release-zip mtimes ahead of "
                "the system clock). Re-run after the CLI clamps future mtimes, "
                "or touch the project tree / fix the clock."
            )
        raise RuntimeError(err)


def _soft_stop(pid: int, timeout: int = 30) -> None:
    try:
        os.kill(pid, 15)  # SIGTERM
    except ProcessLookupError:
        return
    for _ in range(timeout):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(1)
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass


def _pgo_train_warning(*, hide_video: bool) -> str:
    if hide_video:
        return (
            "WARNING: PGO training is running (no video window). Do not cancel "
            "or kill this process until training finishes — interrupting may "
            "corrupt profiles and force a restart of this step."
        )
    return (
        "WARNING: PGO training is running. Do not close the game window or "
        "interact with the application until training finishes — closing or "
        "clicking may corrupt profiles and force a restart of this step."
    )


def run_pgo_train(
    project_root: Path,
    build_dir: Path,
    *,
    exe_basename: str,
    disc: Path,
    config: Path,
    train_secs: int,
    train_runs: int,
    mute_host_audio: bool,
    hide_video: bool,
    progress: ProgressReporter,
) -> None:
    exe = build_dir / exe_basename
    if not exe.is_file():
        exe = build_dir / f"{exe_basename}.exe"
    if not exe.is_file():
        raise RuntimeError(f"train binary missing: {exe_basename} under {build_dir}")

    pgo_dir = build_dir / "pgo"
    if pgo_dir.exists():
        shutil.rmtree(pgo_dir)
    pgo_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.setdefault("PSX_BIOS_HLE", "0")
    env["LLVM_PROFILE_FILE"] = str(pgo_dir / "bpe-%p.profraw")
    # Discard SDL output; SPU/CD still advance (profiles stay valid).
    if mute_host_audio:
        env["PSX_HOST_MUTE"] = "1"
    else:
        env.pop("PSX_HOST_MUTE", None)
    # No on-screen FMV (photosensitive / abrasive content). Guest MDEC/GPU
    # still run; host present is skipped. Matches prior CI train shape.
    if hide_video:
        env["PSX_HEADLESS"] = "1"
        env.setdefault("SDL_VIDEODRIVER", "dummy")
    else:
        env.pop("PSX_HEADLESS", None)

    warning = _pgo_train_warning(hide_video=hide_video)
    progress.phase("pgo_train", pct=0.35, message=warning)
    # Always surface on the console even when JSONL owns stdout.
    print(f"\n*** {warning} ***\n", file=sys.stderr, flush=True)
    if mute_host_audio:
        progress.log("Host audio muted for train (PSX_HOST_MUTE=1; SPU still runs).")
    else:
        progress.log("Host audio unmuted for train (speakers will play).")
    if hide_video:
        progress.log(
            "Video hidden for train (--headless / PSX_HEADLESS=1; "
            "guest FMV decode still runs, nothing shown on screen)."
        )
    else:
        progress.log("Video window visible for train (host present paths included).")

    for run in range(1, train_runs + 1):
        progress.phase(
            "pgo_train",
            pct=0.4 + 0.4 * (run - 1) / max(train_runs, 1),
            message=(
                f"WARNING: PGO train run {run}/{train_runs} ({train_secs}s) — "
                + (
                    "Do not cancel this process."
                    if hide_video
                    else "Do not close or interact with the application."
                )
            ),
        )
        cmd = [
            str(exe),
            "--no-launcher",
            "--game",
            str(config),
            "--disc",
            str(disc),
        ]
        if hide_video:
            cmd.append("--headless")
        proc = subprocess.Popen(
            cmd,
            cwd=str(project_root),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(train_secs)
        if proc.poll() is None:
            _soft_stop(proc.pid)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    n_gcda = len(list(build_dir.rglob("*.gcda")))
    n_raw = len(list(pgo_dir.glob("*.profraw")))
    progress.log(f"PGO profiles: {n_gcda} .gcda, {n_raw} .profraw")
    if n_raw >= 1:
        merge = shutil.which("llvm-profdata")
        if not merge:
            # macOS xcrun
            try:
                r = subprocess.run(
                    ["xcrun", "--find", "llvm-profdata"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if r.returncode == 0 and r.stdout.strip():
                    merge = "xcrun"
            except OSError:
                pass
        raws = list(pgo_dir.glob("*.profraw"))
        out = pgo_dir / "default.profdata"
        if merge == "xcrun":
            cmd = ["xcrun", "llvm-profdata", "merge", "-sparse", *[str(p) for p in raws], "-o", str(out)]
        elif merge:
            cmd = [merge, "merge", "-sparse", *[str(p) for p in raws], "-o", str(out)]
        else:
            raise RuntimeError("llvm-profdata not found (Clang PGO merge)")
        subprocess.run(cmd, check=True)
    if n_gcda < 1 and not (pgo_dir / "default.profdata").is_file():
        raise RuntimeError("no PGO profiles written — train did not flush")


def cmd_rebuild(args: argparse.Namespace, progress: ProgressReporter) -> int:
    config = Path(args.config).expanduser().resolve()
    if not config.is_file():
        progress.error(f"config not found: {config}", code=EXIT_USAGE)
        return EXIT_USAGE
    project_root = (
        Path(args.project_root).expanduser().resolve()
        if args.project_root
        else config.parent
    )
    build_dir = Path(args.build_dir).expanduser()
    if not build_dir.is_absolute():
        build_dir = (project_root / build_dir).resolve()
    else:
        build_dir = build_dir.resolve()
    target = args.target or "psx-runtime"
    exe_basename = args.exe_basename or "psx-runtime"

    secs = load_sections(config)
    pgo = secs.get("pgo") or {}
    # Opt-in only: requires game.toml [pgo] enabled = true (or --force-pgo).
    pgo_enabled = bool(pgo.get("enabled", False)) and not args.no_pgo
    if args.force_pgo:
        pgo_enabled = True

    game = secs.get("game") or {}
    prep = secs.get("prepare_disc") or {}
    if args.disc:
        disc = Path(args.disc).expanduser()
        if not disc.is_absolute():
            disc = (project_root / disc).resolve()
        else:
            disc = disc.resolve()
    else:
        cue_name = prep.get("cue_name")
        if cue_name:
            disc = (
                project_root / str(prep.get("out_dir") or "bpe") / str(cue_name)
            ).resolve()
        elif game.get("disc"):
            disc = (project_root / str(game["disc"])).resolve()
            if disc.suffix.lower() == ".bin":
                cue = disc.with_suffix(".cue")
                if cue.is_file():
                    disc = cue
        else:
            disc = None

    cmake_extra = []
    if args.cmake_extra:
        cmake_extra.extend(args.cmake_extra)
    # Full playable link after local generate (not the CI setup-host shape).
    cmake_extra.append("-DBPE_FORCE_SETUP_HOST=OFF")
    cmake_extra.append("-DPSXRECOMP_ALLOW_NO_BIOS=OFF")

    clamped = clamp_future_mtimes(project_root, skip=build_dir)
    if clamped:
        progress.log(
            f"Clamped {clamped} future mtime(s) under {project_root} "
            "(avoids Ninja dirty-manifest loop from release-zip clocks)."
        )

    # mute_host_audio / hide_video: default ON; game.toml / CLI can disable.
    mute_host = True
    if "mute_host_audio" in pgo:
        mute_host = bool(pgo.get("mute_host_audio"))
    if getattr(args, "pgo_audio", False):
        mute_host = False
    if getattr(args, "pgo_mute", False):
        mute_host = True

    hide_video = True
    if "hide_video" in pgo:
        hide_video = bool(pgo.get("hide_video"))
    if getattr(args, "pgo_video", False):
        hide_video = False
    if getattr(args, "pgo_hide_video", False):
        hide_video = True

    try:
        if not pgo_enabled:
            progress.phase("build", pct=0.2, message="cmake Release build…")
            _cmake_configure(
                project_root, build_dir, pgo="", extra=cmake_extra, progress=progress
            )
            _cmake_build(build_dir, target, progress)
        else:
            # Framework defaults (60×2); game.toml / CLI may lengthen for hard titles.
            train_secs = int(getattr(args, "train_secs", 0) or 0)
            if train_secs <= 0:
                train_secs = int(pgo.get("train_secs") or 60)
            train_runs = int(getattr(args, "train_runs", 0) or 0)
            if train_runs <= 0:
                train_runs = int(pgo.get("train_runs") or 2)
            progress.phase(
                "pgo_instrument",
                pct=0.1,
                message="PGO instrumented build…",
            )
            _cmake_configure(
                project_root,
                build_dir,
                pgo="generate",
                extra=cmake_extra,
                progress=progress,
            )
            _cmake_build(build_dir, target, progress)
            if not disc or not Path(disc).is_file():
                raise RuntimeError(
                    f"PGO train needs a playable disc (got {disc!s}). "
                    "Pass --disc or set game.disc / prepare_disc.cue_name."
                )
            run_pgo_train(
                project_root,
                build_dir,
                exe_basename=exe_basename,
                disc=Path(disc),
                config=config,
                train_secs=train_secs,
                train_runs=train_runs,
                mute_host_audio=mute_host,
                hide_video=hide_video,
                progress=progress,
            )
            progress.phase("pgo_use", pct=0.85, message="PGO optimized rebuild…")
            _cmake_configure(
                project_root,
                build_dir,
                pgo="use",
                extra=cmake_extra,
                progress=progress,
            )
            _cmake_build(build_dir, target, progress)
    except Exception as exc:  # noqa: BLE001
        progress.error(str(exc), code=EXIT_ERROR)
        return EXIT_ERROR

    exe = build_dir / exe_basename
    if not exe.is_file():
        exe = build_dir / f"{exe_basename}.exe"
    progress.phase("done", pct=1.0, message="Rebuild complete")
    progress.result(ok=True, exe=str(exe), pgo=pgo_enabled)
    return EXIT_OK


def cmd_pgo_train(args: argparse.Namespace, progress: ProgressReporter) -> int:
    args.force_pgo = True
    args.no_pgo = False
    return cmd_rebuild(args, progress)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="psxrecomp_cli", description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", default="game.toml", help="game.toml path")
        p.add_argument("--project-root", default="", help="game project root")
        p.add_argument("--json-progress", action="store_true")

    v = sub.add_parser("verify-disc", help="verify disc digests")
    add_common(v)
    v.add_argument("--disc", required=True)
    v.add_argument("--skip-hash-check", action="store_true")
    v.set_defaults(handler=cmd_verify_disc)

    g = sub.add_parser(
        "generate", help="regen BIOS backends + prepare disc + generate game C"
    )
    add_common(g)
    g.add_argument("--disc", default="", help="source dump or working cue/bin")
    g.add_argument(
        "--bios",
        default="",
        help="optional retail BIOS dump (staged as bios/SCPH1001.BIN + regen)",
    )
    g.add_argument(
        "--force-bios",
        action="store_true",
        help="regenerate OpenBIOS even if generated/ backends already exist",
    )
    g.add_argument("--skip-hash-check", action="store_true")
    g.add_argument("--force-prepare", action="store_true")
    g.add_argument("--gen-marker", default="", help="expected marker under out_dir")
    g.set_defaults(handler=cmd_generate)

    r = sub.add_parser("rebuild", help="cmake build (+ optional local PGO)")
    add_common(r)
    r.add_argument("--build-dir", required=True)
    r.add_argument("--target", default="psx-runtime")
    r.add_argument("--exe-basename", default="")
    r.add_argument("--disc", default="", help="disc for PGO train")
    r.add_argument(
        "--no-pgo",
        action="store_true",
        help="skip PGO even if game.toml [pgo] enabled=true",
    )
    r.add_argument(
        "--force-pgo",
        action="store_true",
        help="run PGO even without [pgo] enabled=true",
    )
    r.add_argument(
        "--train-secs",
        type=int,
        default=0,
        help="override [pgo] train_secs (default framework 60)",
    )
    r.add_argument(
        "--train-runs",
        type=int,
        default=0,
        help="override [pgo] train_runs (default framework 2)",
    )
    r.add_argument(
        "--pgo-mute",
        action="store_true",
        help="force mute host speakers during train (default)",
    )
    r.add_argument(
        "--pgo-audio",
        action="store_true",
        help="allow host speakers during train (overrides mute default)",
    )
    r.add_argument(
        "--pgo-hide-video",
        action="store_true",
        help="force headless train with no on-screen video (default)",
    )
    r.add_argument(
        "--pgo-video",
        action="store_true",
        help="show a game window during train (includes host present paths)",
    )
    r.add_argument("--cmake-extra", action="append", default=[])
    r.set_defaults(handler=cmd_rebuild)

    p = sub.add_parser("pgo-train", help="force PGO rebuild+train+use")
    add_common(p)
    p.add_argument("--build-dir", required=True)
    p.add_argument("--target", default="psx-runtime")
    p.add_argument("--exe-basename", default="")
    p.add_argument("--disc", default="")
    p.add_argument("--train-secs", type=int, default=0)
    p.add_argument("--train-runs", type=int, default=0)
    p.add_argument("--pgo-mute", action="store_true")
    p.add_argument("--pgo-audio", action="store_true")
    p.add_argument("--pgo-hide-video", action="store_true")
    p.add_argument("--pgo-video", action="store_true")
    p.add_argument("--cmake-extra", action="append", default=[])
    p.add_argument("--no-pgo", action="store_false", default=False)
    p.add_argument("--force-pgo", action="store_true", default=True)
    p.set_defaults(handler=cmd_pgo_train)

    return ap


def main() -> int:
    args = build_parser().parse_args()
    progress = ProgressReporter(json_progress=bool(getattr(args, "json_progress", False)))
    try:
        return int(args.handler(args, progress))
    except BrokenPipeError:
        return EXIT_ERROR
    except KeyboardInterrupt:
        progress.error("interrupted", code=EXIT_ERROR)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
