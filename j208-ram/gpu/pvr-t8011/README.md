# pvr_apple on T8011 (J208): out-of-tree port (2026-11-08)

pvr_apple, the A10 (T8010) driver in
`hardware-j208-charging/kernel/patches/drivers/gpu/0001-…T8010-GPU.patch`,
is ported here to J208's A10X (T8011). The modules load into the existing
image. The kernel, the DTB and the image stay unchanged.

Status: **built, W=1 clean, not yet run.** No device has been used.

## Modules

| Module | What it is |
| --- | --- |
| `j208-rtkit.ko` | A private copy of RTKit and the Apple mailbox, renamed `j208_rtkit_*` / `j208_mbox_*`. It carries T8010 patches 0007 (A7IOP mailbox) and 0013 (`mbox_stop`). It binds only `apple,t8011-a7iop-mailbox`. The image's RTKit is built in and has neither patch. |
| `j208-gpu-dt.ko` | Adds the mailbox and GPU nodes to the live DT, links the power domains, and puts both devices in the GFX domain. It accesses no register. |
| `j208-powervr.ko` | The image kernel's imagination driver, plus the T8010 patch and `0002` (T8011). It depends on `gpu-sched`, `drm_exec` and `drm_gpuvm` from the build volume. |

Load order: `j208-gpu-dt`, then `j208-rtkit`, then `j208-powervr
exp_hw_support=1`. Unload in reverse order.

## Build

```sh
./build.sh      # assemble.sh in the toolchain container, then make W=1
```

`assemble.sh` copies the image kernel's sources from the build volume. It
applies the three T8010 patches, each pinned by SHA-256. It then renames
RTKit and the mailbox, applies `0001` and `0002`, and writes the Kbuild
files. Output goes to `build/tree/`, which is ignored. The build is
reproducible: two runs give the same module hashes.

```
4eeeb754e15d899026785ff4c32dbb020010f923832e61c9f96a6ee9ce4b30a6  j208-rtkit.ko
215a35553edc9ef9e2e974dbc7587610cad6cbd1576de3d7cc59097e4829b8d6  j208-gpu-dt.ko
2e33317d9bd975138241c47af0ee1b0d51d26ca385f4cefb2afe5b5eca72abad  j208-powervr.ko
```

## T8011 changes (`0002`)

- **`struct pvr_apple_soc`** holds the per-SoC values, selected by
  compatible:

  | | T8010 | T8011 |
  | --- | --- | --- |
  | Firmware | `apple/gfx-t8010.bin` | `apple/gfx-t8011.bin` |
  | `fw_runtime` | `0x8f000` | `0x90000` ([fw-compare](../fw-compare/README.md): bss ends at `0x8ff04`) |
  | Fastest state, no OPP table | 6 | 4 (stages I and J) |
  | Clusters | 6 | 12 (`CORE_ID 0x011b0c02`) |
  | Bridges | no | yes |

- **BVNC 13.7.12.2** is listed as experimental, as T8010's is, so it needs
  `exp_hw_support=1`. For Apple, the BVNC is decoded from `CORE_REVISION`
  (+0x20) and `CORE_ID` (+0x18) only. These are the RGX registers stages H
  to J read. The 64-bit `PBVNC` read would also touch +0x24, so it is not
  made.
- **Bridges 7 and 9** (`bridge0`, `bridge1`) get the writes stages B2 to J
  made. Each bridge must then read back `0xc0010005` at +0xc; any other value
  gives `-EIO`. They are written whenever the mailbox starts after the GFX
  domain was off:
  - **At probe:** the mailbox device holds the domain on while the bridges
    are written and RTKit starts the mailbox. From then on, the mailbox
    keeps the domain on.
  - **At a boot after a runtime power-off** (`rtk_idle`): before the mailbox
    restarts.

  This keeps stage D's order: bridges, then mailbox, window, run bit, RTKit.
- **`pvr_power_domains_init`** accepts a device with no `power-domains`
  property if it is already in a domain. `pvr_apple` and the private
  mailbox refuse to probe without a domain.

## Power domains

The image DT has no phandle on `ps_gfx` or `ps_dwi`, so no new node can
name them in `power-domains`. A changeset can add a `phandle` property, but
that does not register an existing node's phandle.

`j208-gpu-dt` takes three steps:

1. `of_genpd_add_subdomain(dwi, gfx)`. genpd then powers DWI before GFX and
   powers it off after GFX. Stage G showed GFX needs DWI on.
2. A changeset adds the mailbox and GPU nodes under `/soc`. The platform
   devices appear at once, with no driver bound.
3. `of_genpd_add_device(gfx, …)` adds both devices to the domain.
   `platform_probe` then finds `dev->pm_domain` already set and leaves it
   alone. The domain is not powered at this step.

No DT device has ever attached to the GFX domain, so genpd's `detach` is
unset. An unbind therefore does not drop the domain.

## DT values

- **Mailbox:** `0x201d24000`+`0x1000`, interrupts 185 to 188.
  - J208's ADT `gfx-kf` lists `0xba 0xb9 0xbc 0xbb`.
  - T8010's ADT (`DeviceTree.j71bap` in the cached shared IPSW) lists
    `0xbe 0xbd 0xc0 0xbf`, which `t8010.dtsi` names send-not-empty,
    send-empty, recv-not-empty, recv-empty.
  - The two orders match, so J208's lines are send-empty 185,
    send-not-empty 186, recv-empty 187, recv-not-empty 188.
- **GPU:**
  - `rgx` `0x201000000`+`0x100000`
  - `fender` `0x201d00000`+`0x5000`
  - `iop` `0x201d20000`+`0x4000`
  - `bridge0` `0x201f00000`+`0x9000`
  - `bridge1` `0x201f80000`+`0x9000`
- **No OPP table yet.** The exact frequencies of J208's `perf-states` are not
  recorded. Without a table, the GPU starts in state 4, the state stage J
  held, and devfreq stays off.

## Still open

These are for the first run:

- **P+`0xa5c`.** iOS sets it; the driver leaves it at 0. T8011 reads it
  15 times, against 8 on T8010. It is the first suspect if power
  transitions fail.
- **Unit masks.** One scheduling unit per engine may cover only part of the
  12 clusters. Only a job will tell.
- **Firmware delivery.** `request_firmware("apple/gfx-t8011.bin")` needs the
  carved image (`0x5c320` bytes, `f9c8b823…`) on the firmware search path,
  e.g. `firmware_class.path` set to a `/run` directory.
- **Runtime power-off.** Once probe has run, autosuspend shuts down the
  firmware and powers GFX and DWI off. The next use boots them again. That
  cycle is new on T8011.

## Running it

[`run-pvr-t8011.py`](run-pvr-t8011.py) does the first run. It needs a
fresh audio-image boot and a clean stage A on the same boot, and refuses a
session in which any stage from B to J has run. Each recorded session gets
one attempt.

The runner:
1. Uploads the pinned modules from `build/` (`build.sh` also copies
   `gpu-sched`, `drm_exec` and `drm_gpuvm` there) and the carved firmware.
2. Sets `firmware_class.path`.
3. Loads the modules in order, with dynamic debug on.
4. Waits up to 10 s for runtime autosuspend.
5. Unloads the modules and checks that GFX and DWI are off. It reads
   `pm_genpd_summary` to check, mounting debugfs if needed.

It submits no jobs. Kernel lines are streamed to `pvr-t8011-stream.log`.

```sh
cd /Users/leon/codeing/orchardlinux
python3 j208-ramboot/desktop-session.py --usable --audio-identify --minutes 180
python3 j208-ramboot/driver-work/gpu/gfx-power/run-gfx-power.py --run j208-ramboot/runs/<new> --confirmation power-gfx-once
python3 j208-ramboot/driver-work/gpu/pvr-t8011/run-pvr-t8011.py --run j208-ramboot/runs/<new> --confirmation probe-pvr-t8011-once
```

| Outcome | Meaning |
| --- | --- |
| `probed-suspended-unloaded` | Probe bound, GFX and DWI powered off by autosuspend, everything unloaded with both domains off |
| `probed-no-autosuspend-unloaded` | Probe bound, but the GPU did not reach runtime suspend within 10 s |
| `probe-failed-unloaded` | Probe failed cleanly; the stream log says where |
| `stopped` | A step failed; the result file names it |
| `uncertain` | A command timed out (likely a hang); no cleanup |

## Run 1 (2026-11-08 23:3x, `runs/20261108-232107`): `stopped`, no GPU access

Fresh audio-image boot, clean stage A, one attempt. Every insmod and rmmod
exited 0, no kernel fault appeared, and the desktop stayed up. The probe
failed with `powervr 201000000.gpu: error -ENODEV: RTKit init failed`.
Afterwards GFX and DWI were left **on**, with no consumer. There were two
causes, both in `j208-gpu-dt`:

1. **The mailbox phandle was 0.** `of_changeset_create_node()` queues the
   attach before the properties. `__of_attach_node()` sets `np->phandle`
   only from a `phandle` property already present, so the kernel never saw
   the mailbox's phandle. `of_parse_phandle(gpu, "mboxes")` therefore
   failed in `pvr_apple_rtkit_init()` before anything was powered or
   accessed: no bridges, no mailbox registers, no firmware.
   - Fix: set `np->phandle` once the node is in the tree, and check it with
     `of_find_node_by_phandle()`.
2. **genpd never powered the domain off.** Unbinding the mailbox resumes it,
   which powers GFX and DWI on. When the mailbox suspends again, the unbound
   GPU device, whose runtime PM is disabled, counts as busy, so genpd keeps
   the domain on.
   - Fix: teardown takes the mailbox out of the domain, then resumes and
     suspends the GPU device once. genpd then powers GFX off, then DWI.

`j208-gpu-dt.ko` is now `8df61751…`. The other modules are unchanged.

## Run 2 (2026-11-08 23:5x, `runs/20261108-234735`): **firmware initialised**, clean

This was a fresh audio-image boot with a clean stage A, using the fixed
`j208-gpu-dt` (`8df61751…`).

**Probe (t = 154.10 s to 154.65 s) worked end to end:**
- `j208-gpu-dt` added the nodes (mailbox phandle `0x3b`).
- RTKit protocol 12. The endpoint maps are `0x3`@0 and `0x3`@1, so 0x20
  and 0x21 are found.
