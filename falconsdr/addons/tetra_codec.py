"""TETRA speech codec (ACELP, ETSI EN 300 395-2) - fetched from ETSI on the user's computer, never shipped.

The reference C code is ETSI's. This module downloads it from etsi.org (pinned by SHA-256), compiles it here
together with FalconSDR's own stdin/stdout front end (``tetra-rs/codec/falconsdr_sdecoder.c``) and leaves one
program that ``falconsdr-tetra --codec`` talks to. Nothing of ETSI's goes into the FalconSDR repository or its
packages. The codec may be covered by patents in some countries; whether to fetch it is the user's decision."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile
from pathlib import Path

from ..audio_transcode import NO_WINDOW, system_env
from ..digital_voice import _urlopen
from ..i18n import tr
from .external import BIN_DIR, which

URL = "https://www.etsi.org/deliver/etsi_en/300300_300399/30039502/01.03.01_60/en_30039502v010301p0.zip"
SHA256 = "1fe18c4773c8ccb52ef23ca5b4a0b0841b38d54ddd5d9c7d86eb8b060f132f39"
# What the decoder needs (ETSI's makefile, SRCS3, without its own main).
SOURCES = ["sdec_tet.c", "sub_sc_d.c", "sub_dsp.c", "fbas_tet.c", "fexp_tet.c", "fmat_tet.c", "tetra_op.c"]
FRONT_END = Path(__file__).resolve().parents[2] / "tetra-rs" / "codec" / "falconsdr_sdecoder.c"
CODEC_DIR = BIN_DIR / "tetra"
FRAME_WORDS, FRAME_SAMPLES = 138, 240

_lock = threading.Lock()
_state = {"running": False, "log": [], "error": "", "hint": "", "link": ""}
MSYS2_URL = "https://www.msys2.org/"   # a free C compiler for Windows (then: pacman -S mingw-w64-ucrt-x86_64-gcc)
# Windows has no C compiler by default: TinyCC (LGPL, 0.5 MB) from its authors does the job. Tried: it builds ETSI's code
# to a program whose output is byte-identical to gcc's (Linux build of the same release); run on Windows only by hand.
TCC_URL = "https://download.savannah.gnu.org/releases/tinycc/tcc-0.9.27-win64-bin.zip"
TCC_SHA256 = "34a721949a2583fdff725312da092fa0f5f1f284b702e6f811c6954714faabb2"


def exe_name() -> str:
    return "falconsdr-tetra-codec.exe" if sys.platform == "win32" else "falconsdr-tetra-codec"


def codec_executable() -> Path | None:
    p = CODEC_DIR / exe_name()
    return p if p.is_file() else None


def can_install() -> bool:
    return FRONT_END.is_file()


def install_state() -> dict:
    with _lock:
        return {**_state, "log": list(_state["log"][-12:])}


def _say(line: str) -> None:
    with _lock:
        _state["log"].append(line)


def start() -> None:
    if not can_install():
        raise RuntimeError(tr("The codec front end (tetra-rs/codec/) is not present in this installation."))
    with _lock:
        if _state["running"]:
            return
        _state.update(running=True, log=[], error="", hint="", link="")
    threading.Thread(target=_build, daemon=True, name="tetra-codec-build").start()


def uninstall() -> None:
    with _lock:
        if _state["running"]:
            raise RuntimeError(tr("Installation is in progress - wait for it to finish."))
        _state.update(log=[], error="", hint="", link="")
    (CODEC_DIR / exe_name()).unlink(missing_ok=True)


def _compiler() -> str | None:
    for name in ("cc", "gcc", "clang"):
        found = which(name)
        if found:
            return found
    return None


def _fetch_tcc(work: Path) -> str:
    """TinyCC for Windows, checked by SHA-256, unpacked next to the build; returns tcc.exe."""
    _say("No C compiler found - downloading TinyCC (a small free one, LGPL) from its authors…")
    zip_path = work / "tcc.zip"
    try:
        with _urlopen(urllib.request.Request(TCC_URL, headers={"User-Agent": "FalconSDR"})) as resp, open(zip_path, "wb") as out:
            shutil.copyfileobj(resp, out)
    except OSError as exc:
        with _lock:
            _state["link"] = MSYS2_URL
            _state["hint"] = "pacman -S mingw-w64-ucrt-x86_64-gcc"
        raise RuntimeError(tr("Could not download a small C compiler ({error}). Check the internet connection, or install one yourself (below) and try again.").format(error=exc)) from exc
    if hashlib.sha256(zip_path.read_bytes()).hexdigest() != TCC_SHA256:
        raise RuntimeError(tr("The compiler download is not the version FalconSDR was tested with, so it was not used."))
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(work)      # zipfile drops absolute paths and ".." parts itself
    exe = work / "tcc" / "tcc.exe"
    if not exe.is_file():
        raise RuntimeError("TinyCC's archive does not have tcc.exe")
    return str(exe)


def _extract(zip_path: Path, dest: Path) -> None:
    """Only ETSI's c-code/ folder, lower-cased (the zip has upper-case names), flat; no path can escape dest."""
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            parts = info.filename.lower().replace("\\", "/").split("/")
            if info.is_dir() or len(parts) != 2 or parts[0] != "c-code" or not re.fullmatch(r"[a-z0-9_.]+", parts[1]):
                continue
            (dest / parts[1]).write_bytes(z.read(info))


