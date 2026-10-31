# Sourced by the Linux build scripts: finds libhackrf.so.0 and libairspy.so.0 (BSD-3-Clause, AGENTS.md gotcha 55)
# on the build machine and fills NATIVE_ARGS with PyInstaller --add-binary flags, so HackRF/Airspy work without
# SoapySDR on a machine with nothing installed. PyInstaller adds their libusb on its own.
#   collect_native_libs strict   - a missing library fails the build (the .deb: built in a known container)
#   collect_native_libs soft     - a missing library is only a warning (build.sh run by hand on whatever machine)
#   FALCONSDR_REQUIRE_NATIVE_LIBS=1 (set by every build-in-docker.sh) makes it strict, and --self-test then requires them too.
# Needs the packages libhackrf0 and libairspy0 (Debian/Ubuntu) / hackrf and airspyone_host (Fedora).
collect_native_libs() {
  local mode="$1" cache lib path
  [ "${FALCONSDR_REQUIRE_NATIVE_LIBS:-}" = 1 ] && mode=strict   # the container builds: a missing library fails them
  NATIVE_ARGS=()
  cache="$(mktemp)"
  ldconfig -p > "$cache" 2>/dev/null || true       # to a file: `ldconfig -p | awk` would SIGPIPE under pipefail
  for lib in libhackrf.so.0 libairspy.so.0; do
    path="$(awk -v l="$lib" '$1 == l {print $NF; exit}' "$cache")"
    if [ -n "$path" ]; then
      NATIVE_ARGS+=(--add-binary "$path:.")
    elif [ "$mode" = strict ]; then
      echo "ERROR: $lib not found on the build machine (apt install libhackrf0 libairspy0)" >&2
      rm -f "$cache"; return 1
    else
      echo "WARNING: $lib not found - this build will not carry it (HackRF/Airspy then need the system library)" >&2
    fi
  done
  rm -f "$cache"
}

# Appends the licence files of what collect_native_libs bundled (and the libusb they load) to a licence file:
# Debian's /usr/share/doc/<pkg>/copyright, Fedora's /usr/share/licenses/<pkg>/*.
append_native_licenses() {
  local out="$1" pkg f here
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  { printf '\n%s\nlibhackrf (bundled shared library)\n%s\n\n' "==============================================================================" "=============================================================================="
    cat "$here/licenses/libhackrf-BSD-3-Clause.txt"; } >> "$out"
  for pkg in libhackrf0 libairspy0 libusb-1.0-0 hackrf airspyone_host libusb1; do
    for f in "/usr/share/doc/$pkg/copyright" /usr/share/licenses/"$pkg"/*; do
      [ -f "$f" ] || continue
      { printf '\n%s\n%s (bundled shared library - its distribution'"'"'s licence file %s)\n%s\n\n' \
          "==============================================================================" "$pkg" "$f" \
          "=============================================================================="
        cat "$f"; } >> "$out"
    done
  done
}
