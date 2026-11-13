#!/bin/sh
# Assemble (assemble.sh) and build j208-rtkit.ko, j208-powervr.ko and
# j208-gpu-dt.ko against the image kernel, W=1. Output: build/tree/.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
patches=$(CDPATH= cd -- "$here/../../../../hardware-j208-charging/kernel/patches" && pwd)
docker run --rm -v j208-linux-build:/work:ro -v "$here:/obj" -v "$patches:/patches:ro" \
    j208-kernel-toolchain:20261027 sh -eu -c '
        sh /obj/assemble.sh /work/source /patches /obj /obj/build/tree
        make -C /work/source O=/work/build M=/obj/build/tree J208_TREE=/obj/build/tree \
            ARCH=arm64 LLVM=-19 LLVM_IAS=1 LOCALVERSION=-orchard-j208 W=1 modules
        # modules of the image kernel that j208-powervr needs
        mkdir -p /obj/build/deps
        cp /work/build/drivers/gpu/drm/scheduler/gpu-sched.ko \
            /work/build/drivers/gpu/drm/drm_exec.ko \
            /work/build/drivers/gpu/drm/drm_gpuvm.ko /obj/build/deps/
    '
cd "$here/build/tree"
shasum -a 256 j208-rtkit.ko j208-gpu-dt.ko drivers/gpu/drm/imagination/j208-powervr.ko ../deps/*.ko