def check_codec(exe: Path) -> None:
    """Two frames in, two frames out - the same exchange the TETRA decoder has with it."""
    r = subprocess.run([str(exe)], input=b"\x00" * (2 * FRAME_WORDS * 2), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                       timeout=20, creationflags=NO_WINDOW)
    if r.returncode != 0 or len(r.stdout) != 2 * FRAME_SAMPLES * 2:
        raise RuntimeError("the compiled codec does not answer as expected")


def _build() -> None:
    CODEC_DIR.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="build-", dir=str(CODEC_DIR)))
    try:
        cc = _compiler()
        tiny = False
        if not cc and sys.platform == "win32":
            cc, tiny = _fetch_tcc(work), True
        if not cc:
            with _lock:
                _state["hint"] = "xcode-select --install" if sys.platform == "darwin" else "sudo apt install build-essential"
            raise RuntimeError(tr("A C compiler is needed to build the codec and none was found. Install one (below), then try again."))
        _say("Downloading the TETRA speech codec from ETSI (etsi.org)…")
        zip_path = work / "codec.zip"
        try:
            # etsi.org answers 403 to urllib's default User-Agent
            with _urlopen(urllib.request.Request(URL, headers={"User-Agent": "FalconSDR"})) as resp, open(zip_path, "wb") as out:
                shutil.copyfileobj(resp, out)
        except OSError as exc:
            with _lock:
                _state["link"] = URL
            raise RuntimeError(tr("Could not download from etsi.org ({error}). Check the internet connection and try again.").format(error=exc)) from exc
        if hashlib.sha256(zip_path.read_bytes()).hexdigest() != SHA256:
            raise RuntimeError(tr("The file from etsi.org is not the version FalconSDR was tested with, so it was not used."))
        src = work / "src"
        src.mkdir()
        _extract(zip_path, src)
        if not all((src / n).is_file() for n in SOURCES):
            raise RuntimeError("ETSI's archive does not have the expected files")
        # ETSI's code is 32-bit era C: its 32-bit word is a `long`, which is 64 bits on Linux/macOS.
        h = src / "source.h"
        text = h.read_text(encoding="latin-1")
        fixed = re.sub(r"typedef\s+long\s+Word32\s*;", "typedef int Word32;", text)
        if fixed == text:
            raise RuntimeError("ETSI's source.h is not as expected")
        h.write_text(fixed, encoding="latin-1")
        shutil.copy2(FRONT_END, src / FRONT_END.name)
        _say("Compiling…")
        exe = src / exe_name()
        if tiny:    # TinyCC: no -O2/-std/-lm (the C library's maths is in msvcrt already)
            cmd = [cc, "-B", str(Path(cc).parent), "-w", "-I.", "-o", str(exe), FRONT_END.name, *SOURCES]
        else:
            cmd = [cc, "-O2", "-w", "-std=gnu89", "-I.", "-o", str(exe), FRONT_END.name, *SOURCES, "-lm"]
            if os.name == "posix" and shutil.which("nice"):
                cmd = ["nice", "-n", "15", *cmd]
        proc = subprocess.run(cmd, cwd=str(src), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                              env=system_env() or os.environ.copy(), creationflags=NO_WINDOW)
        if proc.returncode != 0 or not exe.is_file():
            for line in proc.stdout.decode("utf-8", "replace").strip().splitlines()[-8:]:
                _say(line)
            raise RuntimeError(tr("Building the codec failed. The last lines are above; you can also build it yourself from ETSI's code and enter the program's path below."))
        _say("Checking it…")
        check_codec(exe)
        shutil.copy2(exe, CODEC_DIR / exe_name())
        _say("Done.")
    except Exception as exc:  # noqa: BLE001
        with _lock:
            _state["error"] = str(exc)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        with _lock:
            _state["running"] = False
