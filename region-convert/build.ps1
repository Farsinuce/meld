# Build region-convert from the vendored source and drop the binary in bin/ for Meld to find.
# Windows. Requires a Rust toolchain (https://rustup.rs).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

cargo build --release

$arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "x86_64" }
New-Item -ItemType Directory -Force -Path bin | Out-Null
$dest = if ($arch -eq "x86_64") { "bin/region_converter.exe" } else { "bin/region_converter-windows-$arch.exe" }
Copy-Item "target/release/region_converter.exe" $dest -Force
Write-Host "built -> $dest"
