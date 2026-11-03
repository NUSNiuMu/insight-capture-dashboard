#!/usr/bin/env bash
set -euo pipefail
output=${1:?Usage: build_hotspot_driver.sh OUTPUT_DIRECTORY}
mkdir -p "$output"
output=$(realpath "$output")
archive="$output/public_sources.tbz2"
checksum=2c177804679e3ed650dabec6fa958388579896f170570c6171a1b6c386669216
if [[ ! -f "$archive" ]]; then
    curl --fail --location --output "$archive" \
        https://developer.download.nvidia.com/embedded/L4T/r36_Release_v4.3/sources/public_sources.tbz2
fi
echo "$checksum  $archive" | sha256sum --check
tar -xjf "$archive" -C "$output" Linux_for_Tegra/source/kernel_oot_modules_src.tbz2
mkdir -p "$output/driver-source"
tar -xjf "$output/Linux_for_Tegra/source/kernel_oot_modules_src.tbz2" \
    -C "$output/driver-source" nvidia-oot/drivers/net/wireless/realtek/rtl8822ce
source_root="$output/driver-source/nvidia-oot"
driver="$source_root/drivers/net/wireless/realtek/rtl8822ce"
make -C "/lib/modules/$(uname -r)/build" M="$driver" ARCH=arm64 \
    srctree.nvidia-oot="$source_root" CONFIG_RTL8822CE=m \
    USER_EXTRA_CFLAGS='-DCONFIG_CONCURRENT_MODE -DCONFIG_IFACE_NUMBER=2' -j2 modules
echo "Driver: $driver/rtl8822ce.ko"
