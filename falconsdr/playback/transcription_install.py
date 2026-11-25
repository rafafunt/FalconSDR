"""Installs the optional transcription extra (faster-whisper, + GPU libs if
requested: NVIDIA's CUDA wheels, or for an AMD card CTranslate2's ROCm build
and AMD's ROCm runtime wheels - see _install_rocm) from inside the running
app - see worker.py, which runs it on whichever computer was asked to (Settings → Transcription & computers).
pip installing this can take a while (ctranslate2/av/tokenizers are
sizeable downloads).

1. ``pip install`` into the running interpreter - no restart needed.
2. Only on pip's "externally-managed-environment" error (PEP 668): install
   into ``venv_paths.VENV_DIR`` (``--system-site-packages``), which main.py
   re-execs into - this needs a restart.
Any other error is reported as-is.

A packaged (PyInstaller) build has no Python of its own to pip into, but it
carries pip: ``falconsdr.exe --pip ...`` (main.py) runs it, installing
prebuilt wheels into ``ADDONS_DIR`` in the user's profile, which
``enable_addons()`` puts on sys.path - no restart, no system Python needed.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional


from ..venv_paths import VENV_DIR, VENV_PYTHON
from .config import APP_DATA_DIR

# Per Python version: wheels with compiled code only fit the interpreter they were built for.
ADDONS_DIR = APP_DATA_DIR / "addons" / f"cp{sys.version_info[0]}{sys.version_info[1]}"
# Left by uninstall_transcription() when Windows still had the files loaded.
_REMOVE_MARKER = ADDONS_DIR.with_name(ADDONS_DIR.name + ".remove")
# What uninstalling removes - faster-whisper's own heavy parts; small shared
# helpers (huggingface_hub, numpy...) stay, other programs may use them.
# AMD (ROCm): CTranslate2's own ROCm build (one zip per OS on its GitHub release,
# every Python version inside) + AMD's ROCm runtime as pip wheels, one index per
# GPU family. Only the families that build was compiled for (`strings
# libctranslate2*.so | grep gfx`). Bump a version and its hashes together.
_CT2_ROCM_VERSION = "4.9.1"
_CT2_ROCM_ZIPS = {  # sys.platform -> (asset, SHA-256, wheel platform tag)
    "linux": ("rocm-python-wheels-Linux.zip",
              "283c6800475961a13120f8ad2761a00ebe645aac3c155a6794f6d784b1111593",
              "manylinux_2_27_x86_64.manylinux_2_28_x86_64"),
    "win32": ("rocm-python-wheels-Windows.zip",
              "59ea928a2219dde5f9fdf3bb63fc340e751103467f73d75a4e1cdec9e1e9863f", "win_amd64"),
}
_ROCM_SDK_VERSION = "7.13.0"
_ROCM_INDEX = "https://repo.amd.com/rocm/whl/{family}/"
_ROCM_FAMILIES = {
    "gfx1030": "gfx103X-all", "gfx1031": "gfx103X-all", "gfx1032": "gfx103X-all", "gfx1035": "gfx103X-all",
    "gfx1100": "gfx110X-all", "gfx1101": "gfx110X-all", "gfx1102": "gfx110X-all", "gfx1103": "gfx110X-all",
    "gfx1150": "gfx1150", "gfx1151": "gfx1151", "gfx1200": "gfx120X-all", "gfx1201": "gfx120X-all",
}
# shortcut: Windows has no KFD to ask for the gfx target, so it goes by the
# marketing name - extend when AMD ships new cards (untested on real Windows).
_ROCM_WINDOWS_NAMES = [
    (r"RX 9\d{3}|AI PRO R9\d{3}", "gfx120X-all"),
    (r"RX 7\d{3}|PRO W7\d{3}|7[468]0M", "gfx110X-all"),
    (r"RX 6[6-9]\d{2}|PRO W6[6-9]\d{2}|6[68]0M", "gfx103X-all"),
    (r"8[89]0M", "gfx1150"),
    (r"80[456]0S", "gfx1151"),
]
_UNINSTALL_PACKAGES = ["faster-whisper", "ctranslate2", "av", "onnxruntime", "tokenizers",
                       "nvidia-cublas-cu12", "nvidia-cudnn-cu12", "nvidia-cuda-nvrtc-cu12", "rocm-sdk-core",
                       *sorted({f"rocm-sdk-libraries-{f.lower()}" for f in _ROCM_FAMILIES.values()})]


def _loaded() -> bool:
    """Has transcription run in this process? Windows then keeps its .pyd files locked."""
    return any(m in sys.modules for m in ("faster_whisper", "ctranslate2", "av", "onnxruntime", "tokenizers"))


def _remove_addons() -> bool:
    """True once ADDONS_DIR is gone. Windows refuses to delete a .pyd another
    FalconSDR process still has loaded - rmtree then removes everything else,
    leaving packages that import but are broken."""
    shutil.rmtree(ADDONS_DIR, ignore_errors=True)
    return not ADDONS_DIR.exists()


def enable_addons() -> None:
    """Packaged build: make the extras install_transcription() put in
    ADDONS_DIR importable. Appended, so the build's own numpy etc. always win
    over the copies pip also places there. Created up front - a sys.path
    entry that doesn't exist yet stays invisible after a later install."""
    if _REMOVE_MARKER.exists():  # an uninstall that had to wait for this restart
        # The process this restart replaced may still be exiting and holding its .pyd files.
        for _ in range(10):
            if _remove_addons():
                break
            time.sleep(0.5)
        else:
            return  # still locked: keep the half-removed packages off sys.path, retry next start
        _REMOVE_MARKER.unlink(missing_ok=True)
    ADDONS_DIR.mkdir(parents=True, exist_ok=True)
    if str(ADDONS_DIR) not in sys.path:
        sys.path.append(str(ADDONS_DIR))


