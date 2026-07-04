# region-convert

A fast, cross-platform Rust tool Meld calls to convert Minecraft Java region saves between
formats: **mca ↔ linear ↔ blinear (v2/v3)**. Meld uses it for the **blinear (`B_LINEAR`)** export
and for `.b_linear` round-trips — it is ~1.2–2.5× faster than Meld's built-in Python codec.

Bundled prebuilt binaries (in `bin/`) so it works out of the box with no Rust toolchain:

| Platform | Binary |
|----------|--------|
| Windows x86_64 | `bin/region_converter.exe` |
| Windows arm64  | `bin/region_converter-windows-arm64.exe` |
| Linux x86_64   | `bin/region_converter-linux-x86_64` |
| Linux arm64    | `bin/region_converter-linux-arm64` |
| macOS          | build from source (see below) — no prebuilt is published upstream |

Meld resolves the right binary for the current OS/arch automatically
(`src/export.resolve_region_converter`), falling back to `target/release/` (a local build) and
then to `region_converter` on `PATH`.

## Build from source

Requires a Rust toolchain (`cargo`). The full source is vendored here.

```bash
./build.sh        # Linux / macOS  -> bin/region_converter-<os>-<arch>
./build.ps1       # Windows        -> bin/region_converter.exe
# or directly:
cargo build --release   # -> target/release/region_converter[.exe]
```

macOS users: run `./build.sh` once; it builds and drops the binary in `bin/` so later runs are
instant.

## Format note (why Meld's `.linear` stays Python)

This tool writes **Linear v3** (`--to linear`, version byte 3). Leaf's `LINEAR_V2` reader handles
Linear **v1/v2** only, so Meld keeps its own Python encoder for the Leaf-served `.linear` format
(byte-compatible Linear **v1**). This tool is used for **blinear** (Leaf `B_LINEAR`, which is v3
buckets and reads fine) and for any mca/linear/blinear round-trip in tooling.

## Attribution

Built from third-party open-source (MIT). See `LICENSE` here and the Meld CHANGELOG entry.