- The firmware asked for a `0x2000` buffer.
- IOP and AP went `0x0 → 0x20`.
- The BVNC passed with `exp_hw_support`.
- `GFX firmware initialized (window 0x802300000, heap 0x802400000)` was
  logged, then `[drm] Initialized powervr 1.0.0 … on minor 1`, and
  `renderD128` appeared.
- So the bridges, mailbox (IRQs 185 to 188), window, state 4, RGX ID read
  and init data all worked on T8011.

**Runtime autosuspend (t = 155.76 s, about 1.1 s later):** AP went
`0x20 → 0x10` and IOP `0x20 → 0x1`. The GPU and mailbox were `suspended`,
and GFX and DWI were `off-0`.

**The runner stopped here** because of its own bug.
`pm_genpd_summary` lists a parent's subdomains as one-field rows, and
`dict()` rejected those rows. The runner therefore never reached its unload
steps, so I finished the planned unload by hand over SSH:
1. `rmmod j208_powervr` exited 0. The unbind's runtime resume went through
   the **resume path**: power on, bridges, mailbox restart, RTKit boot,
   `GFX firmware initialized` again (t = 199.72 s), then shutdown. GFX and
   DWI ended `off-0`.
2. `rmmod j208_rtkit` exited 0. GFX and DWI turned `on`, as expected: the
   mailbox unbind resumes the mailbox.
3. `rmmod j208_gpu_dt` exited 0. Its power-off pass logged
   `resume=0 suspended=1`, and GFX and DWI ended `off-0`.
4. The dependency modules unloaded, the nodes and devices are gone, and
   `firmware_class.path` is empty again.

The run left no kernel fault signature, and the desktop stayed up. The
runner has since been fixed: it now skips the child rows, and it restores
the firmware path with `echo`.

This is the first time the GFX firmware has been initialised on T8011 by a
real driver. Not run yet: a job; a resume driven by userspace; devfreq;
P+`0xa5c`; the unit masks.

## Compute job test (`--compute-job`)

[`pvr-compute-job.py`](pvr-compute-job.py) runs on the device and needs
only python3 and the powervr uAPI. It:
1. Reads the BVNC and the heaps.
2. Creates a VM and one BO at the general heap base, used as the CDM
   context-state buffer.
3. Creates a compute context with no static state, which Apple allows.
4. Submits compute jobs whose control stream is pvr_apple's terminate-only
   stream at `PVR_APPLE_GPU_DATA` (`0x200000`, mapped into every VM). It
   submits one job, then two back to back (each phase waited for at most
   10 s), then a burst of 300 back to back (at most 30 s). The burst goes
   past the 16 job slots, the firmware's 128 scheduling entries and the 256
   values of the stamp's low byte.

A syncobj wait also succeeds for a fence that ended with an error, so the
runner reads the kernel log as well: a `GFX firmware crashed` or `Apple GPU
job timed out` line makes `job_ok` false.

This exercises job dispatch, the compute ring, the doorbell, the completion
stamp and the fences, with no shader.

In this mode the runner lets the GPU autosuspend after probe. The job
therefore also tests a resume driven by userspace. After the job the GPU
must autosuspend again, and only then are the modules unloaded. The mode
has its own confirmation and marker, and refuses a session that already has
any pvr-t8011 attempt. After a non-hang error, the runner now unloads its
modules itself. After a firmware crash it does not: it records the state
once and stops with the modules loaded (see compute run 1 below), and the
device needs a reboot.

```sh
python3 j208-ramboot/driver-work/gpu/pvr-t8011/run-pvr-t8011.py --run j208-ramboot/runs/<new> --compute-job --confirmation compute-pvr-t8011-once
```

| Outcome | Meaning |
| --- | --- |
| `compute-jobs-done-unloaded` | All 303 jobs signalled with no crash or timeout in the log, the GPU autosuspended after them, everything unloaded with GFX and DWI off |
| `firmware-crashed-left-loaded` | The firmware crashed; the GPU is kept powered and the modules stay loaded until a reboot |
| `compute-job-failed-unloaded` | A job failed or timed out; `job_events` and the stream log say where |
| `compute-jobs-done-no-autosuspend-unloaded` | The jobs finished, but the GPU did not suspend within 10 s |

## Compute run 1 (`runs/20261109-000414`, 2026-11-09)

Outcome `uncertain`: the SoC hung, and the user rebooted it by hand.
Modules as pinned then (`j208-powervr.ko` `bd53102d…`).

- BVNC read back as `13.7.12.2`, 3 phantoms. The heaps were as expected.
- **Job 1 succeeded in 2.64 ms.** That included a resume from autosuspend
  driven by userspace (firmware booted again, init data accepted). The
  firmware sent ep `0x20` message `0x42…` on completion.
- **Jobs 2 and 3 (back to back) crashed the firmware:** `DAG: Non
  Sequential Stamp Updates seen entryIdx 0x0 roots.dag 0x1 stampIdx 0x0
  stampValue 0x3 channel 0x12b000 channelRingCommandIndex 0x2`. After 5 s
  the job timed out (`stamps compute 1; submitted compute 3`), the GPU was
  reset, and the firmware booted and initialised again.
- **About 1.2 s later the runtime autosuspend ran** (AP `0x20 → 0x10`, IOP
  `0x20 → 0x1`), and the SoC stopped answering while the runner was polling
  for that suspend. The `rmmod` in the result is the cleanup attempt that
  came after the SSH connection had already died.

### The DAG assert

Both firmwares have it: the function is at T8010 `0xe6a0` and T8011
`0xee28` (172 instructions each, same code). For a compute command (type
3), the dispatcher (T8011 `0xe63c`) passes it the word at command `+0x274`
and the byte at `+0x278`:

- **Bits 8-14 of `+0x274`** are the entry index into the channel's 128
  scheduling entries. These are the 44-byte records at queue `+0x0c`
  (`HEAP2`, "128 x 44 bytes" in the layout).
- **The byte at `+0x278`** selects a per-slot record at `0x5cab0 + 40 *
  slot`. Its byte `+8` holds the next expected entry. On a match it is
  incremented mod 128; on a mismatch the firmware asserts.
- **A helper (T8011 `0xed84`) reloads that counter from the arriving
  command's entry** when the slot's current channel differs. This matches
  the run: job 2 arrived after job 1 had retired and passed, and job 3
  (`stampValue 3`, ring index 2) found the counter at 1 with entry 0.

The driver wrote `seq` there, so every job below 256 had entry 0. Any two
jobs in flight on one channel would therefore crash the firmware, and T8010
has the same check. Geometry and fragment (`+0x558`, `+0x8a4`, `seq &
0xff`) avoid it only because the driver never has two of them in flight.

The firmware copies the 0x18-byte descriptor at `+0x26c` into its
completion record, so `+0x274` is also where the completion stamp at
`HEAP_STAMP + 4` comes from. That stamp read 1 after job 1, while `+0x284`
was 0.

**Fix (`0002`):**
- `+0x274 = (seq & 0x7f) << 8 | (seq & 0xff)`, so consecutive jobs take
  consecutive entries.
- The driver now takes the completed job from the stamp's low byte only,
  counting back from `last_seq`. At most 16 jobs are in flight, so this
  holds whether the firmware writes the token or a count of its own.

### The hang after the reset

The same power-off sequence (firmware stop, RTKit shutdown, mailbox stop,
GFX and then DWI off) worked in run 2, and before job 1 in this run. Only
the crash and reset came before it this time. The cause is not known
offline. One possibility: the crashed firmware never stopped the CDM job it
had started, and powering off with that state hangs, much as stage F did.

**Mitigation (`0002`):** from the first crash or reset, the driver holds a
runtime PM reference, so the GPU stays powered until unbind. Resets still
restart the firmware. Unloading would still power it off through
`j208-gpu-dt`'s teardown, so after a crash the runner leaves the modules
loaded, and the device is rebooted.


## Compute run 2 (`runs/20261109-083519`, 2026-11-09)

Run on a fresh boot after a clean stage A, with the DAG fix (`j208-powervr.ko`
`f0ce8977…`). The outcome was `uncertain`: the SoC hung again.

- Probe, firmware init and autosuspend all worked, as in run 2.
- **The DAG fix holds.** Job 1 took 2.13 ms, including a resume driven by
  userspace. Jobs 2 and 3 went out back to back. The firmware sent three
  ep `0x20` `0x42…` messages, one per job, and logged no crash, assert or
  timeout.
- **There was a test script bug.** The job 2-3 wait listed all 302
  remaining syncobjs, so it included the burst's, which had no fence yet.
  `DRM_IOCTL_SYNCOBJ_WAIT` returned `EINVAL` at once. The script then stopped
  with `ok: false`, and the burst never ran. Fixed: the wait now lists
  `syncs[1:3]` only.
- **About 1.2 s after the jobs, the runtime autosuspend ran** (AP `0x20 →
  0x10`, IOP `0x20 → 0x1`). The SoC then stopped answering while the runner
  was polling for that suspend, as in compute run 1.

**This narrows down the hang.** No crash and no reset happened this time.
The same power-off (firmware stop through Fender state 0, RTKit shutdown,
mailbox stop, GFX and then DWI off) works after probe, where no job has
run. It hung both times it followed jobs. So it is the power-off after the
GPU has run work that hangs, not the crash. The crash/reset power hold in
`0002` does not cover this case. Not run yet: the burst; a power-off after
jobs that does not hang.

## Burst test with the GPU held on (`--keep-powered`)

Both compute runs hung on the runtime power-off after GPU work. This mode
validates the jobs without that power-off:
1. After the GPU autosuspends following probe (the power-off that is known
   to be safe), the runner writes `on` to the GPU's `power/control`. The GPU
   resumes and the firmware boots again.
2. It then runs `pvr-compute-job.py`: one job, two back to back, then the
   burst of 300.
3. It records the state once and stops. Nothing is unloaded or powered off,
   so the device needs a reboot afterwards.

The mode has its own confirmation (`burst-pvr-t8011-once`) and its own
files: `pvr-t8011-burst-attempted.json`, `-result.json` and `-stream.log`.

```sh
python3 j208-ramboot/driver-work/gpu/pvr-t8011/run-pvr-t8011.py --run j208-ramboot/runs/<new> --keep-powered --confirmation burst-pvr-t8011-once
```

