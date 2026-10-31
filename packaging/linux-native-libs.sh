# Sourced by the Linux build scripts: finds libhackrf.so.0 and libairspy.so.0 (BSD-3-Clause, AGENTS.md gotcha 55)
# on the build machine and fills NATIVE_ARGS with PyInstaller --add-binary flags, so HackRF/Airspy work without
# SoapySDR on a machine with nothing installed. PyInstaller adds their libusb on its own.
#   collect_native_libs strict   - a missing library fails the build (the .deb: built in a known container)
#   collect_native_libs soft     - a missing library is only a warning (rpm/AppImage: built on whatever machine)
# Needs the packages libhackrf0 and libairspy0 (Debian/Ubuntu) / hackrf-libs and airspy-libs (Fedora).
collect_native_libs() {
  local mode="$1" cache lib path
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

# Appends the Debian copyright files of what collect_native_libs bundled (and the libusb they load) to a licence file.
append_native_licenses() {
  local out="$1" doc
  for doc in libhackrf0 libairspy0 libusb-1.0-0; do
    if [ -f "/usr/share/doc/$doc/copyright" ]; then
      { printf '\n%s\n%s (bundled shared library - its distribution'"'"'s copyright file)\n%s\n\n' \
          "==============================================================================" "$doc" \
          "=============================================================================="
        cat "/usr/share/doc/$doc/copyright"; } >> "$out"
    fi
  done
}
