# FalconSDR on the AUR

This directory holds the Arch User Repository packaging. The AUR package
repackages the official self-contained `.deb` from GitHub Releases
(PyInstaller bundle in `/opt/falconsdr` - no Python packaging involved),
with two Arch adjustments done in `PKGBUILD`'s `package()`:

- the udev rule moves from Debian's `/etc/udev/rules.d` to Arch's
  `/usr/lib/udev/rules.d`,
- an alpm hook reloads udev rules on install/remove (the `.deb`'s
  `postinst` equivalent - best-effort, never fails the transaction).

## Maintainer

`9M2PJU <9m2pju@gmail.com>` (see `PKGBUILD`'s `Maintainer:` line).

## Updating for a new upstream release

1. Download the new `.deb` and take its SHA-256:
   ```
   curl -fsSL -o falconsdr_<ver>_amd64.deb \
     https://github.com/rafafunt/FalconSDR/releases/download/v<ver>/falconsdr_<ver>_amd64.deb
   sha256sum falconsdr_<ver>_amd64.deb
   ```
   (Upstream publishes a `SHA256SUMS` file next to the release assets -
   cross-check against it.)
2. Bump `pkgver` and `sha256sums` in `PKGBUILD`, reset `pkgrel=1`.
3. Regenerate and verify:
   ```
   makepkg --printsrcinfo > .SRCINFO
   makepkg -sif --noconfirm   # builds, installs, runs nothing but file copies
   falconsdr --self-test
   ```
4. Push to the AUR (first push creates the `falconsdr` package):
   ```
   git clone ssh://aur@aur.archlinux.org/falconsdr.git /tmp/aur-falconsdr
   cp PKGBUILD .SRCINFO /tmp/aur-falconsdr/
   cd /tmp/aur-falconsdr && makepkg --printsrcinfo > .SRCINFO \
     && git add PKGBUILD .SRCINFO && git commit -m "v<ver>" && git push
   ```

## Why repackage the .deb instead of building from source?

Upstream's supported Linux builds are self-contained PyInstaller bundles
(RTL-SDR only; SoapySDR needs a source install). Building that bundle from
the Arch side would reintroduce exactly the venv/PyInstaller machinery the
bundle exists to avoid, plus two PyPI-only dependencies
(`pyrtlsdr`, `pyrtlsdrlib`) that have no Arch packages. Repackaging the
tested release artifact keeps the AUR package a thin, auditable file move.
When upstream publishes official `.rpm`/AppImage assets, the `source=`
line can switch to one of those instead.
