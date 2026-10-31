"""Optional digital voice (DMR, C4FM/YSF) through dsd-fme - an add-on the user
installs from Settings, never bundled: FalconSDR distributes none of its code
(nor mbelib's AMBE/IMBE vocoder), it is fetched from its authors on the user's
own machine. Windows gets the authors' portable build; Linux/macOS build the
pinned source (needs a compiler and a few -dev packages - install_hint()).

A digital slot sends its FM discriminator output (48 kHz s16, Demodulator.
discriminate()) to its own dsd-fme process and reads decoded 8 kHz voice back,
plus what dsd-fme prints about the call (talkgroup, source, encryption).
Encrypted voice stays muted - dsd-fme's default; no key is ever passed.
"""
from __future__ import annotations

import gzip
import hashlib
import os
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

import numpy as np

from .audio_transcode import NO_WINDOW, system_env
from .models import Modulation
from .playback.config import APP_DATA_DIR

DIGITAL_MODULATIONS = (Modulation.DMR, Modulation.YSF)
DISCRIMINATOR_RATE = 48000
_VOICE_RATE = 8000
_MODE_ARGS = {Modulation.DMR: ("-fs", 2), Modulation.YSF: ("-fy", 1)}  # dsd-fme mode, output channels
_MAX_BUFFER_S = 1.0  # decoded voice queued beyond this is dropped - latency must not grow

DSD_DIR = APP_DATA_DIR / "addons" / "dsd-fme"
# Pinned: the same release on every platform, and a build that was tested.
_RELEASE = "20260715"
_WINDOWS_ZIP = (f"https://github.com/lwvmobile/dsd-fme/releases/download/{_RELEASE}/"
                f"dsd-fme-x86-64-cygwin-portable-{_RELEASE}.zip")
# SHA-256 of what is downloaded (of the *unzipped* .tar for the sources: GitHub may re-compress
# a tag's tarball). Code fetched from the internet and run here is only run if it is the code
# that was tested - a re-pointed tag or release asset fails the install instead. Update with _RELEASE.
_WINDOWS_ZIP_SHA256 = "006cfa420e79033b51ded97ed4d2d0a9715e350ffcfe35c41e89b9e6ef00e9f8"
_SOURCES = {
    "mbelib": ("https://codeload.github.com/lwvmobile/mbelib/tar.gz/34adf9f054bc5650ace162a4917dcbc2cfa6102e",
               "f877f02d19736888cda7e8b28b3c9c1689af3cde7840fb235aec078c96fb4cb6"),
    "dsd-fme": (f"https://codeload.github.com/lwvmobile/dsd-fme/tar.gz/refs/tags/{_RELEASE}",
                "873a70e38cb05ca0ae8722d7e2b946a786dce1870446dfcff273e5af42e42f82"),
}
ISSUES_URL = "https://github.com/rafafunt/FalconSDR/issues"


def dsd_executable() -> Path | None:
    """The one source of truth for "is digital voice available"."""
    name = "dsd-fme.exe" if sys.platform == "win32" else "dsd-fme"
    for path in (DSD_DIR / "bin" / name, DSD_DIR / "dsd-fme-portable" / "dsd-fme" / name):
        if path.is_file():
            return path
    found = shutil.which("dsd-fme")
    return Path(found) if found else None


def is_digital_installed() -> bool:
    return dsd_executable() is not None


def _env() -> dict:
    env = dict(system_env() or os.environ)
    lib = str(DSD_DIR / "lib")
    for key in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        env[key] = lib + (os.pathsep + env[key] if env.get(key) else "")
    return env


# ---------------------------------------------------------------- decoding
_TALKGROUP_RE = re.compile(r"TGT=(\d+) SRC=(\d+)")
_YSF_RE = re.compile(r"DST: (\S+).*?SRC: (\S+)")