| Outcome | Meaning |
| --- | --- |
| `compute-jobs-done-left-powered` | All 303 jobs signalled, and the log shows no crash or timeout |
| `compute-job-failed-left-powered` | A phase failed or timed out; see `job_events` and the stream log |
| `stopped-left-powered` | The run stopped on an error after the GPU was held on |

## Burst run 1 (`runs/20261109-090127`, 2026-11-09)

Run on a fresh boot after a clean stage A. The outcome was
**`compute-jobs-done-left-powered`**.

- Probe, firmware init and the autosuspend after probe all worked.
  `power/control=on` resumed the GPU, and the firmware initialised again.
- **Every phase passed:**
  - job 1 in 0.40 ms;
  - jobs 2 and 3 back to back in 0.27 ms;
  - **300 jobs back to back in 27.1 ms**, about 90 µs per job.

  That makes 303 jobs. They went past the 16 job slots, the 128 scheduling
  entries and the stamp's low-byte wrap at 256.
- The kernel log shows no crash, assert or timeout. The firmware sent 79 ep
  `0x20` `0x42…` messages for the 303 jobs, so it batches them; completion
  is taken from the stamps.
- Afterwards the GPU was `active`, GFX and DWI were `on`, the device
  answered, there was no new fault signature, and the desktop stayed up.
  Everything is left loaded until a reboot.

Compute dispatch on T8011 works with the DAG fix. What remains open is the
runtime power-off after GPU work, which hung the SoC in both compute runs.

## The power-off after GPU work: offline notes and the trace run

**What the runs show.** The runtime power-off runs these steps:
1. `power_off`, after the wait for idle.
2. `fw_stop`: Fender state 0, RTKit shutdown (AP `0x10`, IOP `0x1`), then
   `FENDER_RUN` cleared.
3. The mailbox stops.
4. GFX goes off, then DWI.

After probe this always worked. After jobs it hung twice, both times after
the IOP `0x1` message. The Fender state 0 request comes before that message
and the SoC was still alive then, so the hang follows the RTKit shutdown.
That leaves `FENDER_RUN`, the mailbox stop, GFX off and DWI off. A delayed
death caused by an earlier step, as in stage F, is not ruled out.

**What differs from T8010.** The T8010 driver runs this exact sequence after
work without trouble (Hyprland, power domain off while idle). The obvious
hardware difference is T8011's bridges 7 and 9, which sit between the GPU
and the fabric. Native PMGR has a "before-off" bridge walk:
- it reads the counters at `+0x8110` and `+0x8108`;
- it writes the control pair `+0x8104`/`+0x8100` again.

Linux does neither ([BRIDGE-SEQUENCE](../BRIDGE-SEQUENCE-20261029.md)). A GPU
that has only probed has made no memory traffic through the bridges; a GPU
that has run jobs has. This is a lead, not a finding.

**The Linux code checked.** Nothing that only jobs set up touches MMIO late:
- `pvr_apple_mmu_flush`, `pvr_apple_switch_vm` and `pvr_apple_vm_fini` write
  shared memory only;
- the IMG power path (`pvr_power_fw_disable`) does not run for Apple;
- there is no devfreq without an OPP table.

**Trace run (`--trace-off`).** `j208-powervr` gains the debug parameter
`apple_off_trace_ms`, which is 0 (off) by default. When set, every
power-off step logs `off trace: <step> (fender <state>)` and pauses for
that many ms. The steps are idle, state 0, RTKit asleep, coprocessor
stopped and mailbox stopped. A notifier on the GPU device's genpd logs the
GFX domain's pre-off, off, pre-on and on events, and pauses after pre-off
and off; DWI goes off after the off event. The Fender reads touch only the
register the driver already polls.

The runner's `--trace-off` mode is the `--compute-job` run with
`apple_off_trace_ms=1000` and a 30 s suspend wait. It has its own
confirmation (`trace-off-pvr-t8011-once`) and its own files
(`pvr-t8011-traceoff-*`). The probe-time power-off gives a traced baseline.
The power-off after the jobs is expected to hang, and the last `off trace`
line in the stream log names the step. If it does not hang, the pauses
changed the timing, and that is a result too.

```sh
python3 j208-ramboot/driver-work/gpu/pvr-t8011/run-pvr-t8011.py --run j208-ramboot/runs/<new> --trace-off --confirmation trace-off-pvr-t8011-once
```

## Trace run 1 (`runs/20261109-092146`, 2026-11-09)

Run with `--trace-off`, `apple_off_trace_ms=1000`, all steps paused
(`j208-powervr` `9bfafa4f…`). The outcome was `uncertain`.

- **All 303 jobs passed again:** 2.3 ms, 0.26 ms and 27.3 ms.
- **The power-off after the jobs did not hang this time.** Every step was
  logged, each followed by a 1 s pause:
  1. idle (Fender `0x4`)
  2. state 0
  3. RTKit asleep
  4. coprocessor stopped
  5. mailbox stopped
  6. GFX domain pre-off, then off

  The runner then read GFX and DWI as `off-0` and the GPU as `suspended`.
  The unload's resume booted the firmware again. Without the pauses, the
  same power-off hung both times.
- **A bug in the trace itself.** The GFX genpd is IRQ-safe, so its notifier
  runs under a spinlock, and its `msleep` gave `BUG: scheduling while
  atomic` and `workqueue leaked atomic (preempt=0xffffffff)` at every
  power-off. The unload's power-off then hung after `GFX domain off`. With
  the kernel state already damaged, that hang is not evidence.

**Result:** pausing between the power-off steps avoids the hang, so some
step needs time before the next one. The notifier now only logs.
`apple_off_trace_mask` selects which steps pause: bit 0 idle, bit 1 state
0, bit 2 RTKit asleep, bit 3 coprocessor stopped, bit 4 mailbox stopped
(just before GFX goes off). The runner passes it as `--trace-mask`.
`j208-powervr` is now `2e33317d…`.

## Trace run 2 (`runs/20261109-093301`, 2026-11-09, `--trace-mask 0x10`)

This run paused only after "mailbox stopped", for 1 s just before the GFX
domain goes off. `j208-powervr` was `2e33317d…`. The outcome was
`uncertain`.

- All 303 jobs passed (2.4 ms, 0.27 ms, 27.4 ms). No kernel BUG this time.
- **The probe-time power-off worked.** It went: steps 0-4 within about
  0.5 ms, the pause, GFX `pre-off`/`off`, then a resume for the jobs.
- **The power-off after the jobs hung.** Steps 0-4 again took about 1 ms.
  After the 1 s pause came GFX `pre-off` and `off`, then nothing: the hang
  followed GFX off, which is where DWI goes off.

So a pause just before GFX off does not help. The pauses that kept the
power-off alive in trace run 1 sit earlier: after idle, after state 0,
after RTKit asleep, or after the coprocessor stopped. Without them, state
0, the RTKit quiesce/sleep and the coprocessor stop follow each other
within about 0.5 ms. After jobs, one of those steps needs time before the
next. The next run pauses only after state 0 (`--trace-mask 0x02`).

## Trace run 3 (`runs/20261109-103331`, 2026-11-09, `--trace-mask 0x02`)

This run paused only after state 0. The outcome was `uncertain`, and no
jobs ran.

- **The probe-time power-off went wrong.** It ran: state 0 and the 1 s
  pause, then RTKit asleep, the coprocessor stopped and the mailbox stopped
  within 0.1 ms. The GPU device went `suspended`, but the GFX domain never
  went off. There was no `pre-off`/`off` event, and GFX and DWI were still
  `on` 30 s later. The runner therefore skipped the jobs and unloaded.
- **The unload hung without any jobs having run.** `rmmod j208_powervr`
  resumed the GPU, booted the firmware and powered off the same way, and
  again GFX stayed on. `rmmod j208_rtkit` went through. `rmmod j208_gpu_dt`
  logged `power-off pass resume=0 suspended=1` and `removed`, and then the
  SoC hung: its teardown forces GFX and DWI off.

**The four data points so far:**

| Pauses | After probe | After 303 jobs |
| --- | --- | --- |
| none | off, fine | hang (twice) |
| all (`0x1f`) | off, fine | off, fine |
| before GFX off only (`0x10`) | off, fine | hang after GFX off |
| after state 0 only (`0x02`) | **GFX stays on**; the forced off at teardown hung | not reached |

**A reading that fits all four (not proven).** Clearing `FENDER_RUN` right
after the IOP acknowledges sleep stops the coprocessor before it has
finished going to sleep. After jobs it has more to finish, such as cache
writeback, than after a bare probe. A 1 s pause after state 0 with the
firmware still running seems to have the same effect. Turning GFX off
afterwards then hangs. Only the all-pauses run had a pause between "RTKit
asleep" and "coprocessor stopped", and it is the only one where the
power-off after jobs survived.

The next run pauses only there (`--trace-mask 0x04`). If that holds, the
fix is to wait for the coprocessor to settle after the sleep
acknowledgement before clearing `FENDER_RUN`: first with a delay, then, if
a known status bit is found, by polling it.

## Trace run 4 (`runs/20261109-111122`, 2026-11-09, `--trace-mask 0x04`)

This run paused only after "RTKit asleep". The outcome was `uncertain`, and
no jobs ran. **The probe-time power-off hung**, which had never happened
before: state 0, RTKit asleep, the 1 s pause, the coprocessor stopped, the
mailbox stopped, GFX `pre-off`, GFX `off`, then nothing. That refutes the
reading above.

| Pauses | After probe | After 303 jobs |
| --- | --- | --- |
| none | off, fine (many times) | hang (twice) |
| all (`0x1f`) | off, fine | off, fine |
| before GFX off only (`0x10`) | off, fine | hang after GFX off |
| after state 0 only (`0x02`) | GFX stays on; forced off at teardown hung | not reached |
| after RTKit asleep only (`0x04`) | **hang after GFX off** | not reached |

**What the hangs have in common.** Every hang that the trace places came
right after the GFX `off` event: GFX had reached state 0 (no
`Failed to reach power state`). The next step is genpd powering off the
parent, **DWI**. The trace runs 3 and 4 also hung there, or in the
teardown's forced off, with no jobs at all. Which pause comes before seems
to move the timing rather than the cause.

