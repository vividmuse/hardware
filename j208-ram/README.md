# J208 RAM bring-up code: GPU

This is bring-up code from RAM desktop sessions on an iPad Pro 10.5 Cellular
(J208, A10X/T8011, iPadOS 17.7.11 21H461). It is not meant for the tree as
it is. The facts it found are in `docs/bring-up/j208.md`, and the open work
for real drivers is in `devices/j208/README.md` (item 13) on OrchardLinux main.

It ran against the RAM image's kernel `7.3.0-rc1-orchard-j208`, which is
built from Hoolock `6831bc701` plus the old J71b series. The scripts call
helpers from the contributor's RAM-boot workspace (session checks, SSH and
serial transport), which are not part of this copy. Read them as a record of
what was done.

## `gpu/pvr-t8011`: kernel side

| File | What it is |
| --- | --- |
| `assemble.sh`, `build.sh` | Copy the image kernel's imagination, RTKit and mailbox sources. Apply T8010 patches `drivers/gpu/0001`, `soc-apple/0007` and `soc-apple/0013`, each pinned by SHA-256. Rename RTKit and the mailbox to private symbols, apply `0001` and `0002`, and build three modules with `W=1` |
| `0002-drm-imagination-t8011.patch` | The T8011 delta to `pvr_apple`: `struct pvr_apple_soc` per compatible (firmware, runtime window `0x90000`, 12 clusters, fastest state 4, bridges), the bridge 7/9 writes, and the longer per-cluster register lists |
| `0001-j208-mailbox-t8011-only.patch` | The private mailbox refuses to probe unless it is already in the GFX power domain |
| `j208-gpu-dt.c` | Runtime OF changeset that adds the mailbox and GPU nodes. Because the RAM DTB has no phandle on `ps_gfx`/`ps_dwi`, it makes GFX a subdomain of DWI and holds DWI on, then puts both devices in the GFX domain |
| `run-pvr-t8011.py`, `pvr-compute-job.py` | The device runner: probe, compute jobs, render suite, GL, labwc on the GPU |
| `gpu-desktop.py`, `restore-desktop.py` | Switch the running desktop to the GPU, and back to pixman |
| `README.md` | Every device run: power-off hang and its DWI root cause, the fragment hang and the cluster lists, render and GL bisects, benchmarks |

Load order: `j208-gpu-dt`, then `j208-rtkit`, then `j208-powervr
exp_hw_support=1`.

## `gpu/mesa-t8011`: userspace

These patches apply to OrchardLinux/mesa `73a49abe`. `build.sh` builds the
Vulkan driver and `build-zink.sh` builds Zink, both in a Debian trixie
container matching the device's libraries.

| Patch | Change |
| --- | --- |
| `0001` | A10X device info, BVNC 13.7.12.2 |
| `0002` | A vertex shader without varyings writes varying 0. It took effect, but whether it is needed is not known |
| `0003` | Debug overrides for the vertex-shader register counts. Only used for the bisect |
| `0004` | `VDM_STATE` vertex-shader registers allocated in groups of four. This was the fix for blank Zink draws |
| `0005` | kmsro dumb buffers for Zink with a 64-byte aligned pitch |
| `0006` | `vkMapMemory` returns the mapping the G9P backend already holds |

`gl-probe.c`, `frame-count.c` and `shaders/` are the test programs the
runner used.
