# iPad Pro 10.5 (Cellular), J208

Measured bring-up facts, including the hardware identity, are in
[`docs/bring-up/j208.md`](../../docs/bring-up/j208.md). This file tracks what
needs testing, open work, and contributed code that is not in the tree yet.
Its `device.toml` holds identity only, from public sources: it has no `[kernel]`
until the firmware recipe is written (open work 10).

## Needs testing

Nothing J208-specific is in the kernel series yet, and without `[kernel]` in
its descriptor Orchard doesn't build for the J208. Once it has one, a RAM boot of
`examples/hardware-check` with the mainline `t8011-j208` device tree (SoC,
framebuffer, buttons) would confirm display and buttons.

## Open work

From the review of #42. Each needs a test on a J208:

1. Wi-Fi: `pcie-apple-h9p` T8011 support through match data and the
   device tree (port 2 and its tunables), without machine checks or DMA test
   gates.
2. Wi-Fi: the BCM4355 rev 10 (`14e4:43dc`) Apple profile in brcmfmac
   through the existing OTP and board selection, without J208 checks in
   shared code.
3. A J208 device tree beyond mainline's (`arm64: dts: apple: t8011-j208`):
   the PMICs, touch SPI device, PCIe Wi-Fi, UART5/HDQ battery and I2C3
   light sensors used in the RAM sessions.
4. Touch: an `apple_z2` J208 variant that reports input (the RAM driver
   only captured frames), plus its binding.
5. Backlight: a WLED cell in the Dialog PMU driver (`040-dialog`) for the
   D2400 registers, instead of direct writes.
6. Power: the D2400 LDO13 and D2401 WLAN GPIO1 in the existing PMU
   regulator driver. (The RAM module used mask `0x19` while writing 0 or 1,
   which clears bits 3–4; check that.)
7. Battery: a bq27xxx chip/profile entry for the BQ27540 behaviour seen
   here (remaining capacity at `0x10` answers, NAC at `0x0c` does not),
   instead of a runtime table swap.
8. Light sensor: an IIO driver for the I2C3 parts (`0x29`, `0x39`; the
   register map looks TCS34xx-like, check against `tcs3472`).
9. SmartIO: an `apple,t8011-sio` compatible, without the firmware-size gate.
10. A firmware recipe from iPadOS 17.7.11 (21H461), from the IPSW itself, and
    then the rest of the device descriptor. #42's descriptor is at the tag
    (`devices/j208/device.toml`); its identity values are in the bring-up
    doc. When it comes back: touch, Wi-Fi, battery and backlight are
    `missing` until their drivers are in the tree, and mainline does have
    `t8011-j208.dts` (SoC, framebuffer, buttons).
11. Shared SPI word-size setting: the RAM patch changed
    `APPLE_SPI_T8010_DESC_WORD_BITS << word_index` to
    `word_index << APPLE_SPI_T8010_DESC_WORD_BITS` in `spi-apple`. That
    also changes J71b touch (8-bit setting `0x0f` → `0`). Check on J71b
    before any change.
12. Charging: the D2355 in `apple_tristar_charger` (facts in the bring-up
    doc). The driver's PMIC side is D2365-only today: the status offset,
    the input-limit register and the code↔mA formula (12.5 mA steps) are
    constants, and its match table has no data. Add per-chip match data
    under a chip compatible (status at byte 6 of the `0x80` block, limit at
    `0x44e` in 10 mA steps, floor `0x08`), a binding update, and a
    `t8011-j208` node: the D2355 at `0x75` as an `apple,i2c-pmic` parent,
    the Tristar at `0x1a`, the USB port link. Each needs a J208 test:
    - BrickID and Type-C CC sampling read an ADC on the charger's regmap;
      J208's ADT routes BrickID to the D2400, so the driver needs a second
      ADC source.
    - Withdrawal on unplug uses PMU event lines from the D2365 event
      controller; J208 has none in the kernel yet. Until then, polling the
      `0x80` status (500 ms worked) or the D2355's IRQ 204.
    - The Tristar mux and BC1.2 writes were never done on J208; only its
      capability frame was read.
13. GPU: T8011 in `pvr_apple` through match data. Facts are in the bring-up
    doc. The match data needs:
    - the firmware name;
    - runtime window `0x90000`;
    - 12 clusters, with the per-cluster and per-pipe register lists
      (3D 42, TA 34);
    - fastest state 4;
    - the bridge 7/9 writes on every power-up;
    - BVNC 13.7.12.2 decoded from `CORE_ID`/`CORE_REVISION`.

    Beyond the match data:
    - an `apple,t8011-a7iop-mailbox` compatible;
    - a `t8011` GPU node whose domain keeps `ps_dwi` on, since the J208 has
      no `dwi_bl`;
    - an OPP table from the ADT `perf-states`.

    The Mesa changes (A10X device info, vertex-shader registers in groups
    of four, the kmsro pitch, `vkMapMemory` reuse) go to OrchardLinux/mesa.
    The last three apply to every G9P, so check them on J71b.