DWI is the AP-to-PMU link, and stages F/G established its relation to the
GPU ([GFX-PRIMARY-HANG](../GFX-PRIMARY-HANG-20261107.md)): with DWI off, a
GPU in any Fender state kills the SoC within 10-50 ms, time-based rather
than access-based. Upstream T8010 **never turns DWI off**, because its
`dwi_bl` backlight node holds it, and that is the SoC whose driver runs
this power-off fine. iBoot leaves DWI on, and only Linux's unused-domain
cut at 24 s turns it off. Our `j208-gpu-dt` made GFX a subdomain of DWI,
so genpd turns DWI off right after every GFX off. If the PMU link is still
in use then (for example, the GPU rail stepping down after GFX off), the
SoC hangs, and when it does depends on timing. That is consistent with the
scattered results above, and with T8010 never seeing it.

**Change: DWI held on.**
- `j208-gpu-dt` now registers a holder device in the DWI domain and resumes
  it at load (`dwi_hold()`), so genpd never turns DWI off after GFX. GFX
  stays a subdomain, so DWI is still on before GFX.
- At unload, GFX goes off as before, but **DWI stays on until reboot**. The
  holder is left registered and refers to no module code. `release_dwi=1`
  powers DWI off at unload instead.
- The runner now counts the GPU as suspended or unloaded when GFX is off
  **and DWI is on**.
- The next run is `--trace-off --trace-mask 0`, with no pauses and only the
  logs: the exact sequence that hung after jobs, now with DWI held.

## Trace run 5 (`runs/20261109-123009`, 2026-11-09, DWI held, `--trace-mask 0`)

This run used the new `j208-gpu-dt` (`215a3555…`, DWI held on) and
`j208-powervr` `2e33317d…`. Nothing paused; the steps were only logged.
**The outcome was `compute-jobs-done-unloaded`, `completed: true`.** This
is the first full run with no hang:

1. **Probe**, then the runtime power-off. Steps 0-4 and GFX `pre-off`/`off`
   took about 1 ms. GFX went `off-0` and DWI stayed `on`.
2. **Jobs.** Job 1 took 2.6 ms, with a resume driven by userspace. Jobs 2
   and 3 back to back took 0.18 ms. **300 back to back took 24.9 ms.** There
   was no crash or timeout.
3. **The runtime power-off after the jobs** is the exact sequence that hung
   in compute runs 1 and 2, with no pauses. **It completed:** GFX `off-0`,
   DWI `on`, the GPU `suspended`.
4. **Unload.** `rmmod j208_powervr` resumed the GPU, booted the firmware and
   powered off again. `j208_rtkit` and `j208_gpu_dt` unloaded, and the
   latter logged `power-off pass resume=0 suspended=1` and `DWI stays on
   until reboot`. The nodes and devices are gone, GFX is `off-0`, the RAM
   files are removed, there is no new fault signature, and the desktop is
   up.

**Conclusion:** the hangs came from genpd powering off DWI, the AP-to-PMU
link, right after GFX. With DWI held on, the T8011 GPU now goes through
probe, compute jobs, runtime power-off after work, resume and unload.
DWI stays on until reboot, as on T8010 and as iBoot leaves it.

Still open:
- Should DWI ever be released, and how? `release_dwi=1` is untested and
  probably unsafe after GPU work.
- The PMGR auto-mode exit that iOS performs before a power-state change,
  which Linux's pmgr-pwrstate skips.
- Render jobs (geometry and fragment) on T8011, and Mesa.
- Dropping the debug-only pieces once they are no longer needed:
  `apple_off_trace_ms`, and the crash/reset power hold, which is now
  probably unnecessary.

## Cleanup after the DWI fix (2026-11-09)

The port is back to the DAG fix (`3b47c6b`). I reverted three commits:
- the off trace, both of its commits (`apple_off_trace_ms` and
  `apple_off_trace_mask`, plus the GFX genpd notifier);
- the crash/reset power hold. The hang it worked around was the DWI cut,
  and `pvr_apple_fw_stop()` already copes with a crashed firmware.

`j208-powervr` is now `843f3ae3…` (W=1 clean); `j208-gpu-dt` and
`j208-rtkit` are unchanged. The runner no longer has `--trace-off` or
`--trace-mask`, but it still refuses a session that holds a trace marker.
On a firmware crash, the runner still stops without unloading, because the
power-off after a crash has not been tried since DWI is held.

Not yet run on the device: the next `--compute-job` run checks this build.

## Render mode (`--render`, 2026-11-09)

Geometry and fragment jobs still take token `seq & 0xff`, which is entry 0
for every job below 256. That is safe: the driver keeps one job per engine
in flight. It waits for the engine's last job before it writes the next,
and a geometry job also waits for the last fragment job to retire. So a
channel is always idle when its next command arrives, the firmware reloads
the counter from that command's entry, and the DAG check cannot fire. The
same is why T8010 ran Hyprland for thousands of renders with entry 0.

`--render` (confirmation `render-pvr-t8011-once`) runs:
1. the compute jobs;
2. after autosuspend, Orchard's Mesa with the A10X device info and its
   `vk-tri` test, at 64x64, then 512x512, then 1668x2224, stopping at the
   first failure (`../mesa-t8011`);
3. autosuspend, then unload.

The Mesa driver, ICD and `vk-tri` are pinned by SHA-256 like the modules.
The 64x64 image is saved to the run directory.

This mode has not run on the device yet.

### Render run 1 (`runs/20261109-125850`): stopped by the host, no render

This run used `j208-powervr` `843f3ae3` (the cleanup build). It was its
first run on the device.

- **Compute passed.** Job 1 took 2.3 ms, jobs 2 and 3 took 0.21 ms, and
  the 300-job burst took 27.1 ms. The GPU autosuspended after the jobs.
  This is the first device check of the cleanup build.
- **No render ran.** I piped the runner's output into `head`. That pipe
  closed while the Mesa files were uploading, and the runner's progress
  output raised `BrokenPipeError`, so `vk-tri` never ran.
- **The cleanup after the error was clean.** All three modules unloaded,
  and `j208-gpu-dt` logged `power-off pass resume=0 suspended=1` and
  `DWI stays on`. GFX was `off-0`, DWI `on`, the RAM files were removed,
  and there was no new fault.
- **No retry on this boot:** the attempt marker is set. Next time the
  runner's output goes to a file.

### Render run 2 (`runs/20261109-134941`): geometry done, fragment hangs

**The outcome was `render-failed-unloaded`.** There was no firmware crash
and no new fault, and the unload was clean.

- **Compute passed:** 2.8 ms, 0.22 ms, and 27.3 ms for the 300-job burst.
  The GPU autosuspended afterwards.
- **`vk-tri` 64x64 failed.** Mesa took the A10X device info without
  complaint. Every pixel read back as `0x55555555`, the fill `vk-tri`
  writes before the render, so nothing reached the target.
- **The kernel log of the render** comes from `dmesg`, read on the live
  device afterwards. The follower's own log stops after the compute jobs;
  see below.

  ```
  281.163  GFX firmware initialized                (resume for vk-tri)
  286.176  [drm] *ERROR* Apple GPU job timed out
  286.176  hang: fragment job 1; stamps geometry 1 fragment 0 compute 0;
           submitted geometry 1 fragment 1 compute 0; fragment retired 0x1;
           rings ta 0x1 3d 0x1; events 0x1/0x1
  286.176  resetting the GPU          (firmware restarted at 286.180)
  287.375  power-off; 288.149 resume and power-off for the unbind
  288.500  j208-gpu-dt: power-off pass resume=0 suspended=1; DWI stays on
  ```

  - The **geometry job finished**: its stamp reads 1.
  - The firmware **took the fragment job**: ring 1's first index reads 1.
  - The fragment job **never ended**: there was no `PIXELBE_END_RENDER`,
    so no fragment-done event and no stamp. The only event was
    geometry's.
- **The reset path works on T8011.** This was its first use, and the first
  test since the crash/reset hold was dropped. It stopped the firmware,
  went to state 0, rebooted the firmware, then powered off, resumed and
  unbound normally.
- **The follower lost its SSH session.** Its log ends at 206.9 s, during
  the 13 MB Mesa upload. That session has ServerAlive 5 s × 2, so the
  likely cause is the upload saturating Wi-Fi, but that is not confirmed.
  **Runner fixes:**
  - The Mesa files now upload before the follower starts.
  - The crash and timeout check after each `vk-tri` case reads `dmesg`.
  - The result keeps the GPU lines of `dmesg` from the start of the run.

**Next: the fragment hang, offline.** These are the T8011 differences that
touch only the fragment path:
- **3 phantoms instead of 2** (12 clusters vs 6). The ISP and pixel back
  end work per phantom, and Mesa sizes tiles in flight per phantom.
- **The unit masks** (fw-compare "Not covered"): one hardware unit per
  engine. T8011's dispatcher combines more mask fields.
- **The fragment command's per-cluster blocks:** 6 entries of 0, then 6 of
  `0x1d3`.

### The fragment hang: T8011 reads per-cluster and per-pipe blocks (offline)

The register-block copies in both firmwares were mapped by following each
store's source word and register. The tool is a symbolic walk over
`fw-compare`'s Thumb-2 disassembly. Start3D's handler is T8010 `0xb7fc` and
T8011 `0xbea4`. Its T8010 mapping matches `pvr_apple_3d_job.regs64[29]`
entry for entry, including `[10] = 0xf28`.

| Block | T8010 | T8011 |
| --- | --- | --- |
| 3D 64-bit (`cmd+0x68`) | 29 entries: 0xfa8, 0xfb0, 0xfb8, **0xfc0 ×2 pipes** (select 0x8388), 0xf48..0xf68, 0xf28, 0x6a0..0x6b8, 0xf30, **0x11020 ×6, 0x11080 ×6** (select 0x11ff8), 0xfd0 | **42**: the same with **0xfc0 ×3**, **0x11020 ×12, 0x11080 ×12** |
| 3D 32-bit (`cmd+0x3b0`) | 17 used, 0xf18..0x11f8 | the same |
| TA 64-bit (`HEAP_TA_REGS64`) | 28: 22 registers, **0x11030 ×6** | **34**: **0x11030 ×12** |
| CDM arguments (`0x11040`) | 6 per cluster | 12 per cluster. The driver writes only `+0..+0x17` and the rest is zero, so the extra entries are zero as well |

So on T8011 the 6-entry blocks fed every register after the pixel pipes
from the wrong word. In the fragment job, 0xf48 got 0xf68's value, 0xf28
got `PDS_BGRND0`'s, the background program pointers were off by one, and
clusters 6-11 got the next fields. That fits the hang: the job was
dispatched and never ended. Geometry's 0x11030 for clusters 6-11 read past
its block into zeroed memory, which is harmless for a terminate-only job.
It finished anyway.

