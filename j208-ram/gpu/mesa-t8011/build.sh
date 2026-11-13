#!/bin/sh
# Build Orchard's Mesa fork (PowerVR Vulkan driver with the G9P backend) for
# the A10X, plus the fork's vk-tri, vk-compute-test and kms-grab, for the J208's
# Debian trixie.
#
#   sh build.sh
#
# The sources are pinned: OrchardLinux/mesa at MESA_COMMIT with the 0*.patch
# patches here (0001 A10X device info, 0002 a varying for vertex shaders
# without one, 0003 vertex-shader register overrides for debugging, 0004
# vertex-shader registers in groups of 4 in VDM_STATE, 0005 Zink's scanout
# dumb buffers with a 64-byte-aligned pitch, for build-zink.sh, 0006
# vkMapMemory returning the mapping the G9P backend made),
# OrchardLinux/gpu at GPU_COMMIT for vk-tri, vk-compute-test and kms-grab.
# They are fetched into the Docker volume j208-mesa-build, so later builds
# are incremental. Output: build/ (stripped driver, ICD JSON, test programs) and
# the SHA-256 of each file.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
MESA_COMMIT=73a49abe24726c703ff32f060a9b7cd0c4444ce2
GPU_COMMIT=42f6add03616d065d137e60c2cd7fde8780e5203
IMAGE=j208-mesa-g9p:trixie

docker build -q -t "$IMAGE" -f "$here/Containerfile" "$here" >/dev/null
mkdir -p "$here/build"
docker run --rm -v j208-mesa-build:/work -v "$here:/src:ro" -v "$here/build:/out" \
    -e MESA_COMMIT=$MESA_COMMIT -e GPU_COMMIT=$GPU_COMMIT "$IMAGE" sh -eu -c '
    fetch() { # DIR URL COMMIT
        if [ "$(git -C "$1" rev-parse HEAD 2>/dev/null)" != "$3" ]; then
            rm -rf "$1"; git init -q "$1"
            git -C "$1" fetch -q --depth 1 "$2" "$3"
            git -C "$1" -c advice.detachedHead=false checkout -q FETCH_HEAD
        fi
    }
    fetch /work/gpu https://github.com/OrchardLinux/gpu.git $GPU_COMMIT
    # the patched tree is rebuilt whenever a patch changes
    sum=$(cat /src/0*.patch | sha256sum | cut -c1-64)
    if [ "$(cat /work/mesa.patch-sum 2>/dev/null)" != "$sum" ]; then
        fetch /work/mesa https://github.com/OrchardLinux/mesa.git $MESA_COMMIT
        git -C /work/mesa reset -q --hard $MESA_COMMIT
        git -C /work/mesa clean -fdq -e /build -e /build-zink   # files a patch added
        for p in /src/0*.patch; do git -C /work/mesa apply $p; done
        echo "$sum" > /work/mesa.patch-sum
    fi
    cd /work/mesa
    [ -d build ] || meson setup build -Dvulkan-drivers=imagination -Dgallium-drivers= \
        -Dplatforms= -Dglx=disabled -Degl=disabled -Dgbm=disabled \
        -Dbuildtype=debugoptimized -Dprefix=/usr >/dev/null
    ninja -C build >/dev/null || { ninja -C build | grep -E "error|FAILED"; exit 1; }
    strip --strip-unneeded -o /out/libvulkan_powervr_mesa.so \
        build/src/imagination/vulkan/libvulkan_powervr_mesa.so
    sed "s|\"library_path\": \"[^\"]*\"|\"library_path\": \"/run/j208-mesa/libvulkan_powervr_mesa.so\"|" \
        build/src/imagination/vulkan/powervr_mesa_devenv_icd.*.json > /out/powervr_icd.json

    t=/work/gpu/tools/a10/mesa
    for s in $t/shaders/gfx/*; do
        n=$(basename $s | tr . _)
        glslangValidator -V --vn ${n}_spv -o /tmp/$n.spv.h $s >/dev/null
    done
    for s in $t/shaders/*.comp; do
        n=$(basename $s .comp)
        glslangValidator -V --vn ${n}_spv -o /tmp/$n.spv.h $s >/dev/null
    done
    for p in vk-tri vk-compute-test; do
        gcc -O2 -Wall -ffp-contract=off -I/tmp -o /out/$p $t/$p.c -lvulkan -lm
        strip /out/$p
    done
    # what the display scans out (simpledrm), for the GPU desktop (--labwc)
    gcc -O2 -Wall -o /out/kms-grab $t/kms-grab.c $(pkg-config --cflags --libs libdrm)
    strip /out/kms-grab
'
cd "$here/build"
shasum -a 256 libvulkan_powervr_mesa.so powervr_icd.json vk-tri vk-compute-test kms-grab
