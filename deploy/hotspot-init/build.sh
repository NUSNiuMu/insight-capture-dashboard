#!/usr/bin/env bash
# Build from the indexed ARM64 offline Debian repository and per-kernel driver directories.
set -euo pipefail
debs=${1:?Usage: build.sh OFFLINE_DEBS DRIVERS OUTPUT.run}
drivers=${2:?Specify the directory containing KERNEL/rtl8822ce.ko}
output=${3:?Specify the output .run file}
src=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$(dirname "$output")"
work=$(mktemp -d "$(dirname "$output")/.hotspot-build.XXXXXX")
trap 'rm -rf -- "$work"' EXIT
payload="$work/payload"
mkdir -p "$payload/deploy" "$payload/insight_capture/runtime" "$payload/offline-debs"
cp "$debs"/*.deb "$debs/Packages" "$payload/offline-debs/"
gzip -nc "$payload/offline-debs/Packages" > "$payload/offline-debs/Packages.gz"
for driver in "$drivers"/*/rtl8822ce.ko; do
    kernel=$(basename "$(dirname "$driver")")
    [[ $(modinfo -F vermagic "$driver") == "$kernel "* ]]
    install -Dm644 "$driver" "$payload/drivers/$kernel/rtl8822ce.ko"
done
cp -R "$src/hotspot" "$payload/deploy/"
cp "$src"/{install_capture_hotspot.sh,install_network_services.py,offline/select_iptables_backend.sh} "$payload/deploy/"
cp "$src/../insight_capture/runtime/discovery.py" "$payload/insight_capture/runtime/"
cp "$src/hotspot-init/"{setup,report,verify}.sh "$src/hotspot-init/insight-hotspot-verify.service" "$payload/"
(cd "$payload" && find . -type f ! -name SHA256SUMS -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > SHA256SUMS)
{
    head -n 1 "$src/hotspot-init/launcher.sh"
    cat "$src/hotspot-init/report.sh"
    tail -n +2 "$src/hotspot-init/launcher.sh"
} > "$work/package.run"
tar --owner=0 --group=0 -C "$payload" -czf - . >> "$work/package.run"
chmod 755 "$work/package.run"
mv -fT "$work/package.run" "$output"