**Fix (port `9508d8a`, `0002`):** the writers keep T8010's blocks and
expand them for the SoC's cluster count (`pvr_apple_soc.num_clusters`):
- cluster c takes entry `c % 6`, since each cluster's value is the same;
- there is one 0xfc0 per 4 clusters.

T8010's output is unchanged. A host test of `frag_regs64()` gives the
identity for 6 clusters and the 42-entry order above for 12. The uAPI and
Mesa are unchanged. `j208-powervr` is `10e5d2a7…` (W=1 clean).

### Render run 3 (`runs/20261109-182257`): the first frames on T8011

This run used `j208-powervr` `10e5d2a7` (12-cluster and 3-pipe blocks) and
Mesa `4b3d4091`. **The outcome was `render-done-unloaded`,
`completed: true`.**

- **Compute:** 2.4 ms, 0.29 ms, and 27.4 ms for the 300-job burst.
- **`vk-tri` passed at every size:**

  | Case | Clear pixels / triangle | Corners (red, green, blue) |
  | --- | --- | --- |
  | 64x64 | 3584 / 512 | `ffd91214`, `ff0ae114`, `ff060eeb` |
  | 512x512 | 229376 / 32768 | `ffde1010`, `ff0fdf10`, `ff0b0ce8` |
  | 1668x2224 (full panel) | 3245928 / 463704 | `ffdf1010`, `ff10df10`, `ff0c0ce7` |

  The triangle covers 1/8 of the target, as it should. The 64x64 image
  (`pvr-t8011-tri-64.ppm`) shows a vertex-coloured triangle, red to green to
  blue, on the dark blue clear.
- **No timeouts, crashes or faults.** The GPU autosuspended after the jobs
  and after the renders. The firmware booted six times in this run: probe,
  compute, and one per render case plus the unbind. The unload logged
  `power-off pass resume=0 suspended=1` and `DWI stays on`. GFX was off and
  the RAM files were removed.
- **The follower kept its log for the whole run.** With Mesa uploaded first,
  it no longer drops.

**T8011 now runs compute and render jobs through the stock powervr uAPI and
Orchard's Mesa.** Next steps:
- the remaining `vk-tri` variants: texture, depth, discard, linear;
- `vk-compute-test`, which runs real compute shaders and so exercises
  0x11040 per cluster;
- Zink and a compositor.

## Suite mode (`--suite`)

`--suite` (confirmation `suite-pvr-t8011-once`) runs what `--render` does,
with `SUITE_CASES`, 40 cases:
- the three `RENDER_CASES`;
- 13 `vk-tri` variants: linear target; textures (twiddled, linear,
  filtered, 300x200); discard; `gl_FragCoord`; scissor; blending (over and
  dst); strip; negative-height viewport; `vkCmdClearAttachments`;
- `vk-compute-test`'s 12 shaders, then `basic` with 4096 groups. These are
  real compute shaders, so they exercise the per-cluster 0x11040;
- depth: pass, fail, load, and pass at full panel. These are the 0xf48 ..
  0xf68 registers that shifted on T8011;
- loads (LOAD 1, 2, 3), 100 draws, 100 instances, 3 pre-clears, and 100
  repeats with timings.

A failing case is recorded and the suite goes on. A firmware crash or a job
timeout stops it. When every case ran, the outcome is `suite-done-unloaded`;
`render_ok` says whether all of them passed.

### Suite run 1 (`runs/20261109-190412`)

**The outcome was `suite-done-unloaded`, `completed: true`.** All 40 cases
ran in 82 s. There were no timeouts, crashes or faults, the GPU suspended
and resumed around every case, and the unload was clean.

- **Render: 25 of 27 `vk-tri` cases passed.**
  - All three sizes, linear target, the four texture cases (including
    300x200 linear, a known A10 failure), discard, `gl_FragCoord`, scissor,
    both blends, strip, FLIPY and ClearAttachments.
  - Depth: pass, fail, and pass at full panel. These exercise
    0xf48..0xf68, the registers the 6-cluster blocks had shifted.
  - LOAD 1 and 2, 100 draws, 100 instances, 3 pre-clears.
  - 100 repeats: **0.43 ms submit to fence** (0.39 to 0.49).

  The runner marked four of these failed: linear, tex-300x200, flipy and
  instances-100. Each printed `PASS` and exited 0, but a Mesa warning on
  stderr came after the `PASS`. Fixed: a case now passes when it exits 0
  and prints a `PASS` line anywhere.
- **The two real failures are Mesa or known gaps, not T8011:**
  - `TRI_LOAD=3` (a load after a clear-only pass) also fails on the A10.
    The fork's MESA.md lists it as a known gap. Every pixel came back as
    the expected `0xff4080bf`, yet `vk-tri` reports FAIL. That is the same
    symptom.
  - `TRI_DEPTH=load`: Mesa logs `G9P: texture format VK_FORMAT_D32_SFLOAT
    not decoded yet` before submitting. The store wrote 96 of 4096 words,
    the A10's count. The load path is unfinished in Mesa's G9P backend.
- **The compute shaders did not run.** The runner made only `vk-tri`
  executable, so all 13 compute cases exited 126 ("Permission denied").
  Fixed: `vk-compute-test` is made executable too. They need another
  boot.

### Suite run 2 (`runs/20261109-191338`): 38 of 40, all compute shaders pass

**The outcome was `suite-done-unloaded`, `completed: true`.** The run took
86 s, with no timeouts, crashes or faults and a clean unload.

- **All 13 compute cases passed.** Each of `vk-compute-test`'s 12 shaders
  ran 4096 invocations with 0 wrong: basic, float, ifelse, loop, break,
  fcmp, twobreak, julia, juliapc, and the three palette variants. `basic`
  with 4096 groups ran 262144 invocations, 0 wrong. These are the first real
  compute shaders on T8011, and they run on all 12 clusters with 0x11040
  per cluster.
