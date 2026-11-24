# Sourced by the Linux build scripts (like linux-native-libs.sh): builds our own falconsdr-tetra (Rust, no dependencies,
# GPL-3.0-or-later) and fills TETRA_ARGS with PyInstaller --add-data flags, laid out as in the repository
# (tetra-rs/target/release/..., tetra-rs/codec/...) so falconsdr/addons/tetra.py finds them in the bundle unchanged.
# The speech codec is NOT built here: ETSI's code is fetched on the user's computer (addons/tetra_codec.py).
# Needs cargo on the build machine (the build-in-docker.sh scripts install it).
build_tetra() {
  local root="$1"
  command -v cargo >/dev/null 2>&1 || { echo "ERROR: cargo not found (the TETRA decoder is built from tetra-rs/)" >&2; return 1; }
  cargo build --release --locked --manifest-path "$root/tetra-rs/Cargo.toml"
  TETRA_ARGS=(
    --add-data "$root/tetra-rs/target/release/falconsdr-tetra:tetra-rs/target/release"
    --add-data "$root/tetra-rs/codec/falconsdr_sdecoder.c:tetra-rs/codec"
  )
}
