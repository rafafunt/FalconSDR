"""Downloads the Windows builds of libhackrf and libairspy (and the two DLLs they need) for the bundle.

    python packaging/windows/fetch_native_libs.py <output dir>      (needs: pip install zstandard)

Why conda-forge: HackRF publishes no Windows binaries (only source), and Airspy's own zip carries a 2021
libusb and pthreadVC2.dll, which needs the Visual C++ 2010 runtime - absent on a clean Windows. The four
packages below are pinned by SHA-256 (a changed file fails the build), built by conda-forge from the
authors' sources, x64, and need nothing but the UCRT (Windows 10+) and the VC runtime the bundle already
has. To update: pick newer files at https://anaconda.org/conda-forge/<package>/files, put their URL and
SHA-256 here, then check `main.py --self-test` on the built exe (it loads both libraries).

Licences (AGENTS.md gotcha 55; checked against the sources 2026-10-31): libhackrf and libairspy are
BSD-3-Clause, libusb LGPL-2.1 (a separate DLL, so it can be replaced), libwinpthread MIT. Their texts
go into <output dir>/LICENSES.txt, which build.ps1 appends to THIRD-PARTY-LICENSES.txt.
"""
from __future__ import annotations

import hashlib
import io
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

_BASE = "https://conda.anaconda.org/conda-forge/win-64/"

# (file, sha256, DLL taken from Library/bin, what it is, licence, where the source is)
PACKAGES = [
    ("libhackrf0-2026.01.3-hfd05255_0.conda", "fd9a6a718cad7ff61f830e6c3789e26363d7f9e12399e25d9d5a68c06251f8f7",
     "hackrf-0.dll", "libhackrf 2026.01.3", "BSD-3-Clause (Great Scott Gadgets, Jared Boone, Benjamin Vernoux)",
     "https://github.com/greatscottgadgets/hackrf"),
    ("libairspy-1.0.10-h2466b09_1.conda", "12977c9ba7a376392907cc50873f1dbde731a7c9d325c33c390ad13b723784b1",
     "airspy.dll", "libairspy 1.0.10", "BSD-3-Clause (Airspy, Jared Boone, Michael Ossmann, Benjamin Vernoux, Youssef Touil)",
     "https://github.com/airspy/airspyone_host"),
    ("libusb-1.0.30-h6a83c73_1.conda", "b6a4af4d521c5494b5deffdc846823bf3ee18b4d6cb0dd60b1ff5505f8ff6d13",
     "libusb-1.0.dll", "libusb 1.0.30", "LGPL-2.1-or-later - a separate DLL: replace it with any libusb 1.0.30+ build",
     "https://github.com/libusb/libusb"),
    ("libwinpthread-12.0.0.r688.g6b2176247.1-h57928b3_0.conda", "bab5558663cb0e964988b859abd69d0f2380b6ea9f140495ab543e51af13635d",
     "libwinpthread-1.dll", "libwinpthread (mingw-w64) 12.0.0", "MIT", "https://www.mingw-w64.org/"),
]

# The conda package of libhackrf carries only the repository's GPL text (for the tools), not the library's own
# notice - BSD-3-Clause needs it with a binary: packaging/licenses/libhackrf-BSD-3-Clause.txt, which the Linux
# builds append to their licence files as well (packaging/linux-native-libs.sh).
_HACKRF_NOTICE = (Path(__file__).resolve().parent.parent / "licenses" / "libhackrf-BSD-3-Clause.txt").read_text(encoding="utf-8")

_NOTE = (
    "The conda-forge packages list GPL-2.0-or-later for their command-line tools (hackrf_transfer, airspy_rx...), which\n"
    "are NOT part of this build; the libraries themselves are BSD-3-Clause per the headers of their sources. The\n"
    "licence files shipped inside each package follow verbatim.\n\n"
)


def _download(name: str, sha256: str) -> bytes:
    with urllib.request.urlopen(_BASE + name, timeout=120) as r:
        data = r.read()
    got = hashlib.sha256(data).hexdigest()
    if got != sha256:
        raise SystemExit(f"{name}: SHA-256 is {got}, expected {sha256} - refusing to use it")
    return data


def _package_files(data: bytes):
    """(name, bytes) of every regular file in the package's pkg-*.tar.zst."""
    import zstandard
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        inner = next(n for n in z.namelist() if n.startswith("pkg-") and n.endswith(".tar.zst"))
        raw = z.read(inner)
    with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw)) as stream, tarfile.open(fileobj=stream, mode="r|") as tar:
        for member in tar:
            if member.isfile():
                yield member.name, tar.extractfile(member).read()


def fetch(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    licences = ["Native receiver libraries in this build\n" + "=" * 78 + "\n\n" + _NOTE]
    for name, sha256, dll, what, licence, source in PACKAGES:
        found = False
        texts = [f"--- libhackrf notice\n{_HACKRF_NOTICE}"] if dll == "hackrf-0.dll" else []
        for member, content in _package_files(_download(name, sha256)):
            if member == f"Library/bin/{dll}":
                (out / dll).write_bytes(content)
                found = True
            elif member.startswith("info/licenses/"):
                texts.append(f"--- {member[len('info/licenses/'):]}\n{content.decode('utf-8', 'replace')}")
        if not found:
            raise SystemExit(f"{name}: Library/bin/{dll} is not in the package")
        licences.append(f"{what}  ({dll})\nLicence: {licence}\nSource: {source}\nBinary: conda-forge, {name}\n\n"
                        + "\n".join(texts) + "\n" + "-" * 78 + "\n\n")
        print(f"{dll}: {(out / dll).stat().st_size} bytes ({what})")
    (out / "LICENSES.txt").write_text("".join(licences), encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    fetch(Path(sys.argv[1]))
