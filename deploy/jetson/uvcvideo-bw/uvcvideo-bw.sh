#!/bin/sh
# Patched uvcvideo for Jetson Orin: cap the isochronous bandwidth each UVC camera
# reserves, so four USB 2.0 MJPEG cameras fit on the Orin's single USB 2.0 bus.
# See README.md in this directory.
#
#   uvcvideo-bw.sh build       compile uvcvideo.ko for the running kernel (no sudo)
#   uvcvideo-bw.sh try         load it now, until reboot          (sudo; stop capture first)
#   uvcvideo-bw.sh install     load it on every boot                (sudo)
#   uvcvideo-bw.sh uninstall   back to the stock module             (sudo)
#   uvcvideo-bw.sh status      which module is loaded, with what cap
#
# MAX_PAYLOAD (default 1020 = the cameras' smallest isochronous altsetting) is the
# cap in bytes per 125 us microframe.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
KVER=$(uname -r)
BASE=${KVER%%-*}                       # 5.15.199-tegra -> 5.15.199
BUILD="$HERE/build-$KVER"
MAX_PAYLOAD=${MAX_PAYLOAD:-1020}
UPSTREAM="https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/plain/drivers/media/usb/uvc"
FILES="uvc_ctrl.c uvc_debugfs.c uvc_driver.c uvc_entity.c uvc_isight.c uvc_metadata.c uvc_queue.c uvc_status.c uvc_v4l2.c uvc_video.c uvcvideo.h"

build() {
    rm -rf "$BUILD" && mkdir -p "$BUILD"
    if [ -d "$HERE/src-$BASE" ]; then
        cp "$HERE/src-$BASE"/* "$BUILD/"
    else
        # A different kernel (e.g. after a JetPack upgrade): fetch the matching
        # upstream driver; the patch is small and usually applies unchanged.
        echo "no bundled source for $BASE, fetching v$BASE from kernel.org"
        for f in $FILES; do
            curl -sfL -m 60 -o "$BUILD/$f" "$UPSTREAM/$f?h=v$BASE" || { echo "fetch $f failed"; exit 1; }
        done
    fi
    cp "$HERE/Kbuild" "$BUILD/"
    (cd "$BUILD" && patch -p1 < "$HERE/max_payload.patch")
    make -C "/lib/modules/$KVER/build" M="$BUILD" modules
    modinfo "$BUILD/uvcvideo.ko" | grep -E "vermagic|max_payload"
}

need_module() {
    [ -f "$BUILD/uvcvideo.ko" ] || { echo "run '$0 build' first"; exit 1; }
}

case "$1" in
build)
    build
    ;;
try)
    need_module
    # modprobe first so the dependencies (videobuf2, videodev, mc) are loaded;
    # rmmod then drops only uvcvideo, and insmod does not resolve dependencies.
    sudo modprobe uvcvideo
    sudo rmmod uvcvideo || { echo "uvcvideo is in use: stop the capture container first"; exit 1; }
    sudo insmod "$BUILD/uvcvideo.ko" max_payload="$MAX_PAYLOAD"
    "$0" status
    ;;
install)
    need_module
    sudo install -D -m 644 "$BUILD/uvcvideo.ko" "/lib/modules/$KVER/updates/uvcvideo.ko"
    echo "options uvcvideo max_payload=$MAX_PAYLOAD" | sudo tee /etc/modprobe.d/uvcvideo-bw.conf >/dev/null
    sudo depmod -a
    echo "installed: $(modinfo -n uvcvideo) (active from next load or reboot)"
    ;;
uninstall)
    sudo rm -f "/lib/modules/$KVER/updates/uvcvideo.ko" /etc/modprobe.d/uvcvideo-bw.conf
    sudo depmod -a
    echo "uninstalled: $(modinfo -n uvcvideo) (active from next load or reboot)"
    ;;
status)
    echo "on disk : $(modinfo -n uvcvideo)"
    if [ -f /sys/module/uvcvideo/parameters/max_payload ]; then
        echo "loaded  : patched, max_payload=$(cat /sys/module/uvcvideo/parameters/max_payload)"
    elif [ -d /sys/module/uvcvideo ]; then
        echo "loaded  : stock (no bandwidth cap)"
    else
        echo "loaded  : none"
    fi
    ;;
*)
    sed -n '2,13p' "$0"
    exit 1
    ;;
esac
