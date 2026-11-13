#!/bin/sh
# Build OpenGL (ES) for the J208: the same pinned Mesa fork and A10X patch as
# build.sh, with Zink, EGL (glvnd vendor library), GBM and the Wayland
# platform, plus gl-probe and frame-count, for the J208's Debian trixie.
#
#   sh build-zink.sh
#
# Mesa is configured with the prefix /run/j208-zink, where the runner unpacks
# it on the device: libgbm finds its backend, the Vulkan loader manifest its
# driver and the EGL vendor manifest its library there, with no paths to
# override. Clients reach it with
#   LD_LIBRARY_PATH=/run/j208-zink/lib
#   __EGL_VENDOR_LIBRARY_FILENAMES=/run/j208-zink/share/glvnd/egl_vendor.d/50_mesa.json
#   VK_DRIVER_FILES=/run/j208-zink/share/vulkan/icd.d/powervr_mesa_icd.aarch64.json
#   MESA_LOADER_DRIVER_OVERRIDE=zink PVR_I_WANT_A_BROKEN_VULKAN_DRIVER=1
# The device's own Mesa (trixie 25.0) stays as it is.
#
# Shares build.sh's source tree in the Docker volume j208-mesa-build and builds
# in build-zink next to its build directory. Output: build-zink/ with
# j208-zink.tar.gz (the stripped install tree), gl-probe, frame-count, the SPIR-V of
# shaders/ for vk-tri, and their SHA-256.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
MESA_COMMIT=73a49abe24726c703ff32f060a9b7cd0c4444ce2
IMAGE=j208-mesa-g9p:trixie-zink

docker build -q -t j208-mesa-g9p:trixie -f "$here/Containerfile" "$here" >/dev/null
docker build -q -t "$IMAGE" -f "$here/Containerfile.zink" "$here" >/dev/null
mkdir -p "$here/build-zink"
docker run --rm -v j208-mesa-build:/work -v "$here:/src:ro" -v "$here/build-zink:/out" \
    -e MESA_COMMIT=$MESA_COMMIT "$IMAGE" sh -eu -c '
    fetch() { # DIR URL COMMIT
        if [ "$(git -C "$1" rev-parse HEAD 2>/dev/null)" != "$3" ]; then
            rm -rf "$1"; git init -q "$1"
            git -C "$1" fetch -q --depth 1 "$2" "$3"
            git -C "$1" -c advice.detachedHead=false checkout -q FETCH_HEAD
        fi
    }
    # the patched tree is rebuilt whenever a patch changes (as in build.sh)
    sum=$(cat /src/0*.patch | sha256sum | cut -c1-64)
    if [ "$(cat /work/mesa.patch-sum 2>/dev/null)" != "$sum" ]; then
        fetch /work/mesa https://github.com/OrchardLinux/mesa.git $MESA_COMMIT
        git -C /work/mesa reset -q --hard $MESA_COMMIT
        git -C /work/mesa clean -fdq -e /build -e /build-zink   # files a patch added
        for p in /src/0*.patch; do git -C /work/mesa apply $p; done
        echo "$sum" > /work/mesa.patch-sum
    fi
    cd /work/mesa
    # b_ndebug=true: assertions off, as in the distribution builds of Mesa the A10
    # desktop runs Zink from. Zink asserts that a driver offers D24S8 or
    # D32S8 (zink_get_format); G9P has neither yet, and without the
    # assertion Zink reports the format unsupported (runs/20261109-213456).
    opts="--prefix=/run/j208-zink --libdir=lib \
        -Dbuildtype=debugoptimized -Db_ndebug=true \
        -Dvulkan-drivers=imagination -Dgallium-drivers=zink \
        -Dplatforms=wayland -Degl=enabled -Dgbm=enabled -Dglvnd=enabled -Dglx=disabled \
        -Dopengl=true -Dgles1=disabled -Dgles2=enabled -Dllvm=enabled -Ddraw-use-llvm=false \
        -Dgallium-va=disabled -Dgallium-rusticl=false \
        -Dvideo-codecs= -Dtools= -Dvulkan-layers= -Dlmsensors=disabled \
        -Dvalgrind=disabled -Dlibunwind=disabled"
    # set up again whenever the options change
    if [ ! -f build-zink/build.ninja ] || [ "$(cat build-zink/j208-options 2>/dev/null)" != "$opts" ]; then
        rm -rf build-zink; meson setup build-zink $opts >/tmp/setup.log || { tail -30 /tmp/setup.log; exit 1; }
        printf %s "$opts" > build-zink/j208-options
    fi
    ninja -C build-zink >/tmp/ninja.log || { grep -E "error|FAILED" /tmp/ninja.log | head -40; exit 1; }
    rm -rf /tmp/stage
    DESTDIR=/tmp/stage meson install -C build-zink --quiet --no-rebuild >/dev/null
    find /tmp/stage -name "*.so*" -type f -exec strip --strip-unneeded {} +
    gcc -O2 -Wall -o /out/gl-probe /src/gl-probe.c \
        -I/tmp/stage/run/j208-zink/include $(pkg-config --cflags libdrm) \
        -L/tmp/stage/run/j208-zink/lib -lgbm -lEGL -lGLESv2 -ldrm
    strip /out/gl-probe
    # frame-count: Labwc'"'"'s output frames as a client sees them (the benchmark)
    wp=/usr/share/wayland-protocols/stable
    for x in xdg-shell/xdg-shell presentation-time/presentation-time; do
        n=$(basename $x)
        wayland-scanner client-header $wp/$x.xml /tmp/$n-client-protocol.h
        wayland-scanner private-code $wp/$x.xml /tmp/$n-protocol.c
    done
    gcc -O2 -Wall -I/tmp -o /out/frame-count /src/frame-count.c /tmp/xdg-shell-protocol.c \
        /tmp/presentation-time-protocol.c -lwayland-client
    strip /out/frame-count
    # SPIR-V for vk-tri (TRI_VS_SPV, TRI_FS_SPV): shaders/*.vert, *.frag
    for f in /src/shaders/*.vert /src/shaders/*.frag; do
        glslangValidator -V -o /out/$(basename $f | tr . -).spv $f >/dev/null
    done
    # headers, pkg-config files and the wayland-protocols subproject are build-only
    rm -rf /tmp/stage/run/j208-zink/include /tmp/stage/run/j208-zink/lib/pkgconfig \
        /tmp/stage/run/j208-zink/share/pkgconfig /tmp/stage/run/j208-zink/share/wayland-protocols
    # one archive with fixed owners and times, so its hash follows the content
    tar -C /tmp/stage/run/j208-zink --sort=name --owner=0 --group=0 --numeric-owner \
        --mtime=@0 -cf - . | gzip -n -9 > /out/j208-zink.tar.gz
    tar -tzvf /out/j208-zink.tar.gz | awk "{ s += \$3 } END { printf \"install tree: %d files, %.1f MB\n\", NR, s / 1048576 }"
    tar -tzf /out/j208-zink.tar.gz | grep -v "/$" | sed "s|^\./|  |"
    echo "needed:"; for f in $(find /tmp/stage -name "*.so*" -type f) /out/gl-probe; do
        readelf -d $f | sed -n "s/.*NEEDED.*\[\(.*\)\]/\1/p"; done | sort -u | tr "\n" " "; echo
'
cd "$here/build-zink"
shasum -a 256 j208-zink.tar.gz gl-probe frame-count *.spv
