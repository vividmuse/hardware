#!/bin/sh
# Assemble the out-of-tree source in OUT; runs in the toolchain container.
#
#   1. copy the image kernel's imagination driver, RTKit and Apple mailbox;
#   2. apply the T8010 patches (pinned): pvr_apple, the A7IOP mailbox, and
#      apple_rtkit_mbox_stop;
#   3. rename RTKit and the mailbox to j208_rtkit_* / j208_mbox_* with the
#      header at <j208/rtkit.h>: they load as a module next to the kernel's
#      own (built-in) copies;
#   4. apply 0001 (mailbox: T8011 GFX only) and 0002 (pvr_apple: T8011);
#   5. write the Kbuild files: j208-rtkit.ko, j208-powervr.ko, j208-gpu-dt.ko.
#
# Usage: assemble.sh SRC PATCHES HERE OUT
set -eu
src=$1 patches=$2 here=$3 out=$4

check() {
    echo "$2  $1" | sha256sum -c --quiet - || { echo "$1 differs" >&2; exit 1; }
}
t8010_gpu=$patches/drivers/gpu/0001-drm-imagination-Support-the-Apple-A10-T8010-GPU.patch
t8010_mbox=$patches/drivers/soc-apple/0007-soc-apple-mailbox-Add-the-T8010-A7-IOP-mailbox.patch
t8010_rtkit=$patches/drivers/soc-apple/0013-soc-apple-rtkit-Let-a-shut-down-co-processor-s-mailb.patch
check "$t8010_gpu" 740404685d9942eb9722133e2c17966127e171d439cf8d2d01f5d1e24459cc6b
check "$t8010_mbox" 10303d68c5043271449d5614e742c370397f8103ff55011900e80d89dc1b974d
check "$t8010_rtkit" 0c1408e5aa3639d9e3a1d7f579f0e7d7c820b4b96ea189529ef6c82e2101f585

rm -rf "$out"
mkdir -p "$out/drivers/gpu/drm" "$out/drivers/soc/apple" "$out/include/linux/soc/apple"
cp -r "$src/drivers/gpu/drm/imagination" "$out/drivers/gpu/drm/"
for f in rtkit.c rtkit-crashlog.c rtkit-internal.h mailbox.c mailbox.h; do
    cp "$src/drivers/soc/apple/$f" "$out/drivers/soc/apple/"
done
cp "$src/include/linux/soc/apple/rtkit.h" "$out/include/linux/soc/apple/"

cd "$out"
git apply "$t8010_gpu" "$t8010_mbox" "$t8010_rtkit"

mkdir include/j208
mv include/linux/soc/apple/rtkit.h include/j208/rtkit.h
rmdir include/linux/soc/apple include/linux/soc include/linux
perl -pi -e '
    s/(?<![A-Za-z0-9_])(devm_)?apple_(rtkit|mbox)/$1j208_$2/g;
    s/(?<![A-Za-z0-9_])APPLE_(RTKIT|MBOX)/J208_$1/g;
    s/_LINUX_APPLE_RTKIT_H_/_J208_RTKIT_H_/g;
    s#<linux/soc/apple/rtkit.h>#<j208/rtkit.h>#g;
' drivers/soc/apple/rtkit.c drivers/soc/apple/rtkit-crashlog.c \
  drivers/soc/apple/rtkit-internal.h drivers/soc/apple/mailbox.c \
  drivers/soc/apple/mailbox.h include/j208/rtkit.h \
  drivers/gpu/drm/imagination/pvr_apple.c

git apply "$here/0001-j208-mailbox-t8011-only.patch" "$here/0002-drm-imagination-t8011.patch"

cp "$here/j208-gpu-dt.c" .
cat > Kbuild <<'EOF'
obj-m += j208-rtkit.o j208-gpu-dt.o drivers/gpu/drm/imagination/
j208-rtkit-y := drivers/soc/apple/rtkit.o drivers/soc/apple/rtkit-crashlog.o \
	drivers/soc/apple/mailbox.o
ccflags-y += -I$(J208_TREE)/include
EOF
# powervr as j208-powervr, with pvr_apple built in
perl -pi -e '
    s/^powervr-/j208-powervr-/;
    s/^obj-\$\(CONFIG_DRM_POWERVR\) \+= powervr\.o$/obj-m += j208-powervr.o/;
' drivers/gpu/drm/imagination/Makefile
grep -q '^obj-m += j208-powervr.o$' drivers/gpu/drm/imagination/Makefile
{
    printf '%s\n' '# j208 out-of-tree build: pvr_apple on, private RTKit header' \
        'CONFIG_DRM_POWERVR_APPLE := y' \
        'ccflags-y += -DCONFIG_DRM_POWERVR_APPLE=1 -I$(J208_TREE)/include'
    cat drivers/gpu/drm/imagination/Makefile
} > Makefile.tmp
mv Makefile.tmp drivers/gpu/drm/imagination/Makefile

# Nothing outside these may still name the kernel's RTKit or mailbox
if grep -rnE '(^|[^A-Za-z0-9_])(apple_rtkit|apple_mbox)|soc/apple/rtkit\.h' \
        drivers/soc include drivers/gpu/drm/imagination/pvr_apple.c; then
    echo 'kernel RTKit/mailbox names left' >&2
    exit 1
fi
echo "assembled $out"
