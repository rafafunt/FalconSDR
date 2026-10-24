Name:           falconsdr
Version:        0.9.0
Release:        1%{?dist}
Summary:        Wideband SDR ATC/radio scanner (self-contained build)
License:        GPL-3.0-or-later
URL:            https://github.com/rafafunt/FalconSDR
Source0:        https://github.com/rafafunt/FalconSDR/archive/refs/tags/v%{version}.tar.gz
BuildArch:      x86_64
# PyInstaller bundle needs these at build time only.
BuildRequires:  python3 >= 3.10
BuildRequires:  python3-pip
BuildRequires:  binutils
BuildRequires:  systemd-rpm-macros
# Runtime: self-contained /opt bundle, RTL-SDR only. ffmpeg is optional
# (MP3 output falls back to the bundled imageio-ffmpeg binary, system one wins).
Recommends:     ffmpeg
# /opt/falconsdr ships its own bundled .so files (Python, numpy, ...):
# rpm's automatic dependency scan would pick those up as system
# requirements (e.g. libuuid-...so.1.3.0(UUID_1.0)) that no distro
# provides, making the package uninstallable. The bundle needs nothing
# from the system beyond glibc + udev, so both directions are off.
AutoReqProv:    no
# File-based requirement: Fedora ships udevadm in systemd-udev, openSUSE
# in udev - a package-name Require would only fit one of them.
Requires:       /usr/bin/udevadm
Requires(post): /usr/bin/udevadm

%description
Scans a radio band via RTL-SDR and records audio only for transmissions
the scanner actually stops on.

The interface runs in the web browser (http://127.0.0.1:8765), which the
app opens on start; other devices on the network can use it too when run
with --server.

This is a self-contained ("fat") build: its own Python runtime and all
pip-installable dependencies (numpy, scipy, pyrtlsdrlib) are bundled in
/opt/%{name} - nothing from the system Python is required. RTL-SDR
hardware works out of the box.

Not included in this build: SoapySDR-based hardware (HackRF, LimeSDR,
Airspy, SDRplay, USRP - needs a normal pip/source install instead).
See /usr/share/doc/%{name}/packaging-README.md.

%prep
%autosetup -n FalconSDR-%{version}

%build
# Same frozen-bundle rules as packaging/linux-deb/build.sh (AGENTS.md
# gotcha 35): throwaway venv, PyInstaller --onedir, explicit --collect-data
# for runtime-loaded data PyInstaller cannot see, then --self-test on the
# built binary so a silently dropped dependency fails the build here.
python3 -m venv .rpm-venv
.rpm-venv/bin/pip install --upgrade --quiet pip
.rpm-venv/bin/pip install --quiet -r requirements.txt pyinstaller
.rpm-venv/bin/pyinstaller \
  --name %{name} \
  --onedir \
  --noconfirm \
  --add-data "%{_builddir}/FalconSDR-%{version}/falconsdr/locale/pl.po:falconsdr/locale" \
  --add-data "%{_builddir}/FalconSDR-%{version}/falconsdr/presets/band_presets.json:falconsdr/presets" \
  --add-data "%{_builddir}/FalconSDR-%{version}/falconsdr/web/static:falconsdr/web/static" \
  --collect-data tzdata \
  --collect-data pyrtlsdrlib \
  --collect-data imageio_ffmpeg \
  --collect-all pip \
  --distpath dist \
  --workpath build-pyinstaller \
  --specpath build-pyinstaller \
  main.py
./dist/%{name}/%{name} --self-test
.rpm-venv/bin/python packaging/collect_licenses.py THIRD-PARTY-LICENSES.txt

%install
install -d %{buildroot}/opt/%{name}
cp -r dist/%{name}/. %{buildroot}/opt/%{name}/
install -d %{buildroot}%{_bindir}
cat > %{buildroot}%{_bindir}/%{name} <<'WRAPPER'
#!/bin/sh
exec /opt/falconsdr/falconsdr "$@"
WRAPPER
chmod 755 %{buildroot}%{_bindir}/%{name}
install -D -m 644 packaging/linux-deb/falconsdr.desktop %{buildroot}%{_datadir}/applications/falconsdr.desktop
install -D -m 644 falconsdr/web/static/icon.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/falconsdr.svg
install -D -m 644 packaging/linux-deb/README.md %{buildroot}%{_docdir}/%{name}/packaging-README.md
install -D -m 644 LICENSE %{buildroot}%{_docdir}/%{name}/copyright
install -m 644 THIRD-PARTY-LICENSES.txt %{buildroot}%{_docdir}/%{name}/THIRD-PARTY-LICENSES.txt
install -D -m 644 packaging/linux-deb/60-rtl-sdr.rules %{buildroot}%{_udevrulesdir}/60-rtl-sdr.rules
install -D -m 644 packaging/linux-deb/blacklist-rtl-dvb.conf %{buildroot}%{_modprobedir}/blacklist-rtl-dvb.conf

%post
# Same best-effort, never-hang logic as packaging/linux-deb/postinst
# (AGENTS.md gotcha 35): udev may not be running (container/chroot).
if command -v udevadm >/dev/null 2>&1; then
  timeout 5 udevadm control --reload-rules >/dev/null 2>&1 || true
  timeout 5 udevadm trigger --subsystem-match=usb >/dev/null 2>&1 || true
fi
if command -v rmmod >/dev/null 2>&1; then
  timeout 5 rmmod dvb_usb_rtl28xxu >/dev/null 2>&1 || true
fi
%udev_rules_update

%postun
%udev_rules_update

%files
/opt/%{name}/
%{_bindir}/%{name}
%{_datadir}/applications/falconsdr.desktop
%{_datadir}/icons/hicolor/scalable/apps/falconsdr.svg
%{_docdir}/%{name}/
%{_udevrulesdir}/60-rtl-sdr.rules
%{_modprobedir}/blacklist-rtl-dvb.conf

%changelog
* Fri Oct 23 2026 9M2PJU <9m2pju@gmail.com> - 0.9.0-1
- First RPM packaging (PyInstaller self-contained bundle, mirrors .deb).