def removal_pending() -> bool:
    """An uninstall is waiting for FalconSDR to restart (packaged build only)."""
    return _REMOVE_MARKER.exists()


# av<19: faster-whisper 1.2.1 passes av.open(metadata_errors=...), which PyAV
# 19 removed - every transcription failed. The wheels are abi3, no source build.
_PACKAGES = ["faster-whisper>=1.2", "av<19"]
_GPU_PACKAGES = ["nvidia-cublas-cu12", "nvidia-cudnn-cu12"]
_EXTERNALLY_MANAGED_MARKER = "externally-managed-environment"


def _run_streamed(cmd: list[str], on_line: Callable[[str], None]) -> tuple[bool, str]:
    on_line("$ " + " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        on_line(str(exc))
        return False, str(exc)
    lines: list[str] = []
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            on_line(line)
    finally:
        proc.stdout.close()
    proc.wait()
    return proc.returncode == 0, "\n".join(lines)


def _hf_cache_dir() -> Path:
    """Hugging Face's own cache location rules (huggingface_hub.constants), without importing it."""
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    home = os.environ.get("HF_HOME") or Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "huggingface"
    return Path(home) / "hub"


# faster_whisper.utils._MODELS (1.2.1) where it isn't "Systran/faster-whisper-<name>".
_MODEL_REPOS = {
    "large": "Systran/faster-whisper-large-v3",
    "distil-large-v2": "Systran/faster-distil-whisper-large-v2",
    "distil-medium.en": "Systran/faster-distil-whisper-medium.en",
    "distil-small.en": "Systran/faster-distil-whisper-small.en",
    "distil-large-v3": "Systran/faster-distil-whisper-large-v3",
    "distil-large-v3.5": "distil-whisper/distil-large-v3.5-ct2",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}


def _model_folder(model_id: str) -> Path:
    repo = model_id if "/" in model_id else _MODEL_REPOS.get(model_id, f"Systran/faster-whisper-{model_id}")
    return _hf_cache_dir() / ("models--" + repo.replace("/", "--"))


def model_cached(model_id: str) -> bool:
    """True when the model's weights are on this computer already (a folder given by path always is)."""
    if Path(model_id).is_dir():
        return True
    return any(_model_folder(model_id).glob("snapshots/*/model.bin"))


def model_download_bytes(model_id: str) -> int:
    """How much of a model is on disk so far - what a download in progress has fetched."""
    try:
        return sum(f.stat().st_size for f in _model_folder(model_id).rglob("*") if f.is_file() and not f.is_symlink())
    except OSError:
        return 0


def delete_models(model_ids: list[str], on_line: Callable[[str], None]) -> None:
    """Deletes the given Whisper models (faster-whisper names like "large-v3"
    or Hugging Face repo ids) from the Hugging Face cache - only these: the
    cache is shared with any other program on this computer. Never imports
    faster-whisper: an uninstall is often what fixes a broken one."""
    if "faster_whisper" in sys.modules:
        from .transcription_engine import unload_models
        unload_models()  # loaded models keep their files open
    cache = _hf_cache_dir().resolve()
    for model_id in model_ids:
        if not model_id or "\\" in model_id or ".." in model_id or Path(model_id).is_dir():
            on_line(f"model {model_id}: skipped (not a Hugging Face cache id)")
            continue
        repo = model_id if "/" in model_id else _MODEL_REPOS.get(model_id, f"Systran/faster-whisper-{model_id}")
        folder = (cache / ("models--" + repo.replace("/", "--"))).resolve()
        if cache not in folder.parents:
            on_line(f"model {repo}: skipped (outside cache)")
            continue
        if not folder.exists():
            on_line(f"model {repo}: not downloaded")
            continue
        size = 0
        for f in folder.rglob("*"):
            try:
                if f.is_file() and not f.is_symlink():
                    size += f.stat().st_size
            except OSError:
                pass
        shutil.rmtree(folder, ignore_errors=True)
        if folder.exists():
            on_line(f"model {repo}: some files are in use - delete {folder} by hand")
        else:
            on_line(f"deleted model {repo} ({size / 1e9:.1f} GB)")


def uninstall_transcription(model_ids: list[str], on_line: Callable[[str], None]) -> tuple[bool, bool, str]:
    """Removes what install_transcription() added, plus the given models
    (empty list = keep them). Returns (success, needs_restart, error_message)."""
    if model_ids:
        try:
            delete_models(model_ids, on_line)
        except Exception as exc:  # models are a bonus - never block removing the packages
            on_line(f"models: could not delete ({exc})")
    if getattr(sys, "frozen", False):
        if _loaded():
            _REMOVE_MARKER.parent.mkdir(parents=True, exist_ok=True)
            _REMOVE_MARKER.touch()
            on_line("files in use - they are removed when FalconSDR next starts")
            return True, True, ""
        if not _remove_addons():
            _REMOVE_MARKER.touch()
            on_line("some files are in use - they are removed when FalconSDR next starts")
            return True, True, ""
        ADDONS_DIR.mkdir(parents=True, exist_ok=True)  # stays on sys.path for a later install
        return True, False, ""
    if sys.platform == "win32" and _loaded():
        message = ("Transcription has run in this session and Windows keeps its files locked - "
                   'turn off "Transcribe here", restart FalconSDR, then uninstall.')
        on_line(message)
        return False, False, message
    python = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
    ok, output = _run_streamed([python, "-m", "pip", "uninstall", "-y", *_UNINSTALL_PACKAGES], on_line)
    return ok, False, "" if ok else output


def _gfx_target(version: int) -> str:
    """KFD's gfx_target_version (120001) -> "gfx1201"."""
    return f"gfx{version // 10000}{version // 100 % 100}{version % 100:x}"


def amd_gpu() -> Optional[tuple[str, str]]:
    """(name for the UI, AMD's wheel-index family) of an AMD card the ROCm
    build of CTranslate2 supports, else None."""
    if sys.platform.startswith("linux"):
        if not os.access("/dev/kfd", os.R_OK | os.W_OK):  # no ROCm access for this user (render group)
            return None
        for props in sorted(Path("/sys/class/kfd/kfd/topology/nodes").glob("*/properties")):
            try:
                fields = dict(line.split(" ", 1) for line in props.read_text().splitlines() if " " in line)
                gfx = _gfx_target(int(fields.get("gfx_target_version", 0)))
            except (OSError, ValueError):
                continue
            if gfx in _ROCM_FAMILIES:
                return f"AMD Radeon ({gfx})", _ROCM_FAMILIES[gfx]
        return None
    if sys.platform == "win32":
        from ..audio_transcode import system_env
        try:
            powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            out = subprocess.run([str(powershell), "-NoProfile", "-Command", "(Get-CimInstance Win32_VideoController).Name"],
                                 capture_output=True, text=True, errors="replace", timeout=15, env=system_env(),
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        for name in (n.strip() for n in out.splitlines()):
            for pattern, family in _ROCM_WINDOWS_NAMES:
                if "AMD" in name.upper() and re.search(pattern, name, re.IGNORECASE):
                    return name, family
    return None


def _install_rocm(pip: list[str], family: str, on_line: Callable[[str], None]) -> tuple[bool, str]:
    """After faster-whisper is in: swaps ctranslate2 for its ROCm build and
    adds AMD's ROCm runtime. ``pip`` is the "pip install" command line for the
    target (same Python version as this process: the venv is made from it)."""
    from ..digital_voice import _urlopen  # the certificate fallbacks of a packaged build
    asset, sha256, platform_tag = _CT2_ROCM_ZIPS["win32" if sys.platform == "win32" else "linux"]
    tag = f"cp{sys.version_info[0]}{sys.version_info[1]}"
    wheel = f"ctranslate2-{_CT2_ROCM_VERSION}-{tag}-{tag}-{platform_tag}.whl"
    work = APP_DATA_DIR / "rocm-download"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        url = f"https://github.com/OpenNMT/CTranslate2/releases/download/v{_CT2_ROCM_VERSION}/{asset}"
        on_line(f"downloading {url}")
        digest = hashlib.sha256()
        with _urlopen(url) as resp, open(work / asset, "wb") as out:
            while chunk := resp.read(1 << 20):
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != sha256:
            return False, f"{asset}: SHA-256 mismatch, not installed"
        with zipfile.ZipFile(work / asset) as z:
            member = next((m for m in z.namelist() if m.rsplit("/", 1)[-1] == wheel), None)
            if member is None:
                return False, f"no ROCm build of ctranslate2 for {wheel}"
            path = z.extract(member, work)
        # Runtime first: the ROCm ctranslate2 does not even import without it, so a
        # download that fails half-way must leave the CPU build in place.
        ok, output = _run_streamed([*pip, "--upgrade", "--index-url", _ROCM_INDEX.format(family=family),
                                    f"rocm-sdk-core=={_ROCM_SDK_VERSION}",
                                    f"rocm-sdk-libraries-{family.lower()}=={_ROCM_SDK_VERSION}"], on_line)
        if not ok:
            return ok, output
        return _run_streamed([*pip, "--upgrade", "--force-reinstall", "--no-deps", path], on_line)
    except OSError as exc:
        on_line(str(exc))
        return False, str(exc)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def install_transcription(with_gpu: bool, on_line: Callable[[str], None]) -> tuple[bool, bool, str]:
    """Returns (success, needs_restart, error_message - "" on success);
    every line of pip's output goes to ``on_line`` as it arrives. GPU: NVIDIA
    whenever nvidia-smi is there (as before), else a supported AMD card."""
    amd = amd_gpu() if with_gpu and not shutil.which("nvidia-smi") else None
    packages = _PACKAGES + (_GPU_PACKAGES if with_gpu and not amd else [])
    if amd and sys.platform == "win32" and _loaded():
        message = "Transcription has run in this session and Windows keeps its files locked - restart FalconSDR, then install again."
        on_line(message)
        return False, False, message
    if amd:
        on_line(f"GPU: {amd[0]} - installing the AMD ROCm build (several GB)")
    # ctranslate2 already imported in this process stays the old (CPU/CUDA) build until a restart.
    amd_restart = bool(amd) and _loaded()

    def run_streamed(cmd: list[str]) -> tuple[bool, str]:
        return _run_streamed(cmd, on_line)

    def with_rocm(pip: list[str], result: tuple[bool, str]) -> tuple[bool, str]:
        return _install_rocm(pip, amd[1], on_line) if amd and result[0] else result

    def pip_install(python_exe: str) -> tuple[bool, str]:
        pip = [python_exe, "-m", "pip", "install"]
        return with_rocm(pip, run_streamed([*pip, "--upgrade", *packages]))

    # A PyInstaller-frozen build's own sys.executable *is* the app itself,
    # not a real Python interpreter - `-m pip` would re-launch the app.
    # Its own `--pip` mode runs the bundled pip instead (main.py).
    if getattr(sys, "frozen", False):
        if _REMOVE_MARKER.exists():
            # pip --target skips folders that already exist, so installing over a
            # pending (possibly half-done) removal would keep broken packages.
            message = "Restart FalconSDR to finish removing transcription, then install it again."
            on_line(message)
            return False, False, message
        enable_addons()
        # Windows locks a loaded .pyd: once transcription has run in this
        # process, --upgrade can't replace those folders - only add what's
        # missing (e.g. the GPU libraries on a second "Install").
        pip = [sys.executable, "--pip", "install", "--only-binary=:all:", "--disable-pip-version-check",
               "--target", str(ADDONS_DIR)]
        ok, output = with_rocm(pip, run_streamed([*pip, *([] if _loaded() else ["--upgrade"]), *packages]))
        return ok, ok and amd_restart, "" if ok else output
    in_venv = Path(sys.prefix).resolve() == VENV_DIR.resolve()
    # An existing .venv means this machine already needed the fallback
    # once before - go straight there instead of re-discovering PEP 668.
    if VENV_PYTHON.exists():
        ok, output = pip_install(str(VENV_PYTHON))
        return ok, ok and (not in_venv or amd_restart), "" if ok else output

    ok, output = pip_install(sys.executable)
    if ok:
        return True, amd_restart, ""
    if _EXTERNALLY_MANAGED_MARKER not in output:
        return False, False, output  # a real failure unrelated to PEP 668 - a venv wouldn't fix it
    on_line(
        "-- direct install blocked (externally-managed-environment) - "
        "creating a dedicated venv instead --"
    )
    ok, venv_output = run_streamed([sys.executable, "-m", "venv", "--system-site-packages", str(VENV_DIR)])
    if not ok:
        # Remove any half-created venv (e.g. python3-venv missing ensurepip) so a later retry creates it cleanly.
        shutil.rmtree(VENV_DIR, ignore_errors=True)
        return False, False, venv_output
    ok, output = pip_install(str(VENV_PYTHON))
    return ok, ok, "" if ok else output
