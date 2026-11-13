# Mesa for the A10X (T8011)

This directory holds the userspace for rendering on J208. It uses Orchard's
Mesa fork ([OrchardLinux/mesa](https://github.com/OrchardLinux/mesa)): Mesa's
PowerVR Vulkan driver with a G9P backend, the same one that runs Hyprland on
the A10 (T8010) through Zink. The test program is the fork's `vk-tri`
([OrchardLinux/gpu](https://github.com/OrchardLinux/gpu)
`tools/a10/mesa/vk-tri.c`). It clears a target, draws one vertex-coloured
triangle through a geometry job and a fragment job, reads the pixels back,
and prints `PASS` or `FAIL`.

| File | What |
| --- | --- |
| `0001-pvr-Apple-A10X-T8011-BVNC-13.7.12.2.patch` | Adds the A10X device info: BVNC 13.7.12.2, 12 clusters. Every other value is copied from the A10 (`apple-a10.h`). |
| `0002-pvr-g9p-vertex-shaders-without-varyings.patch` | Gives a vertex shader without varyings one (varying 0). It did not make those draws work on T8011 (`../pvr-t8011/README.md`, GL run 5). |
| `0003-pvr-g9p-vertex-shader-register-overrides.patch` | Debugging only: `G9P_VS_MIN_REGS` raises a vertex shader's register allocation in the kick's temps and `VDM_STATE`; `G9P_VS_KICK_REGS` and `G9P_VS_VDM_REGS` raise it in only one of them. Unset, nothing changes. |
| `0004-pvr-g9p-vertex-shader-registers-in-groups-of-4.patch` | The fix for T8011's blank draws: `VDM_STATE` allocates a vertex shader's registers in whole groups of 4, as the kick's temps already do. With 10 registers (5 units of 2) a draw drew nothing (`../pvr-t8011/README.md`, GL run 6). |
| `0005-kmsro-zink-dumb-buffers-with-a-64-byte-aligned-pitch.patch` | For the desktop on the GPU: kmsro pairs simpledrm with Zink and makes Zink's scanout buffers as simpledrm dumb buffers. G9P render targets hold the row pitch in 64-byte units, and a dumb buffer as wide as the J208's display (1668 pixels, 6672 bytes) is not; the Vulkan driver refused it (`../pvr-t8011/README.md`, desktop run 1). The dumb buffer is made wider; the resource keeps its width. |
| `0006-pvr-vkMapMemory-returns-the-G9P-backend-mapping.patch` | The G9P backend's CPU copies map device memory (`g9p_cpu_addr()`) and keep it mapped; an application's later `vkMapMemory` of that memory failed with `VK_ERROR_MEMORY_MAP_FAILED` ("already mapped"). Zink logged it 57 times for its staging memory under the GPU desktop (`../pvr-t8011/README.md`, kept run 1). `vkMapMemory` now returns the existing mapping; a placed map replaces it. No gl-probe run reproduced the failure (see the same-boot A/B in `../pvr-t8011/README.md`), but kept run 2 confirmed both the cause and the fix: 5 maps got the backend's mapping, and 0 failed. With `G9P_TRACE` set, the driver logs each map that got the backend's mapping, and each map that failed with its errno. |
| `Containerfile` | The fork's build image, unchanged (`tools/a10/mesa/Containerfile`): Debian trixie arm64, the same release as the J208 image. |
| `build.sh` | Fetches the pinned commits into the Docker volume `j208-mesa-build`, applies every `0*.patch`, builds the driver (`debugoptimized`, Vulkan only, no WSI) and `vk-tri`, and prints the SHA-256 of each output. |
| `build/` | `libvulkan_powervr_mesa.so` (stripped), `powervr_icd.json` (pointing at `/run/j208-mesa`), `vk-tri`, `vk-compute-test` (the fork's 12 compute shaders, each checked against a CPU reference), and `kms-grab` (what simpledrm scans out, as a PPM, for `--labwc`). |

The commits are pinned in `build.sh`:
- Mesa `73a49abe` ("pvr/g9p: no SPM scratch buffer", the fork's `main`).
- gpu `42f6add0`.

## Why a device-info entry is enough to start

`fw-compare` (`../fw-compare/README.md`) found the following:
- The TA and 3D command formats are the same in the T8010 and T8011
  firmware.
- So are the microsequence interpreter and the Start3D and free-list
  paths.

The fork's G9P code writes the per-cluster blocks of the geometry and
fragment commands as fixed 6-entry tables with the same value in every
entry. Neither the firmware nor the command layout ties them to 6 clusters.

The kernel already reports 12 clusters and 3 phantoms for T8011. Mesa uses
the cluster count for compute shared memory and for the tiles in flight per
phantom.

What only a run can tell:
- whether one hardware unit per engine is enough on 12 clusters (queue
  `+0x44 = 1`);
- whether the GX6650 placeholder values hold.

## Running

`../pvr-t8011/run-pvr-t8011.py --render`, with the confirmation
`render-pvr-t8011-once`, on a fresh boot after stage A. It does the
following:
1. Runs the compute jobs.
2. Waits for the GPU to autosuspend.
3. Uploads `build/` to `/run/j208-mesa`.
4. Runs `vk-tri` with `VK_DRIVER_FILES` pointing at that ICD and
   `PVR_I_WANT_A_BROKEN_VULKAN_DRIVER=1`.

`vk-tri` runs at three sizes: 64x64 (the A10's first frame), 512x512, and
the full panel at 1668x2224. The run stops at the first size that does not
pass. Afterwards the GPU must autosuspend again, and then the modules are
unloaded.

## OpenGL ES through Zink (`build-zink.sh`)

`build-zink.sh` builds the same pinned tree as `build.sh`, in its own build
directory `build-zink`, with:
- the Vulkan driver;
- Gallium Zink;
- EGL (a glvnd vendor library);
- GBM;
- the Wayland platform.

LLVM is used only at build time, for the driver's CLC shaders. Assertions
are off (`-Db_ndebug=true`), as in the distribution builds of Mesa the A10
desktop runs: Zink asserts that a driver has a depth-stencil format, and
G9P has none yet (`../pvr-t8011/README.md`, "GL run 1"). The
install prefix is `/run/j208-zink`. The device's own Mesa (trixie 25.0.7)
stays as it is. A client gets this build through the environment:

```sh
LD_LIBRARY_PATH=/run/j208-zink/lib \
__EGL_VENDOR_LIBRARY_FILENAMES=/run/j208-zink/share/glvnd/egl_vendor.d/50_mesa.json \
VK_DRIVER_FILES=/run/j208-zink/share/vulkan/icd.d/powervr_mesa_icd.aarch64.json \
MESA_LOADER_DRIVER_OVERRIDE=zink PVR_I_WANT_A_BROKEN_VULKAN_DRIVER=1
```

With that environment:
- libgbm finds its backend under the prefix;
- glvnd's `libEGL.so.1` and `libGLESv2.so.2` load this `libEGL_mesa`;
- Zink loads this Vulkan driver.

| File | What |
| --- | --- |
| `Containerfile.zink` | `Containerfile` plus the Wayland, glvnd, EGL and GLES development packages. |
| `gl-probe.c` | An off-screen GLES 2 check through GBM on the render node; see `../pvr-t8011/README.md`, "GL mode". |
| `build-zink/j208-zink.tar.gz` | The stripped install tree, 14 files, 33 MB unpacked: `libgallium` (Zink), `libEGL_mesa`, `libgbm` and its `dri_gbm` backend, the Vulkan driver, and the manifests. The archive has fixed owners and times. |
| `build-zink/gl-probe` | |
| `frame-count.c` | A Wayland client that counts a compositor's output frames over a period (`wp_presentation`), for the GPU desktop's benchmark; see `../pvr-t8011/README.md`, "Pixman's frames". |
| `build-zink/frame-count` | |

`--sort=name --mtime=@0` and `gzip -n` keep the archive's hash tied to its
content. The runner pins the archive and the programs.