- **25 of 27 render cases passed**, the same cases as in suite run 1.
- **The only failures** are the two that are not T8011's: `TRI_DEPTH=load`
  (Mesa's D32 load is unfinished) and `TRI_LOAD=3` (a known A10 gap).

**State:** the T8011 GPU runs Vulkan compute and render through Orchard's
Mesa, with T8010 parity on this suite. Next: Zink and a compositor, as on
the A10 (`OrchardLinux/gpu` `tools/a10/mesa/deploy-zink.sh`).

## GL mode (`--gl`): OpenGL ES through Zink, off screen

The J208's desktop is Labwc 0.8.3 (wlroots 0.18) on the pixman renderer,
drawing into simpledrm (`card0`), with Omarchy A's shell (Quickshell) and
Debian trixie's Mesa 25.0.7. The A10 took this path to a GPU desktop
(`OrchardLinux/gpu` `tools/a10/arch`, MESA.md "The GPU desktop against
pixman"). There, wlroots' GLES2 renderer runs on the GPU's render node
through Zink on the fork's Vulkan driver (`WLR_RENDERER=gles2
WLR_RENDER_DRM_DEVICE=/dev/dri/renderD128 MESA_LOADER_DRIVER_OVERRIDE=zink`).
The frames go to simpledrm as dma-bufs. The shell stays on Qt's software
renderer, because Quickshell on the GPU hung the A10.

Before Labwc, GL has to work on its own. `--gl` (confirmation
`gl-pvr-t8011-once`) does the following:
1. Runs what `--render` runs, with `GL_CASES` in place of `RENDER_CASES`.
2. Unpacks the Zink build (`../mesa-t8011/build-zink.sh`, prefix
   `/run/j208-zink`).
3. Runs `vk-tri` at 64x64 as a check of the Vulkan path.
4. Runs `gl-probe`. It uses GBM on the render node, EGL, and a GLES 2
   context, then checks a clear, a triangle, source-over blending and a
   2x2 texture. It also checks the compositor's own path: a LINEAR XRGB8888
   GBM buffer, exported as a dma-buf and imported as an EGLImage render
   buffer, gets a clear and a triangle. Everything is read back with
   `glReadPixels`.

`gl-probe` never opens `card0`, so the running desktop is not touched. The
outcome is `gl-done-unloaded` when both cases pass and the GPU suspends
afterwards.

### GL run 1 (`runs/20261109-213456`): Zink stops on an assertion

The outcome was `render-failed-unloaded`. There were no timeouts, crashes
or faults, the GPU suspended, and the unload was clean.

- The compute jobs passed, and so did `vk-tri` at 64x64.
- `gl-probe` aborted while Zink set up its screen. Zink asserted in
  `zink_get_format`: `screen->have_D32_SFLOAT_S8_UINT` failed.
  - Zink maps D24S8 to D32S8 when a driver lacks D24S8, and asserts that
    D32S8 is there.
  - G9P offers neither format yet. The fork's MESA.md lists D24S8 as open
    on the A10 too.
  - The A10 desktop runs Zink from a distribution's Mesa, which is built
    without assertions. There Zink just reports the format unsupported.
    wlroots' GLES2 renderer uses no stencil.
- This is not T8011's: the A10X device info copies the A10's formats.
  `build-zink.sh` now builds with `-Db_ndebug=true`, and the archive pin
  is updated.

### GL run 2 (`runs/20261109-215459`): Zink runs; clears work, draws do not show

The outcome was `render-failed-unloaded`. There were no timeouts, crashes
or faults, the GPU suspended, and the unload was clean.

- **Zink came up on T8011.** It reported `zink Vulkan 1.2(PowerVR Apple
  A10X ...)` and `OpenGL ES 2.0 Mesa 27.0.0-devel`, on GBM backend `drm`
  and EGL 1.5.
- **Clears are right.** So is the compositor's import path: a LINEAR
  XRGB8888 GBM buffer (stride 1024, modifier 0) imported as an EGLImage
  render buffer.
- **No draw shows.** The triangle, blend, texture and dma-buf-triangle
  checks all read back the clear colour, with `glGetError` 0. The
  firmware completed about 11 jobs during `gl-probe`, so the draws were
  submitted and ran.
- `vk-tri` draws correctly on the same boot.

How Zink's renders differ from `vk-tri`'s:
- Zink reads the clear back first, so the triangle's render loads the
  target (load op LOAD).
- G9P runs such a render with Rogue's `process_empty_tiles` off
  (`ISP_CTL` `0x21000f`). Only tiles that a draw's region clip reaches are
  processed.
- `vk-tri` never draws into a loaded target that it did not draw into
  before. Its `TRI_LOAD=2` draws the same triangle twice, so a lost draw
  would not show.
- Not yet known: whether the draw is lost (tiles skipped, wrong state), or
  the readback copies stale memory.
- `TRI_LOAD=3`'s failure is `vk-tri`'s own check. It expects the triangle
  after a clear-only pass, and every pixel was the expected clear colour.

The next `--gl` runs the `GL_DIAG` cases after a failing `gl-probe` and
saves each output as `pvr-t8011-gl-diag-NAME.txt`:

| Diagnostic | What it does |
| --- | --- |
| `GLP_QUICK=1` | One triangle with the clear folded into its render (load op CLEAR). |
| `GLP_QUICK=2` | The clear read back first, then the triangle in a render that loads. |
| `GLP_QUICK=2` with `G9P_ALL_TILES=1` | As `QUICK=2`, with every tile processed. |
| `G9P_SYNC=1 G9P_TRACE=1 G9P_PROBE=1`, for both `QUICK` cases | The driver's trace of each render (size, load or clear, draws, tile range, pipelines) and eight target pixels read from GPU memory right after the render, bypassing Zink's readback. |
| `G9P_DEBUG=1 G9P_DUMP=1` for `QUICK=2` | The compiled shaders and the command streams. |
| `vk-tri TRI_LOAD=2` with `G9P_DEBUG=1 G9P_DUMP=1` | The same output for a loaded render with a draw that works, for comparison. |

### GL run 3 (`runs/20261109-221642`): the GPU draws nothing for Zink

The outcome was `render-failed-unloaded`. There were no timeouts, crashes
or faults, and the unload was clean. All seven `GL_DIAG` outputs were
saved.

- **It is not the readback or tile skipping.** `G9P_PROBE` reads the
  target from GPU memory right after each render. That memory held only the
  clear colour, both for a render that clears and draws (`GLP_QUICK=1`,
  load op CLEAR, all 8x8 tiles) and for one that loads and draws. With
  `G9P_ALL_TILES=1` there was still no triangle.
- **The render streams match `vk-tri`'s.** `vk-tri TRI_LOAD=2` draws
  correctly. Its geometry and fragment streams differ from Zink's only in
  addresses and in the viewport size.
- **The vertex shader matches too.** Zink's has the same fetch code as
  `vk-tri`'s for a vec2 at offsets 0 and 4. The vertex buffer holds the
  right positions with stride 8, and the outputs go to slots 0-3.
- **What differs is the fragment side and the varyings:**

  | | Zink's draw | `vk-tri`'s draw |
  | --- | --- | --- |
  | Varyings | none | `v_col` |
  | Fragment colour | a uniform buffer: 4 dwords DMA'd per draw into common-store dwords 16-19 (TEXUNICODEBASE program, SIZEINFO2 = 2 units) | the varying |
  | Vertex size | 4 | 8 |

- `vk-tri TRI_CLEARATT` also draws with no varyings and a fragment DMA, and
  it passes. Neither factor alone is certain to be the cause.

The next `--gl` runs every case of `GL_CASES`; a crash or timeout stops it:
- `vk-tri` with `shaders/` (built by `build-zink.sh`): the colour from a
  constant, push constants or a uniform buffer, each with and without
  varyings (`novary.vert`). `TRI_WANT` checks the colour.
- `gl-probe`'s one triangle with the colour from a constant, a varying or
  a uniform (`GLP_QUICK=1 GLP_FS=...`).
- The full `gl-probe`.
- If that fails, the dumps and the target probe of the constant-colour GL
  triangle, and the dumps of `vk-tri` without varyings with a uniform
  buffer.

### GL run 4 (`runs/20261109-223542`): no varyings, no pixels

The outcome was `gl-done-unloaded`. All 11 cases ran, with no timeouts,
crashes or faults and a clean unload. 4 passed.

| `vk-tri` fragment colour | Vertex shader with a varying (`tri.vert`; the fragment shader ignores it) | Vertex shader writing only the position (`novary.vert`) |
| --- | --- | --- |
| constant | PASS | nothing drawn |
| push constants | PASS | nothing drawn |
| uniform buffer | PASS | nothing drawn |

**On T8011, a draw whose vertex shader writes no varying produces no
pixels**, whatever the colour comes from.
- Zink's GL draws in `gl-probe` have no varyings, and all of them failed.
  That includes `GLP_FS=varying`: its varying is a constant, which the GL
  linker folds away.
- It is not known whether the A10 behaves the same. Every shader used on
  the A10 desktop writes texture coordinates.

**Fix (`../mesa-t8011/0002-pvr-g9p-vertex-shaders-without-varyings.patch`):**
- A vertex shader with no varying now writes varying 0 (four copies of an
  output register) ahead of the position, as `tri.vert` does. The draw then
  has the W iterate and one varying, the configuration that works.
- `build.sh` and `build-zink.sh` now apply every `0*.patch`, and clean the
  files a patch added before they apply them again.
- Both builds are rebuilt and re-pinned:
  - `libvulkan_powervr_mesa.so` is `09b48eb1`;
  - `j208-zink.tar.gz` is `f42dbbaa`.

### GL run 5 (`runs/20261112-174706`, 2026-11-12): patch 0002 is not the fix

The outcome was `gl-done-unloaded`. All 11 cases ran, with no timeouts,
crashes or faults and a clean unload. 4 passed: `tri-64` and `vk-{const,push,ubo}`.
Every position-only `vk-tri` case and every GL case still drew nothing.

The dumps show patch 0002 took effect: the position-only vertex shader now
writes varying 0, has 8 outputs and the varyings program, and its streams
match `vk-tri`'s working draw. The differences that remain are:
- its register count: 10 (and Zink's 10), against `vk-tri`'s 14;
- with it, the kick's temps (`0x18` against `0x20`) and the register field
  of `VDM_STATE` (`0x1481` against `0x1c81`).

Counter-evidence: `vkCmdClearAttachments`' hand-written vertex shader has 8
registers and no varying, and it draws (`tri-clearatt` passes in the suite).
So neither "no varying" nor "few registers" alone explains it. The loads
in the compiled shaders are followed by waits (`004d`), as in the
hand-written one.

**Next run (bisection):**
- `../mesa-t8011/0003-pvr-g9p-vertex-shader-register-overrides.patch` adds
  `G9P_VS_MIN_REGS`, `G9P_VS_KICK_REGS` and `G9P_VS_VDM_REGS`.
- `GL_CASES` runs the position-only shader with 12, 14 and 16 registers, and
  with 14 in only the kick or only `VDM_STATE`.
- `shaders/cola.vert` has no varying but reads `col` (its alpha as W), so it
  fetches the same attributes as `vk-tri`'s shader.
- The GL triangle and `gl-probe` run with 14 and 16 registers.
- `GL_DIAG` dumps the 16-register case and `cola.vert`.
- Re-pinned:
  - `libvulkan_powervr_mesa.so` is `97d444a6`;
  - `j208-zink.tar.gz` is `a4500a7f`;
  - `cola-vert.spv` is `b1255136`.

### GL run 6 (`runs/20261112-184806`, 2026-11-12): the cause is VDM_STATE's register allocation

The outcome was `gl-done-unloaded`. All 13 cases ran, with no timeouts,
crashes or faults and a clean unload. 10 passed.

| Case | Vertex shader registers as allocated | Result |
| --- | --- | --- |
| `vk-novary-const` | 10 (kick and `VDM_STATE`) | nothing drawn |
| `…-min12`, `…-min14`, `…-min16` | 12, 14, 16 in both | PASS |
| `…-kick14` | 14 in the kick, 10 in `VDM_STATE` | nothing drawn |
| `…-vdm14` | 10 in the kick, 14 in `VDM_STATE` | PASS |
| `vk-cola-const` | its own count, no override | PASS |
| `gl-quick1-const` | 10 | nothing drawn |
| `gl-quick1-const-min14`, `-min16` | 14, 16 | PASS |
| `gl-probe-min16` (every scene, dmabuf too) | 16 | PASS |

**The register field of `VDM_STATE` is the cause.** With 10 registers (5
units of 2) the draw is lost; 8 (`vkCmdClearAttachments`), 12, 14 and 16
work. The kick's temps make no difference. Patch 0002's varying was not
the cause, and it is unknown whether it is still needed; it stays.

**Fix (`../mesa-t8011/0004-pvr-g9p-vertex-shader-registers-in-groups-of-4.patch`):**
- `VDM_STATE` now allocates whole groups of 4 registers, as the kick's
  temps already do. `MAX_INSTANCES` follows from the same count.
- Re-pinned:
  - `libvulkan_powervr_mesa.so` is `62e9ab1a`;
  - `j208-zink.tar.gz` is `78689214`.
- `GL_CASES` now checks the fix with no overrides:
  - the position-only shaders with every colour source, `cola.vert`, every
    GL scene and `gl-probe`;
  - `tri-screen`, `tri-tex`, `tri-clearatt`, `tri-draws-100` and
    `tri-instances-100`, whose vertex tasks the change also touches.

### GL run 7 (`runs/20261112-201553`, 2026-11-12): patch 0004 fixes the draws

The outcome was `gl-done-unloaded`. All 14 cases passed with no
overrides. There were no timeouts, crashes or faults, the GPU suspended,
and the unload was clean.

- The position-only vertex shaders draw with each colour source: a
  constant, push constants and a uniform buffer. `cola.vert` draws too.
- The GL triangle draws with its colour from a constant, a varying or a
  uniform.
- `gl-probe` passes every scene: clear, triangle, blending, texture, and
  the dma-buf clear and triangle.
- The vertex tasks the change also touches still pass: `tri-screen`,
  `tri-tex`, `tri-clearatt`, `tri-draws-100` and `tri-instances-100`.

OpenGL ES through Zink now draws on the T8011. Next is the desktop on the
GPU (`--labwc`, below).

## Desktop mode (`--labwc`): Labwc on the GPU, then back on pixman

It needs a fresh boot and its own go-ahead, because it stops the running
desktop twice.

`--labwc` (confirmation `labwc-pvr-t8011-once`) does the following:
1. Runs what `--render` runs, with `LABWC_CASES`:
   - `tri-64` and `gl-probe`;
   - `gl-probe` with `GLP_SCANOUT=1668x2224`, twice: once with Mesa
     choosing the driver itself (`scanout-auto`), once with kmsro named
     (`scanout-kmsro`). This is the compositor's allocation: a scanout
     buffer from a GBM device on simpledrm, imported into the render
     node's EGL and drawn into.

   Labwc runs only when `tri-64`, `gl-probe` and a scanout case pass. It
   uses the loader setting of the first scanout case that passed, and
   otherwise the desktop is not touched.
2. Records Labwc's command line and environment from `/proc`. It checks
   that Labwc is the one `peripherals/init` started: on tty1, on pixman,
   with no render node open.
3. Reads back what simpledrm scans out with `kms-grab`, falling back to
   `grim`.
4. Stops Labwc. Its clients (the shell, foot, Firefox) close with it.
5. Starts Labwc again with the same command line on a copy of
   `/etc/labwc`, with `WLR_RENDERER=gles2`, the render node,
   `WLR_DRM_DEVICES` set to the simpledrm card, the Zink environment
   without `MESA_LOADER_DRIVER_OVERRIDE=zink` (desktop run 1), and
   `G9P_TRACE=1`. This is the A10's setup (`OrchardLinux/gpu`
   `tools/a10/arch/start-labwc.sh`).
   - The copy's autostart runs `/etc/labwc/autostart` with the Zink
     environment removed, so the clients draw in software as before.
   - The copy's `environment` file goes without Omarchy's
     `LIBGL_ALWAYS_SOFTWARE=1`. Labwc sets that file on itself before it
     creates the renderer, so the line would put its EGL on software. The
     autostart sets it again for the clients.
6. Waits for the autostart and a new foot, runs for `LABWC_SECONDS` (20),
   then reads back the screen again.
7. Stops the GPU Labwc and starts Labwc as it ran before, then Firefox
   if it was open. It reads back the screen a third time.

The restore runs whatever happened in steps 4-6. Only an SSH timeout
leaves the desktop down until a reboot.

The GPU desktop passes (`labwc_gpu_ok`) when all of these hold:
- Labwc and foot ran for the whole period;
- Labwc's log names Zink as the GL renderer;
- `G9P_TRACE` counted renders;
- there was no firmware crash or job timeout;
- the GPU readback has at least 16 colours.

The outcome is `labwc-gpu-done-unloaded` when it passes, the desktop is
back and the GPU suspends afterwards. The run keeps:
- `pvr-t8011-labwc-{pixman-before,gpu,pixman-after}.ppm`;
- `pvr-t8011-labwc-gpu.log.gz`, the GPU Labwc's whole log.

### Desktop run 1 (`runs/20261112-203019`, 2026-11-12): simpledrm's buffers are refused

The outcome was `labwc-gpu-failed-unloaded`. There were no timeouts,
crashes or faults, and the unload was clean. The desktop came back on
pixman, with Firefox.

- `tri-64` and `gl-probe` passed.
- The GPU Labwc created its GLES2 renderer on Zink (`GL renderer: zink
  Vulkan 1.2(PowerVR Apple A10X …)`), then enabled no output:
  1. wlroots creates its GBM allocator on the display device, simpledrm,
     not on the render node. With `MESA_LOADER_DRIVER_OVERRIDE=zink`,
     `gbm_create_device` failed there.
  2. wlroots fell back to simpledrm dumb buffers. Their 1668-pixel rows
     are 6672 bytes.
  3. The driver refused to import them
     (`VK_ERROR_INVALID_DRM_FORMAT_MODIFIER_PLANE_LAYOUT_EXT`). G9P render
     targets hold the row pitch in 64-byte units, 16 pixels at 4 bytes.
  4. The mode test failed, so foot never came up.
- This is the J208's display. The A10's is 1536 pixels wide, a multiple
  of 16.
- The restore worked as written. Labwc came back on pixman, and so did
  Firefox. The screen read back with 912 colours before and 783 after.

**Fix:**
- Labwc runs without the override. Mesa's loader maps the powervr render
  node to Zink by itself. On simpledrm it falls back to kmsro, which pairs
  the display with Zink and allocates scanout buffers as dumb buffers.
- `../mesa-t8011/0005-kmsro-zink-dumb-buffers-with-a-64-byte-aligned-pitch.patch`
  makes those dumb buffers wide enough for a 64-byte-aligned pitch. The
  resource and the framebuffer keep the display's width.
- `gl-probe`'s new `GLP_SCANOUT` checks this path before Labwc starts.
- Re-pinned:
  - `j208-zink.tar.gz` is `d95f18d7`;
  - `gl-probe` is `b5eb8088`.
  - The Vulkan driver is unchanged.

### Desktop run 2 (`runs/20261112-210419`, 2026-11-12): the desktop runs on the GPU

The outcome was `labwc-gpu-done-unloaded`. There were no timeouts,
crashes or faults, the GPU suspended, and the unload was clean.

- `tri-64`, `gl-probe`, `scanout-auto` and `scanout-kmsro` passed. With
  patch 0005, simpledrm's scanout buffers go through Zink. Labwc ran with
  Mesa choosing the driver itself, with no override.
- Labwc on wlroots' GLES2 renderer (`zink Vulkan 1.2(PowerVR Apple
  A10X …)`) created its 1668x2224 framebuffers and enabled the output. The
  autostart brought up Omarchy's shell and foot, and both ran for the whole
  20 seconds.
- `G9P_TRACE` counted 14 renders. An idle desktop renders only on damage.
- `kms-grab` read the scanout back: the wallpaper, the Quickshell bar, the
  foot window and the cursor, drawn whole and right. It has 77626 colours,
  against about 900 for the pixman captures, where Firefox covered the
  wallpaper.
- The restore brought Labwc back on pixman, with Firefox.
- wlroots logged "Failed to render cursor buffer" (debug level) a few
  times while the output came up, yet the cursor shows in the capture.

**State:** Omarchy's desktop runs on the T8011 GPU through Zink, as on the
A10, for a test period. The session itself still starts on pixman.

## Kept desktop mode (`--labwc-keep`): a benchmark, then the GPU desktop stays

`--labwc-keep` (confirmation `labwc-keep-pvr-t8011-once`, its own marker)
runs what `--labwc` runs, with these differences:
- **Benchmark:** on each renderer, a scrolling foot (`ls -lR /usr/share`,
  as the A10's `start-labwc.sh` uses) runs for `BENCH_SECONDS` (30). The
  run records:
  - the CPU time of Labwc and foot, and the system's busy share;
  - on the GPU, the frame intervals from `G9P_TRACE`. Renders under 4 ms
    apart count as one frame.

  The pixman benchmark runs before the switch, the GPU one after the
  20-second check.
- **The GPU desktop stays.** When it passes, Firefox is reopened on it and
  nothing is restored or unloaded. The outcome is `labwc-gpu-kept`.
  - The GPU Labwc's output goes through its own logger. It keeps the log
    to at most 4 MB plus a 4 MB `.1`, since `G9P_TRACE` writes a line for
    every render and `/run` is RAM.
  - If the GPU desktop fails its checks, it is restored to pixman as in
    `--labwc`.
- **Back to pixman on the same boot:** `restore-desktop.py --run RUN` stops
  the GPU Labwc and starts Labwc from the recorded command line and
  environment, with Firefox. It runs once per kept session and has its
  own marker.
  - The modules stay loaded, and the GPU suspends once Labwc lets go.
  - A reboot clears everything.

### Kept run 1 (`runs/20261112-213047`, 2026-11-12): benchmark, GPU desktop kept

The outcome was `labwc-gpu-kept`. All four cases passed, with no
timeouts, crashes or faults. The GPU desktop came up on Zink and stayed,
and Firefox was reopened on it.

| 30 s, scrolling foot | pixman | GPU (Zink) |
| --- | --- | --- |
| Labwc CPU | 4.29 s (14.3 % of a core) | 4.33 s (14.4 %) |
| foot CPU | 11.4 s | 12.1 s |
| system busy (3 cores) | 64.9 % | 66.9 % |
| frames | not counted | 4294, mean 7.13 ms (140 fps); all under 20 ms |

- simpledrm has no vblank, so its page flips complete at once and Labwc
  renders as fast as foot gives it damage. That is why there are 140
  frames a second.
- At the same Labwc CPU time, the GPU path drew 4294 full 1668x2224
  frames in 30 s, about 1 ms of Labwc's CPU per frame.
- Pixman's frame count is not traced, so the two frame rates are not
  compared here.

**In use (about 10 minutes):**
- The user tried touch, Firefox and the on-screen keyboard on the GPU
  desktop and saw no problem.
- A read-only check afterwards found the following:
  - no firmware crash, job timeout or kernel fault;
  - Labwc (`WLR_RENDERER=gles2`), foot, the shell and Firefox running;
  - the GPU runtime-suspended while idle;
  - the capped log at 1.2 MB.
- Open: Labwc logged `ZINK: vkMapMemory failed (VK_ERROR_MEMORY_MAP_FAILED)`
  57 times.
  - The failures come right after touch focus/raise actions, from about
    5 minutes in. Nothing visible came with them.
  - Some buffer Zink maps for the CPU is refused by the driver. A
    texture upload or a readback are both possible. The A10 notes list a
    mapping the driver does not offer yet (grim's screencopy).

**The `vkMapMemory` failures** have the following cause and fix:
- **Cause:** G9P copies on the CPU. `g9p_cpu_addr()` maps the memory it
  touches and keeps the mapping. An application's later `vkMapMemory` of
  the same memory then hit "Memory object already mapped" in
  `pvr_MapMemory2`. Zink's staging memory for texture uploads is such
  memory, and a touch makes a client redraw, which makes Labwc upload.
- **Fix:** `../mesa-t8011/0006-pvr-vkMapMemory-returns-the-G9P-backend-mapping.patch`.
  `vkMapMemory` returns the existing mapping, and a placed map replaces
  it.
- **Checks:**
  - `gl-probe`'s new `GLP_UPLOADS=200` re-fills a texture, draws it and
    reads it back, 200 times. It runs as `gl-probe-uploads` in
    `LABWC_CASES`.
  - A case whose output has `vkMapMemory failed` fails.
  - The GPU session's log is counted (`map_failures`), and it must be 0.
- Re-pinned:
  - `libvulkan_powervr_mesa.so` is `41b9c056`;
  - `j208-zink.tar.gz` is `02d65564`;
  - `gl-probe` is `fa7b5a8d`.

**Same-boot A/B of 0006** ran on kept run 1's boot, with the GPU desktop still
up on the old libraries. The new tree and `gl-probe` were unpacked to their own
directory under `/run`, and `gl-probe` ran once against the old Zink tree and
once against the new one.

| `gl-probe` uploads | old tree | new tree |
|---|---|---|
| 200 rounds at 64x64 | PASS, 0 map failures | PASS, 0 |
| 40 rounds at 1024x1024 | PASS, 0 | PASS, 0 |
| 20 rounds at 2048x2048 | PASS, 0 | PASS, 0 |
| 200 rounds at 64x64, decoy pending | PASS, 0 | PASS, 0 |
| 40 rounds at 1024x1024, decoy pending | PASS, 0 | PASS, 0 |

- No new crash or timeout appeared in dmesg, and the desktop (Labwc, foot,
  qs, Firefox) stayed up.
- **The old tree did not fail, so the A/B shows nothing.**
- The desktop's own log kept counting failures on the old tree: 85 by then,
  in bursts at minutes 10, 11 and 16.
- Why `gl-probe` misses it:
  - Zink copies into an idle texture straight from the client's memory
    (`VK_EXT_host_image_copy`, which the driver offers), with no
    `vkMapMemory`. `gl-probe` reads back after every round, so its texture
    is always idle.
  - Into a busy texture, Zink goes through staging memory it maps. Each
    round now fills and draws a decoy colour first, then fills the real one
    with that draw pending. That path still did not fail.
- **So the cause is inferred, not shown.**
  - Labwc's log has only Zink's line. The driver's own message goes to a
    debug messenger, not stderr.
  - The device has no strace or gdb.
  - The other way `vkMapMemory` fails is the mmap itself; the old driver
    does not say which case it hit.
- **0006 now also logs, with `G9P_TRACE` (on in the GPU session):**
  - `g9p: vkMapMemory of N bytes got the backend's mapping` for each map
    that hit the case 0006 handles;
  - `g9p: vkMapMemory of N bytes failed: … (errno E)` for each map that
    failed in the mmap.
- The runner counts the first as `map_reuses`, and lists up to 5 of the
  second in the log summary.
- `gl-probe` changes:
  - `GLP_UPLOAD_SIZE=S` sets the texture's size;
  - `GLP_UPLOADS` draws the decoy first.
- Re-pinned:
  - `libvulkan_powervr_mesa.so` is `1bb3be30`;
  - `j208-zink.tar.gz` is `8e1263bd`;
  - `gl-probe` is `b7b9af81`.
- **Next boot:** `--labwc-keep`, then use the desktop. Afterwards, count in
  its log:
  - `map_failures` 0 and `map_reuses` > 0 confirms both the cause and the
    fix;
  - failures with `g9p: … failed` lines mean the mmap fails, which is
    another cause.

**Kept run 2** (boot `20261113-081222`, Mesa 0006 with its tracing) confirms
both the cause and the fix.
- The run itself:
  - stage A was clean;
  - all 5 cases passed;
  - the bench gave 140.3 fps, as in run 1;
  - `map_failures` and `map_reuses` were both 0 at hand-over.
- **First use: too little to tell.** For about 37 minutes the desktop was
  mostly left alone. It rendered once a minute (qs's clock), plus one
  burst of 103 renders with 5 touches. There were no failures and no
  reuses, so this said nothing.
- **Second use, about 15 minutes:** 150 focus/raise actions, and about
  1900 renders in two busy minutes.

| Log line | Count |
|---|---|
| `ZINK: vkMapMemory failed` | 0 |
| `g9p: vkMapMemory of N bytes got the backend's mapping` | 5 (2 097 152, 2 097 152, 1 458 176, 1 458 176 and 3 719 168 bytes) |
| `g9p: vkMapMemory of N bytes failed` | 0 |

- dmesg had no crash or timeout. Labwc, foot, qs and Firefox stayed up,
  and the GPU runtime-suspended when idle.
- **Each reuse is a map the old driver refused with "already mapped".**
  - These are Zink's maps of memory the G9P backend had mapped for a CPU
    copy: staging buffers of 1.4 to 3.7 MB.
  - With 0006, each gets that mapping, and Zink logs no failure.
  - No map failed in the mmap.
- 0006 is verified on hardware.

### Pixman's frames (`frame-count`, boot `20261113-081222`)

Kept run 1 counted only the GPU's frames, from the G9P trace. Pixman
renders without a trace, and the kernel has no function tracer or kprobes.
`../mesa-t8011/frame-count.c` now counts frames for both renderers, from
the client side.

**How `frame-count` counts:**
- It maps a 32x32 window and commits every 2 ms with presentation feedback
  (`wp_presentation`). Its commits carry no content, so they damage nothing.
- Labwc answers `presented` for each output frame that drew the window
  again. Each frame takes the latest commit's feedback, and the ones before
  it are discarded.
- **Three things it needs** (each found by a run that failed):
  - **Presentation feedback, not frame callbacks.** A pending frame
    callback makes wlroots schedule a frame. Without vblank, callbacks
    would spin with nothing rendered.
  - **A half-transparent window.** wlroots draws a surface again only where
    the frame's damage meets it. The damage of the window below stops where
    an opaque window covers it, so an opaque window got 1 `presented` in
    12 s while foot scrolled under it.
  - **Commits on a timer.** With one commit per `presented`, 24 % of the
    frames were missed. Labwc starts the next frame before the client
    answers.
- Idle, 10 s gives 2–3 `presented`, so the empty commits trigger no frames.
- On the GPU it counted 4527 frames in 30 s; the G9P trace had 4579
  renders at the same time, about 1 % apart.
- Each trace render is a whole 1668x2224 frame. `frame_stats` joins
  renders less than 4 ms apart into one frame, as the A10 did, and so
  counts about 10 % low when Labwc runs unthrottled (4140 there).

**30 s, scrolling foot, Labwc on the GPU then on pixman** (same boot; the
desktop went back to pixman with `restore-desktop.py`):

| | GPU (Zink) | pixman |
| --- | --- | --- |
| frames (`frame-count`) | 4527, 150.9 fps | 4227, 140.9 fps |
| frame intervals | mean 6.63 ms, all under 20 ms | mean 7.10 ms, all under 20 ms |
| Labwc CPU | 0.145 of a core (kept run 2's bench) | 0.114 of a core |
| Labwc CPU per frame | about 0.96 ms | 0.81 ms |

- **The two renderers run at about the same frame rate.**
  - Without vblank, both render as fast as foot damages.
  - pixman repaints only the damage. The GPU path renders the whole target
    each frame.
- **This workload costs Labwc a little less CPU on pixman.**
  - The GPU's CPU figure is from kept run 2's bench, not the same 30 s as
    its frame count.
  - The next `--labwc`/`--labwc-keep` run measures both together.
- The runner's `bench()` now runs `frame-count` over the same 30 s as the
  CPU sample, for both renderers. It records `frames_client` and
  `labwc_cpu_ms_per_frame`, and keeps the G9P trace's `frames` on the GPU.
- `frame-count` is pinned in `ZINK_FILES` (`19ef8afd`).

### The GPU desktop in one command (`gpu-desktop.py`, `--desktop`)

This is how a boot gets the GPU desktop without changing the image. The
image itself stays as it is; a boot image that brings up the GPU in init
would need `desktop_images.py` and `desktop-session.py`.

1. Boot with `desktop-session.py --usable --audio-identify --minutes 180`,
   then press `w` (Wi-Fi) and `x` (SSH).
2. Press `p` in the same menu (below), or run:

   ```
   python3 driver-work/gpu/pvr-t8011/gpu-desktop.py --run runs/RUN --confirmation gpu-desktop-once > log 2>&1
   ```

**What it does:**
- **SSH first.** `x` writes `ssh-runtime-ready.json` only once the SSH
  client is verified, a little after the launch, and stage A refuses to
  start without it. The script waits up to 5 minutes for it.
- **Stage A** (`../gfx-power/run-gfx-power.py`) runs first. If it already
  ran on this boot, its result is used instead; either way it has to be a
  success.
- **`run-pvr-t8011.py --desktop`** comes next. It has its own confirmation,
  marker, result and stream, and runs once per boot, like the other modes.
  - It does everything `--labwc-keep` does except the benchmark: the
    probe, the compute jobs, `LABWC_CASES` and the GPU session's checks.
  - When those pass, Labwc stays on the GPU with Firefox reopened.
  - Outcome: `labwc-gpu-kept`.
- Each step's output goes to `gpu-desktop-stage-a.log` and
  `gpu-desktop-runner.log` in the run directory.
- **Exit code:** 0 when the desktop was kept. Otherwise 1, with stage A's
  and the runner's outcomes printed.
- `restore-desktop.py` now accepts either kept result, `--labwc-keep`'s or
  `--desktop`'s.
- **In the desktop-session menu (`[p]`).** It shows once the image is
  the audio-identify one (the base stage A checks) and SSH is ready.
  - Before the GPU desktop it reads `[p] GPU 桌面` and runs
    `gpu-desktop.py`, with the output in `gpu-desktop.log`.
  - While the desktop is kept on the GPU it reads `[p] 切回 pixman 桌面`
    and runs `restore-desktop.py`, with the output in
    `gpu-desktop-restore.log`.
  - It is gone once both have run, or if stage A already failed on this
    boot. The markers decide; the menu adds no state of its own.
  - `desktop_images.py` and the image are unchanged. The GPU still comes
    up after the desktop, about 2 minutes after `p`.
- **Run on hardware, boot `20261113-102932`: works.**
  - The first try came about a minute after `x`, before the ready record
    existed. Stage A refused (`SSH boundary is not ready`) without writing
    its marker, so nothing ran and nothing was used up. That is why the
    script now waits.
  - The second try: stage A succeeded, then `--desktop` gave
    `labwc-gpu-kept`: stage A ended at 10:34:15, the runner at 10:35:32,
    about 1.5 minutes from SSH ready (10:34:04).
  - Details:
    - render 5/5 passed;
    - no loader override;
    - `map_failures` 0, 15 renders traced;
    - Firefox reopened;
    - the GPU readback had 77628 colours against pixman's 1026 before.
  - The 6 `Failed to render cursor buffer` debug lines were there in the
    kept run on boot `20261113-081222` as well.