14. Speakers: four MAX98723 on I2C1 and I2C2.
    - A MAX98723 entry in `max98927` (match data for the shifted
      `0x21`–`0x26` block, and the enable order).
    - T8011 MCA2/NCO2 in the S8000/T8010 MCA driver (16×16-bit TDM).
    - I2C1 enabled in the J208 tree, with the four amps on it and on I2C2
      and their shared reset GPIO `0x77`.
    - The CS42L83 codec additionally needs the AOP I2S switch.
15. Bluetooth: the J71t BCM4355C0 match entry for a J208 compatible, and a
    UART3 node powered by D2401 GPIO0. Check whether the ADT calibration
    and power table matter.
16. Internal storage: `pcie-apple-h9p` port 0 on T8011, in the native
    order:
    - NVMe mode, then the port;
    - the NVMMU reset polled on bit 16.

    Then `apcie_nvme` in m1n1 for T8011. A port-0-only enumeration module
    is written but has not run.

## Unmerged code

PR #42 brought the drivers from the J208 RAM desktop sessions under
`devices/j208/drivers/`. They are not in the tree: the patches are bare diffs
against Hoolock `6831bc701` that don't apply to the current series, they
gate shared drivers on `apple,j208`, and several modules write PMIC
registers directly. Everything is preserved in the contributor's commit
`a8d54e3`, tagged [`contrib/vividmuse-j208`](https://github.com/OrchardLinux/orchard/tree/contrib/vividmuse-j208/devices/j208):

| Path at the tag | Contents |
| --- | --- |
| `drivers/patches/0001` | SmartIO `t8011-sio` compatible, firmware-size gate |
| `drivers/patches/0002`, `0003` | SPI word-size shift (shared), J208 PIO timing |
| `drivers/patches/0004`, `0005` | PCIe port 2 for Wi-Fi with T8011 tunables (21H461), WLAN UART provider |
| `drivers/patches/0006` | brcmfmac BCM4355 profile (worked: WPA2 association) |
| `drivers/patches/0007` | `apple_z2` J208 frame capture (diagnostic, no input device) |
| `drivers/modules/backlight/` | WLED gate and level (D2400 `0x307`–`0x309`, `0xb00`/`0xb01`) |
| `drivers/modules/battery/` | UART5/HDQ overlay, bq27xxx register-table swap |
| `drivers/modules/als/` | I2C3 enable overlay, identity, config and sample readers |
| `drivers/modules/pmic/` | D2400 LDO13 and D2401 GPIO1 regulator stubs |
| `device.toml`, `firmware.json` | descriptor and a recipe placeholder (not schema-valid) |
| `ram-kernel.config` | the RAM kernel's resolved configuration |
| `tests/test_j208_drivers.py` (repo root) | checks of the layout above |

Get a file with `git show contrib/vividmuse-j208:devices/j208/drivers/patches/0006-j208-brcmfmac-profile.patch`.

The GPU work from the later sessions (open work 13) is on the contributor's
fork, branch
[`j208-ram-drivers-20261113`](https://github.com/vividmuse/hardware/tree/j208-ram-drivers-20261113/j208-ram).
It is RAM bring-up code: out-of-tree modules, a runtime OF changeset and
device runners. It is not meant for the tree as it is.

| Path on the branch | Contents |
| --- | --- |
| `j208-ram/gpu/pvr-t8011/0002-drm-imagination-t8011.patch` | T8011 delta on top of `drivers/gpu/0001` (per-SoC struct, bridges, cluster lists) |
| `j208-ram/gpu/pvr-t8011/0001-j208-mailbox-t8011-only.patch` | the private mailbox copy (`apple,t8011-a7iop-mailbox`) refuses to probe outside the GFX domain |
| `j208-ram/gpu/pvr-t8011/j208-gpu-dt.c` | runtime changeset: mailbox and GPU nodes, GFX under DWI, DWI held on |
| `j208-ram/gpu/pvr-t8011/README.md` | every device run, with the power-off and DWI trace |
| `j208-ram/gpu/mesa-t8011/000[1-6]-*.patch` | Mesa patches against OrchardLinux/mesa `73a49abe` |