class DsdDecoder:
    """One dsd-fme process per digital slot. feed() takes discriminator audio,
    read() returns exactly the requested number of samples of decoded voice
    (zeros while there is none), so the slot's timeline never drifts."""

    def __init__(self, modulation: Modulation, out_rate: int):
        mode, self._channels = _MODE_ARGS[modulation]
        exe = dsd_executable()
        if exe is None:
            raise RuntimeError("dsd-fme is not installed")
        self.out_rate = out_rate
        self.talkgroup = ""
        self.source = ""
        self.encrypted = False
        self._buf = np.zeros(0, dtype=np.float32)
        self._frac = 0.0
        self._lock = threading.Lock()
        self._proc = subprocess.Popen(
            [str(exe), "-i", "-", "-o", "-", mode],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(exe.parent), env=_env(), creationflags=NO_WINDOW,
        )
        threading.Thread(target=self._read_voice, daemon=True, name="dsd-voice").start()
        threading.Thread(target=self._read_info, daemon=True, name="dsd-info").start()

    def feed(self, discriminator: np.ndarray) -> None:
        pcm = (np.clip(discriminator * 40000.0, -32767, 32767)).astype("<i2").tobytes()
        try:
            self._proc.stdin.write(pcm)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass  # dsd-fme ended - read() keeps returning silence

    def read(self, n: int) -> np.ndarray:
        want = n * _VOICE_RATE / self.out_rate + self._frac
        m = int(want)
        self._frac = want - m
        with self._lock:
            take, self._buf = self._buf[:m], self._buf[m:]
        if take.size < m:
            take = np.concatenate([take, np.zeros(m - take.size, dtype=np.float32)])
        if m == 0 or not take.any():
            return np.zeros(n, dtype=np.float32)
        return np.interp(np.arange(n) * (m / n), np.arange(m), take).astype(np.float32)

    def close(self) -> None:
        try:
            self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            self._proc.kill()

    def _read_voice(self) -> None:
        frame = 2 * self._channels
        rest = b""
        while True:
            data = self._proc.stdout.read1(65536)
            if not data:
                return
            data = rest + data
            usable = len(data) - len(data) % frame
            rest = data[usable:]
            pcm = np.frombuffer(data[:usable], dtype="<i2").astype(np.float32) / 32768.0
            if self._channels == 2:  # DMR: the two timeslots - rarely both talking at once
                pcm = np.clip(pcm[0::2] + pcm[1::2], -1.0, 1.0)
            with self._lock:
                self._buf = np.concatenate([self._buf, pcm])[-int(_MAX_BUFFER_S * _VOICE_RATE):]

    def _read_info(self) -> None:
        for raw in self._proc.stderr:
            line = raw.decode("utf-8", "replace")
            if "Encrypted" in line:
                self.encrypted = True
            m = _TALKGROUP_RE.search(line)
            if m:
                self.talkgroup, self.source = m.group(1), m.group(2)
                continue
            m = _YSF_RE.search(line)
            if m:
                self.talkgroup, self.source = m.group(1).strip(), m.group(2).strip()


# ---------------------------------------------------------------- installing
_state_lock = threading.Lock()
_state = {"running": False, "log": [], "error": "", "hint": ""}


def install_state() -> dict:
    with _state_lock:
        return {**_state, "log": list(_state["log"][-12:]), "installed": is_digital_installed()}


def start_install() -> None:
    with _state_lock:
        if _state["running"]:
            return
        _state.update(running=True, log=[], error="", hint="")
    threading.Thread(target=_install_thread, daemon=True, name="dsd-install").start()


def uninstall() -> None:
    shutil.rmtree(DSD_DIR, ignore_errors=True)


def _say(line: str) -> None:
    with _state_lock:
        _state["log"].append(line)


def _install_thread() -> None:
    try:
        if sys.platform == "win32":
            _install_windows()
        else:
            _build_from_source()
        if not is_digital_installed():
            raise RuntimeError("dsd-fme was built but not found")
        _say("Done.")
    except Exception as exc:  # shown in Settings - the only place the user sees why
        with _state_lock:
            _state["error"] = str(exc)
            if sys.platform != "win32":
                _state["hint"] = install_hint()
    finally:
        with _state_lock:
            _state["running"] = False


def _ssl_contexts():
    """Default first; on a certificate error the OS's own verifier, then
    Mozilla's CA list. Reported: CERTIFICATE_VERIFY_FAILED - a packaged
    build's OpenSSL may not find the system CA store, and on Windows only the
    OS verifier fetches a root it doesn't have yet. Every install has pip,
    which vendors both (truststore needs Python 3.10+)."""
    yield None
    try:
        from pip._vendor import truststore
        yield truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        pass
    try:
        from pip._vendor import certifi
        yield ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass


def _urlopen(url: str):
    error = None
    for context in _ssl_contexts():
        try:
            return urllib.request.urlopen(url, timeout=60, context=context)
        except urllib.error.URLError as exc:
            if not isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise
            error = error or exc  # the first one says what the system itself saw
    raise error


def _sha256(path: Path, gunzip: bool) -> str:
    digest = hashlib.sha256()
    with (gzip.open(path, "rb") if gunzip else open(path, "rb")) as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(url: str, dest: Path, sha256: str, gunzip: bool = False) -> None:
    _say(f"Downloading {url.split('/')[4]}…")
    with _urlopen(url) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out)
    if _sha256(dest, gunzip) != sha256:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"{dest.name}: the download is not the tested version (checksum mismatch) - not installed")


def _install_windows() -> None:
    DSD_DIR.mkdir(parents=True, exist_ok=True)
    archive = DSD_DIR / "dsd-fme.zip"
    _download(_WINDOWS_ZIP, archive, _WINDOWS_ZIP_SHA256)
    _say("Unpacking…")
    with zipfile.ZipFile(archive) as z:
        z.extractall(DSD_DIR)
    archive.unlink()


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=system_env())
    if proc.returncode != 0:
        tail = proc.stdout.decode("utf-8", "replace").strip().splitlines()[-6:]
        for line in tail:
            _say(line)
        raise RuntimeError(f"{Path(cmd[0]).name} failed")


def _build_from_source() -> None:
    missing = [tool for tool in ("cmake", "make", "cc") if not shutil.which(tool)]
    if missing:
        raise RuntimeError("Missing build tools: " + ", ".join(missing))
    work = DSD_DIR.with_name("dsd-fme-build")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        jobs = str(max(1, (os.cpu_count() or 2)))
        for name, (url, sha256) in _SOURCES.items():
            archive = work / f"{name}.tar.gz"
            _download(url, archive, sha256, gunzip=True)
            with tarfile.open(archive) as tar:
                top = tar.getnames()[0].split("/")[0]
                if hasattr(tarfile, "data_filter"):  # Python 3.11.4+; Pi OS 12 ships 3.11.2
                    tar.extractall(work, filter="data")
                else:
                    tar.extractall(work)
            src = work / top
            _say(f"Building {name} (a few minutes)…")
            _run(["cmake", "-S", str(src), "-B", str(src / "build"), "-DCMAKE_BUILD_TYPE=Release",
                  f"-DCMAKE_INSTALL_PREFIX={DSD_DIR}", "-DCMAKE_INSTALL_LIBDIR=lib",
                  f"-DCMAKE_PREFIX_PATH={DSD_DIR}", f"-DCMAKE_INSTALL_RPATH={DSD_DIR / 'lib'}",
                  "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"])  # CMake 4 refuses mbelib's old minimum otherwise
            _run(["cmake", "--build", str(src / "build"), "-j", jobs])
            _run(["cmake", "--install", str(src / "build")])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def install_hint() -> str:
    """The one command that installs the build dependencies here (needs root,
    so the user runs it - the app itself never asks for a password)."""
    if sys.platform == "darwin":
        return "brew install cmake libsndfile pulseaudio ncurses"
    ids = ""
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8")
        ids = " ".join(re.findall(r'^(?:ID|ID_LIKE)="?([^"\n]*)', text, re.M))
    except OSError:
        pass
    if re.search(r"\b(fedora|rhel|centos)\b", ids):
        return "sudo dnf install gcc gcc-c++ make cmake libsndfile-devel pulseaudio-libs-devel ncurses-devel"
    if re.search(r"\b(arch)\b", ids):
        return "sudo pacman -S --needed base-devel cmake libsndfile libpulse ncurses"
    if re.search(r"\b(suse|opensuse)\b", ids):
        return "sudo zypper install gcc gcc-c++ make cmake libsndfile-devel libpulse-devel ncurses-devel"
    return "sudo apt install build-essential cmake libsndfile1-dev libpulse-dev libncurses-dev"
