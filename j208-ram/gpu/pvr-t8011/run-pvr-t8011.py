#!/usr/bin/env python3
"""First run of pvr_apple on T8011: probe once, let it power off, unload.

On a fresh audio-image boot after a clean stage A, with no stage B-J on the
boot (stage J's gate is reused), once per recorded session:

  1. upload the pinned modules (build/) and the carved G9G firmware as
     apple/gfx-t8011.bin under a /run directory; point firmware_class.path
     there;
  2. insmod gpu-sched, drm_exec, drm_gpuvm (those not loaded), then
     j208-gpu-dt (nodes, GFX under DWI), j208-rtkit (mailbox binds) and
     j208-powervr exp_hw_support=1 with dynamic debug on. The probe powers
     DWI and GFX, sets up bridges 7/9, starts the mailbox, boots the
     firmware, requests state 4, reads RGX +0x20/+0x18 once and hands over
     the init data. No job is submitted;
  3. wait up to 10 s for runtime autosuspend: the firmware is stopped and
     GFX and DWI power off;
  4. rmmod in reverse, check the nodes are gone and both domains off,
     restore firmware_class.path.

With --compute-job (its own confirmation and marker), the GPU is left to
autosuspend after probe, then pvr-compute-job.py submits terminate-only
compute jobs through the render node (the GPU resumes for them): one, two
back to back, then a burst of 300. The GPU must autosuspend again before
the unload. If the kernel log shows a firmware crash, the run stops there:
the power-off after a crash is untested since DWI is held (the hang in
runs/20261109-000414 came from the DWI cut). The modules stay loaded until
a reboot.

With --render (implies --compute-job; its own confirmation, marker, result
and stream), the compute jobs are followed, once the GPU autosuspended
again, by Orchard's Mesa (PowerVR Vulkan with the G9P backend, A10X device
info: ../mesa-t8011) and its vk-tri test: a cleared target with one
vertex-coloured triangle, through geometry and fragment jobs. The cases in
RENDER_CASES run in order and stop at the first that does not pass; the
first one's image is kept. Then the GPU must autosuspend again before the
unload.

With --suite (its own confirmation, marker, result and stream), as --render
with SUITE_CASES: RENDER_CASES, then the vk-tri variants (textures, depth,
blending, loads, ...) and the fork's vk-compute-test shaders. A case that
fails does not stop the suite; a firmware crash or a job timeout does.

With --gl (its own confirmation, marker, result and stream), as --render
with GL_CASES: vk-tri at 64x64, then OpenGL ES through Zink
(../mesa-t8011/build-zink.sh, unpacked under /run/j208-zink) with gl-probe:
GBM on the render node, EGL, a GLES 2 context, clears, a triangle, blending,
a texture and a GBM dma-buf imported as a render buffer, all read back. It
never opens the display device: the desktop keeps running.

With --labwc (its own confirmation, marker, result and stream), as --render
with LABWC_CASES (vk-tri at 64x64, gl-probe, and gl-probe's scanout check:
the compositor's buffers allocated on simpledrm and drawn by the GPU), then,
when the first two and a scanout case pass, the desktop on the GPU for
LABWC_SECONDS: the running Labwc
(pixman) is stopped, which closes its clients (shell, foot, Firefox), and
started again on wlroots' GLES2 renderer, on the render node through Zink,
scanning out to simpledrm. Its clients start from the same autostart, with
the Zink environment taken out (they draw in software as before). The run
reads back what the display shows (kms-grab) before, on the GPU and after,
then stops the GPU Labwc and starts Labwc as it ran before (its environment
and command line from /proc), and Firefox again if it was open. The desktop
is down for a moment twice; an SSH timeout in between leaves it down until
a reboot.

With --labwc-keep (its own confirmation, marker, result and stream), as
--labwc, with a benchmark of each renderer (a scrolling foot for
BENCH_SECONDS: Labwc's CPU time, and on the GPU its frame intervals), and
the GPU desktop then kept for use with Firefox reopened: nothing is restored
or unloaded, the GPU Labwc's log is capped (LABWC_LOG_CAP, rotated), and
restore-desktop.py brings Labwc back on pixman. A reboot clears it all.

With --desktop (its own confirmation, marker, result and stream), as
--labwc-keep without the benchmark: the same checks, then the GPU desktop
kept for use. gpu-desktop.py runs stage A and then this, as one command
after a boot.

With --keep-powered (implies --compute-job; its own confirmation, marker,
result and stream), the GPU is held on through power/control before the
jobs, and nothing is unloaded or powered off after them. The device needs
a reboot afterwards.

Every step is one SSH command. A command that times out (a hang) ends the
run with no cleanup and no retry. The kernel log is streamed to the run
directory throughout.
"""
import argparse
import base64
import gzip
from datetime import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
GPU = HERE.parent
RAM = HERE.parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


KFL = load('j208_pvr_stage_j', GPU / 'gfx-kf-hold4/run-gfx-kfl.py')
require = KFL.require

BUILD = HERE / 'build'
MODULES = {
    # local path: sha256
    'deps/gpu-sched.ko': '4158f97edcebefcb32070f7851ac2ffd1234e9c5796f16cbe215fc15f5b81862',
    'deps/drm_exec.ko': 'd9eb9183dcf1bc6900783d70b4bbc37e51dfb35496ee467fb87de0e7854dff4b',
    'deps/drm_gpuvm.ko': '063bff4882a7eaa89675460dbe3b9b5aac908ef85e8807405144851f85a8215d',
    'tree/j208-gpu-dt.ko': '215a35553edc9ef9e2e974dbc7587610cad6cbd1576de3d7cc59097e4829b8d6',
    'tree/j208-rtkit.ko': '4eeeb754e15d899026785ff4c32dbb020010f923832e61c9f96a6ee9ce4b30a6',
    'tree/drivers/gpu/drm/imagination/j208-powervr.ko':
        '10e5d2a7c0bc39ea8a4ef535b177d19fa45c8e55b8a0f6b9e13325d3e4eedd98',
}
# insmod order: (sysfs module name, local path, arguments)
DEPS = (('gpu_sched', 'deps/gpu-sched.ko', ''), ('drm_exec', 'deps/drm_exec.ko', ''),
        ('drm_gpuvm', 'deps/drm_gpuvm.ko', ''))
OURS = (('j208_gpu_dt', 'tree/j208-gpu-dt.ko', ''),
        ('j208_rtkit', 'tree/j208-rtkit.ko', 'dyndbg=+p'),
        ('j208_powervr', 'tree/drivers/gpu/drm/imagination/j208-powervr.ko',
         'exp_hw_support=1 dyndbg=+p'))

REMOTE = '/run/j208-pvr-t8011'
REMOTE_FIRMWARE = REMOTE + '/fw/apple/gfx-t8011.bin'
FW_PATH = '/sys/module/firmware_class/parameters/path'
GPU_DEV = '/sys/bus/platform/devices/201000000.gpu'
MBOX_DEV = '/sys/bus/platform/devices/201d24000.mbox'
DT_GPU = '/sys/firmware/devicetree/base/soc/gpu@201000000'
DT_MBOX = '/sys/firmware/devicetree/base/soc/mbox@201d24000'
MARKER = 'pvr-t8011-attempted.json'
RESULT = 'pvr-t8011-result.json'
STREAM = 'pvr-t8011-stream.log'
CONFIRMATION = 'probe-pvr-t8011-once'
JOB_SCRIPT = HERE / 'pvr-compute-job.py'
JOB_SCRIPT_SHA = 'a8976d2caf611a33d0b78438f8f07aee4d57336a79fa445cc9217d1a827d3a11'
# --compute-job: file names and confirmation of that mode
JOB_MODE = dict(marker='pvr-t8011-compute-attempted.json',
                result='pvr-t8011-compute-result.json',
                stream='pvr-t8011-compute-stream.log',
                confirmation='compute-pvr-t8011-once')
# --render: compute jobs, then vk-tri through Mesa
RENDER_MODE = dict(marker='pvr-t8011-render-attempted.json',
                   result='pvr-t8011-render-result.json',
                   stream='pvr-t8011-render-stream.log',
                   confirmation='render-pvr-t8011-once')
MESA_BUILD = GPU / 'mesa-t8011/build'
MESA_FILES = {
    # local name: sha256 (../mesa-t8011/build.sh)
    'libvulkan_powervr_mesa.so':
        '1bb3be30106d151aca06730fae2115f9b995d74ac8d65a9a0407d2d21adfbc2e',
    'powervr_icd.json':
        'e60b1b80be753076b9ce01472d4e8c1604f4d89d5d4777f2ebeb45d2d69f5f68',
    'vk-tri':
        'd949525fe54a2445dc73fea9b997e7ec184cf9cfc8cf5e02c7a073257c2c2c1d',
    'vk-compute-test':
        '2c182a6dac30cb402c285ff9757a48670edb4cdb7a9c816fb89487b2fa1f7a5b',
    'kms-grab':
        'a3fd5d606b9bfd15d682f2548d849544b6758bc6cb4a85e615910476a8deeb48',
}
REMOTE_MESA = '/run/j208-mesa'
VK_ENV = ('VK_DRIVER_FILES=%s/powervr_icd.json VK_ICD_FILENAMES=%s/powervr_icd.json '
          'PVR_I_WANT_A_BROKEN_VULKAN_DRIVER=1' % (REMOTE_MESA, REMOTE_MESA))
# (name, program, environment, arguments): the A10's first frame, then larger targets
RENDER_CASES = (('tri-64', 'vk-tri', '', ''), ('tri-512', 'vk-tri', 'TRI_SIZE=512x512', ''),
                ('tri-screen', 'vk-tri', 'TRI_SIZE=1668x2224', ''))
# --suite: vk-tri variants (vk-tri.c's header) and vk-compute-test's shaders
SUITE_MODE = dict(marker='pvr-t8011-suite-attempted.json',
                  result='pvr-t8011-suite-result.json',
                  stream='pvr-t8011-suite-stream.log',
                  confirmation='suite-pvr-t8011-once')
COMPUTE_TESTS = ('basic', 'float', 'ifelse', 'loop', 'break', 'fcmp', 'twobreak', 'julia',
                 'juliapc', 'palette', 'palette_nopc', 'palette_hoist')
SUITE_CASES = RENDER_CASES + tuple(('tri-' + n, 'vk-tri', e, '') for n, e in (
    ('linear', 'TRI_LINEAR=1'),
    ('tex', 'TRI_TEX=1'),
    ('tex-linear', 'TRI_TEX=linear'),
    ('tex-filter', 'TRI_TEX=1 TRI_FILTER=linear'),
    ('tex-300x200', 'TRI_TEX=linear TRI_TEX_SIZE=300x200'),
    ('discard', 'TRI_DISCARD=1'),
    ('coord', 'TRI_COORD=1'),
    ('scissor', 'TRI_SCISSOR=1'),
    ('blend', 'TRI_BLEND=1 TRI_VCOL=0.25,0.5,0.75,0.5 TRI_WANT=0xff4080df'),
    ('blend-dst', 'TRI_BLEND=dst TRI_WANT=0xff000040'),
    ('strip', 'TRI_STRIP=1'),
    ('flipy', 'TRI_FLIPY=1'),
    ('clearatt', 'TRI_CLEARATT=1'),
)) + tuple(('compute-' + n, 'vk-compute-test', '', n) for n in COMPUTE_TESTS) + (
    ('compute-basic-4096', 'vk-compute-test', '', 'basic 4096'),
) + tuple(('tri-' + n, 'vk-tri', e, '') for n, e in (
    ('depth-pass', 'TRI_DEPTH=pass'),
    ('depth-fail', 'TRI_DEPTH=fail'),
    ('depth-load', 'TRI_DEPTH=load'),
    ('depth-screen', 'TRI_DEPTH=pass TRI_SIZE=1668x2224'),
    ('load', 'TRI_LOAD=1'),
    ('load-draw', 'TRI_LOAD=2'),
    ('load-clear', 'TRI_LOAD=3'),
    ('draws-100', 'TRI_DRAWS=100'),
    ('instances-100', 'TRI_INSTANCES=100'),
    ('pre-3', 'TRI_PRE=3'),
    ('repeat-100', 'TRI_REPEAT=100'),
))
# --gl: OpenGL ES through Zink, off screen (gl-probe), after one vk-tri
GL_MODE = dict(marker='pvr-t8011-gl-attempted.json',
               result='pvr-t8011-gl-result.json',
               stream='pvr-t8011-gl-stream.log',
               confirmation='gl-pvr-t8011-once')
ZINK_BUILD = GPU / 'mesa-t8011/build-zink'
ZINK_FILES = {
    # local name: sha256 (../mesa-t8011/build-zink.sh)
    'j208-zink.tar.gz':
        '8e1263bdfebdb60afbb5ed226d48b49d3e90d088fae0d287f645161366d4e8f6',
    'gl-probe':
        'b7b9af812b6a62579cfd357b854b2192d8e36517fc732c061f3b887bfcf0635a',
    # Labwc's output frames as a client sees them, for the benchmark
    'frame-count':
        '19ef8afd1aa3d4202a1452c6340982786b6a2f01c1bd640da428ca2f4ae52eac',
    # SPIR-V for vk-tri (../mesa-t8011/shaders)
    'novary-vert.spv':
        '16f9f8dbb996b108ef4d0733bfa929c048cc5968b86fdebf6c113a28d6d855f7',
    'cola-vert.spv':
        'b12551367715e392eadeb4894f0336353e0e4ed4d2b18591d717852599333ae4',
    'const-frag.spv':
        '8ce7c482d6db3c2f0c44bef93958f2f00c73caa5e77ab13ca7920636d444d6dc',
    'push-frag.spv':
        'ad6055d3b24876ddf171228a989599abc27c6efac43d8b7478f02213fbbc2fb5',
    'ubo-frag.spv':
        '7475d5c5d612aa353df7c16b4a4c268347094d216ec1a3ecb97532129e9e0a4b',
}
REMOTE_ZINK = '/run/j208-zink'     # Mesa's configured prefix
ZINK_ENV = ('LD_LIBRARY_PATH={0}/lib '
            '__EGL_VENDOR_LIBRARY_FILENAMES={0}/share/glvnd/egl_vendor.d/50_mesa.json '
            'VK_DRIVER_FILES={0}/share/vulkan/icd.d/powervr_mesa_icd.aarch64.json '
            'MESA_LOADER_DRIVER_OVERRIDE=zink MESA_SHADER_CACHE_DISABLE=true '
            'PVR_I_WANT_A_BROKEN_VULKAN_DRIVER=1'.format(REMOTE_ZINK))
# GL run 3: Zink's draws leave no pixel in GPU memory, while vk-tri's draw
# (vertex varyings, no fragment uniforms) works. Zink's draw has no varyings
# and its colour comes from a uniform buffer DMA'd into the fragment common
# store. The cases split those apart, in Vulkan (vk-tri with shaders/: the
# colour from a constant, push constants or a uniform buffer, with and
# without varyings) and in GL (gl-probe's one triangle, the colour from a
# constant, a varying or a uniform). All run; a crash or timeout stops.
_NOVARY = 'TRI_VS_SPV=novary-vert.spv '
_CONST = 'TRI_FS_SPV=const-frag.spv TRI_WANT=0xffff0000'
# GL run 6 (runs/20261112-184806): VDM_STATE's vertex-shader register
# allocation was the cause: 10 registers (5 units) draw nothing, 12, 14 and
# 16 draw, through VDM_STATE alone (Mesa patch 0003's G9P_VS_VDM_REGS). Mesa
# patch 0004 rounds it up to 4 registers. These cases check it with no
# override: the position-only shaders, cola.vert, every GL scene, and suite
# cases whose vertex tasks the change also touches (more registers per task,
# MAX_INSTANCES from them).
_COLOURS = (('const', _CONST),
            ('push', 'TRI_FS_SPV=push-frag.spv TRI_PUSH=0,1,0,1 TRI_WANT=0xff00ff00'),
            ('ubo', 'TRI_UBO=1 TRI_FS_SPV=ubo-frag.spv TRI_WANT=0xff0080ff'))
GL_CASES = ((RENDER_CASES[0], RENDER_CASES[2])
            + tuple(('tri-' + n, 'vk-tri', e, '') for n, e in (
                ('tex', 'TRI_TEX=1'),
                ('clearatt', 'TRI_CLEARATT=1'),
                ('draws-100', 'TRI_DRAWS=100'),
                ('instances-100', 'TRI_INSTANCES=100')))
            + tuple(('vk-novary-' + n, 'vk-tri', _NOVARY + e, '') for n, e in _COLOURS)
            + (('vk-cola-const', 'vk-tri', 'TRI_VS_SPV=cola-vert.spv ' + _CONST, ''),)
            + tuple(('gl-quick1-' + f, 'gl-probe', 'GLP_QUICK=1 GLP_FS=' + f, '')
                    for f in ('const', 'varying', 'uniform'))
            + (('gl-probe', 'gl-probe', '', ''),))
# --gl, when gl-probe fails (GL run 2: clears right, no draw visible): the
# streams and shaders of the GL triangle and of vk-tri's position-only case
# (GL runs 3-5 kept earlier sets: runs/20261109-221642, 20261112-174706).
# Each output is saved as pvr-t8011-gl-diag-NAME.txt.
GL_DIAG = (
    ('quick1-const-dump', 'gl-probe', 'GLP_QUICK=1 GLP_FS=const G9P_DEBUG=1 G9P_DUMP=1', ''),
    ('vk-novary-const-dump', 'vk-tri', _NOVARY + _CONST + ' G9P_DEBUG=1 G9P_DUMP=1', ''),
)
RENDER_TIMEOUT = 60
# --labwc: the desktop on the GPU (Labwc on GLES2 through Zink), after the
# Vulkan and GL checks
LABWC_MODE = dict(marker='pvr-t8011-labwc-attempted.json',
                  result='pvr-t8011-labwc-result.json',
                  stream='pvr-t8011-labwc-stream.log',
                  confirmation='labwc-pvr-t8011-once')
# Desktop run 1 (runs/20261112-203019): with MESA_LOADER_DRIVER_OVERRIDE=zink
# wlroots' GBM allocator on simpledrm failed, and its dumb buffers (6672-byte
# rows) were refused by the driver (64-byte units). Without the override Mesa
# pairs simpledrm with Zink (kmsro); Mesa patch 0005 widens those dumb
# buffers. gl-probe's GLP_SCANOUT checks that path before Labwc, with Mesa
# choosing the driver itself, then with kmsro named; Labwc runs with the first
# that passes. All cases run; a crash or timeout stops.
# --labwc-keep: as --labwc, with a benchmark (a scrolling foot, pixman then
# the GPU), and the GPU desktop kept for use: no restore, the modules stay
# loaded until a reboot (restore-desktop.py brings pixman back)
LABWC_KEEP_MODE = dict(marker='pvr-t8011-labwc-keep-attempted.json',
                       result='pvr-t8011-labwc-keep-result.json',
                       stream='pvr-t8011-labwc-keep-stream.log',
                       confirmation='labwc-keep-pvr-t8011-once')
# --desktop: as --labwc-keep without the benchmark (gpu-desktop.py)
DESKTOP_MODE = dict(marker='pvr-t8011-desktop-attempted.json',
                    result='pvr-t8011-desktop-result.json',
                    stream='pvr-t8011-desktop-stream.log',
                    confirmation='desktop-pvr-t8011-once')
LABWC_DISPLAY = '1668x2224'
LABWC_SCANOUT = (('scanout-auto', ''), ('scanout-kmsro', 'kmsro'))
# Kept run 1: Zink's vkMapMemory failed, likely for memory the G9P backend
# had mapped for its CPU copies (Mesa patch 0006); gl-probe-uploads repeats
# texture uploads as a compositor does. Any case whose output has MAP_FAILED
# fails. With G9P_TRACE, the driver logs MAP_REUSED for each map that got the
# backend's mapping and MAP_DETAIL for each that failed (with its errno).
LABWC_CASES = ((RENDER_CASES[0], ('gl-probe', 'gl-probe', '', ''),
                ('gl-probe-uploads', 'gl-probe', 'GLP_UPLOADS=200', ''))
               + tuple((name, 'gl-probe', 'GLP_SCANOUT=%s MESA_LOADER_DRIVER_OVERRIDE=%s' % (
                   LABWC_DISPLAY, override), '') for name, override in LABWC_SCANOUT))
MAP_FAILED = 'vkMapMemory failed'
MAP_REUSED = "got the backend's mapping"
MAP_DETAIL = 'g9p: vkMapMemory of [0-9]+ bytes failed'
LABWC_SECONDS = 20          # the GPU desktop runs this long before the readback
LABWC_UP_WAIT = 45          # for Labwc, its autostart and foot to come up
LABWC_STOP_WAIT = 10        # for Labwc to exit after SIGTERM (then SIGKILL)
LABWC_CMDLINE = ['labwc', '-d', '-C', '/etc/labwc']     # peripherals/init
REMOTE_LABWC = REMOTE + '/labwc'        # the GPU session's config and logs
# the GPU session's clients run without the Zink environment
LABWC_CLIENT_UNSET = ('LD_LIBRARY_PATH', '__EGL_VENDOR_LIBRARY_FILENAMES', 'VK_DRIVER_FILES',
                      'MESA_LOADER_DRIVER_OVERRIDE', 'MESA_SHADER_CACHE_DISABLE',
                      'PVR_I_WANT_A_BROKEN_VULKAN_DRIVER', 'WLR_RENDER_DRM_DEVICE',
                      'WLR_DRM_DEVICES', 'G9P_TRACE')
# --keep-powered: the GPU stays on from before the jobs until a reboot
KEEP_MODE = dict(marker='pvr-t8011-burst-attempted.json',
                 result='pvr-t8011-burst-result.json',
                 stream='pvr-t8011-burst-stream.log',
                 confirmation='burst-pvr-t8011-once')
# trace runs (j208-powervr apple_off_trace_ms, since removed) used this marker
TRACE_MARKER = 'pvr-t8011-traceoff-attempted.json'
MODE = dict(marker=MARKER, result=RESULT, stream=STREAM, confirmation=CONFIRMATION, job=False,
            keep=False, render=False, suite=False, gl=False, labwc=False, keep_desktop=False,
            bench=False)
DONE = 'J208_PVR_T8011 done'
SUSPEND_WAIT = 10
JOB_TIMEOUT = 120
CRASHED = 'GFX firmware crashed'
TIMED_OUT = 'Apple GPU job timed out'
FOLLOW_SECONDS = 900     # the Mesa upload alone took over a minute
FOLLOW_READY = 'J208_PVR_FOLLOWER ready'

# Read-only: forwards kernel lines about the GPU, its mailbox, genpd and faults.
FOLLOWER = r"""import os,re,select,time
want=re.compile(r'j208|powervr|201000000\.gpu|201d24000\.mbox|rtkit|genpd|gfx|dwi|BUG:|Oops|SError|panic|Unable to handle|J208_PVR')
fd=os.open('/dev/kmsg',os.O_RDONLY|os.O_NONBLOCK)
os.lseek(fd,0,os.SEEK_END)
print('J208_PVR_FOLLOWER ready',flush=True)
end=time.monotonic()+%d
while time.monotonic()<end:
    if not select.select([fd],[],[],1)[0]:
        continue
    try:
        record=os.read(fd,8192).decode(errors='replace')
    except (BlockingIOError,BrokenPipeError):
        continue
    head,_,rest=record.partition(';')
    message=rest.split('\n',1)[0]
    if want.search(message):
        print(head.split(',')[2] if head.count(',')>=2 else '', message, flush=True)
        if %r in message:
            break
""" % (FOLLOW_SECONDS, DONE)

# Read-only device state for this run.
STATE = r"""import json,os,pathlib
P=pathlib.Path
def text(p):
    try: return P(p).read_text().strip()
    except OSError: return None
genpd=[]
s=text('/sys/kernel/debug/pm_genpd/pm_genpd_summary')
for line in (s or '').splitlines():
    f=line.split()
    if len(f)>=2 and f[0] in ('gfx','dwi'):  # skip the parent's child rows
        genpd.append(f[:2])
print(json.dumps({
 'modules':{m:P('/sys/module/'+m).exists() for m in
   ('gpu_sched','drm_exec','drm_gpuvm','j208_gpu_dt','j208_rtkit','j208_powervr','powervr')},
 'dt_gpu':P(%r).exists(),'dt_mbox':P(%r).exists(),
 'gpu_dev':P(%r).exists(),'mbox_dev':P(%r).exists(),
 'gpu_driver':os.path.basename(os.readlink(%r+'/driver')) if P(%r+'/driver').exists() else None,
 'mbox_driver':os.path.basename(os.readlink(%r+'/driver')) if P(%r+'/driver').exists() else None,
 'gpu_runtime':text(%r+'/power/runtime_status'),
 'render':sorted(p.name for p in P('/dev/dri').iterdir() if p.name.startswith('renderD')) if P('/dev/dri').exists() else [],
 'fw_path':text(%r),
 'genpd':genpd,
 'debugfs':P('/sys/kernel/debug/pm_genpd').exists(),
}))
""" % (DT_GPU, DT_MBOX, GPU_DEV, MBOX_DEV, GPU_DEV, GPU_DEV, MBOX_DEV, MBOX_DEV,
       GPU_DEV, FW_PATH)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_session(session):
    metadata = KFL.validate_session(session)
    for path, digest in MODULES.items():
        require(sha(BUILD / path) == digest, path + ' differs from the reviewed build')
    require(not (metadata['session'] / KFL.MARKER).exists(),
            'Stage J already ran in this session; use a fresh boot')
    for marker in (MARKER, JOB_MODE['marker'], KEEP_MODE['marker'], RENDER_MODE['marker'],
                   SUITE_MODE['marker'], GL_MODE['marker'], LABWC_MODE['marker'],
                   LABWC_KEEP_MODE['marker'], DESKTOP_MODE['marker'], TRACE_MARKER):
        require(not (metadata['session'] / marker).exists(),
                'This session already has a pvr-t8011 attempt; no retry')
    if MODE['job']:
        require(sha(JOB_SCRIPT) == JOB_SCRIPT_SHA, 'pvr-compute-job.py differs from the reviewed one')
    if MODE['render']:
        for name, digest in MESA_FILES.items():
            require(sha(MESA_BUILD / name) == digest, name + ' differs from the reviewed build')
    if MODE['gl'] or MODE['labwc']:
        for name, digest in ZINK_FILES.items():
            require(sha(ZINK_BUILD / name) == digest, name + ' differs from the reviewed build')
    return metadata


def state(connection):
    code, output = connection.run('python3 -c ' + shlex.quote(STATE), timeout=15)
    require(code == 0, 'Device state read failed')
    return json.loads(output)


def genpd_off(value):
    domains = dict(value.get('genpd') or [])
    return domains.get('gfx', '').startswith('off') and domains.get('dwi', '').startswith('off')


def gfx_off_dwi_on(value):
    """GPU off: GFX off, DWI still on (j208-gpu-dt holds it until reboot)."""
    domains = dict(value.get('genpd') or [])
    return domains.get('gfx', '').startswith('off') and domains.get('dwi') == 'on'


def validate_before(value):
    require(value['debugfs'], 'pm_genpd debugfs is not available')
    require(not any(value['modules'][m] for m in
                    ('j208_gpu_dt', 'j208_rtkit', 'j208_powervr', 'powervr')),
            'A GPU module is already loaded')
    require(not (value['dt_gpu'] or value['dt_mbox'] or value['gpu_dev'] or value['mbox_dev']),
            'A GPU node or device exists already')
    require(value['render'] == [], 'A render node exists already')
    require(genpd_off(value), 'GFX or DWI is not off: %r' % value.get('genpd'))


def claim_attempt(session, metadata):
    payload = dict(
        boot_id=metadata['boot_id'], image_sha256=metadata['image_sha256'],
        dtb_sha256=metadata['dtb_sha256'], modules=MODULES,
        firmware_sha256=KFL.FIRMWARE_SHA,
        created_at=datetime.now().astimezone().isoformat(),
        driver_probe=True, jobs=MODE['job'], keep_powered=MODE['keep'],
        render=MODE['render'], mesa=MESA_FILES if MODE['render'] else None,
        zink=ZINK_FILES if MODE['gl'] or MODE['labwc'] else None, labwc=MODE['labwc'])
    marker = session / MODE['marker']
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(payload, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    return marker


class Follower(KFL.Follower):
    def __init__(self, connection, boot, path):
        client = load('j208_pvr_client', RAM / 'ssh-runtime/client.py')
        command = client.build_remote_command(boot, 'python3 -u -c ' + shlex.quote(FOLLOWER))
        self.path = Path(path)
        self.stream = self.path.open('wb', buffering=0)
        self.process = subprocess.Popen(
            connection.command + [command], stdin=subprocess.DEVNULL,
            stdout=self.stream, stderr=subprocess.STDOUT)

    def wait_ready(self, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if FOLLOW_READY in self.path.read_text(errors='replace'):
                return True
            if self.process.poll() is not None:
                return False
            time.sleep(0.2)
        return False


def execute(connection, session, metadata):
    boot = metadata['boot_id']
    steps = []
    result = dict(schema_version=1, boot_id=boot, image_sha256=metadata['image_sha256'],
                  dtb_sha256=metadata['dtb_sha256'], modules=MODULES,
                  firmware_sha256=KFL.FIRMWARE_SHA, steps=steps,
                  started_at=datetime.now().astimezone().isoformat(),
                  automatic_retry=False, jobs=MODE['job'], keep_powered=MODE['keep'],
                  render=MODE['render'], mesa=MESA_FILES if MODE['render'] else None,
                  zink=ZINK_FILES if MODE['gl'] or MODE['labwc'] else None,
                  labwc=MODE['labwc'])
    image = KFL.firmware_image()
    before = KFL.snapshot(connection, boot)
    KFL.validate_before(before)
    result['before'] = before
    code, _ = connection.run('test -d /sys/kernel/debug/pm_genpd || '
                             'mount -t debugfs debugfs /sys/kernel/debug', timeout=10)
    require(code == 0, 'debugfs mount failed')
    start = state(connection)
    validate_before(start)
    result['state_before'] = start

    if MODE['render']:
        # before the follower: a 13 MB upload over Wi-Fi dropped its SSH
        # session (keepalive) in runs/20261109-134941
        code, _ = connection.run('test ! -e %s && mkdir -m 700 %s' % (REMOTE_MESA, REMOTE_MESA),
                                 timeout=10)
        require(code == 0, 'Remote Mesa directory exists or cannot be created')
        for name in MESA_FILES:
            connection.upload(MESA_BUILD / name, REMOTE_MESA + '/' + name)
        code, _ = connection.run('chmod 700 %s/vk-tri %s/vk-compute-test %s/kms-grab' % (
            REMOTE_MESA, REMOTE_MESA, REMOTE_MESA), timeout=10)
        require(code == 0, 'test programs not executable')
    if MODE['gl'] or MODE['labwc']:
        code, _ = connection.run('test ! -e %s && mkdir -m 700 %s' % (REMOTE_ZINK, REMOTE_ZINK),
                                 timeout=10)
        require(code == 0, 'Remote Zink directory exists or cannot be created')
        for name in ZINK_FILES:
            connection.upload(ZINK_BUILD / name, REMOTE_MESA + '/' + name)
        code, _ = connection.run('tar -xzf %s/j208-zink.tar.gz -C %s && rm %s/j208-zink.tar.gz '
                                 '&& chmod 700 %s/gl-probe %s/frame-count' % (
                                     REMOTE_MESA, REMOTE_ZINK, REMOTE_MESA, REMOTE_MESA,
                                     REMOTE_MESA),
                                 timeout=30)
        require(code == 0, 'Zink tree not unpacked')
    kernel_start = float(connection.run('cut -d" " -f1 /proc/uptime', timeout=10)[1])

    follower = Follower(connection, boot, session / MODE['stream'])
    if not follower.wait_ready():
        follower.finish(timeout=1)
        raise ValueError('Kernel log follower did not start; nothing attempted')

    uncertain = False
    crashed = False
    kept_on = False     # GPU held on after jobs: no power-off, no unload
    loaded = []
    old_fw_path = start['fw_path'] or ''

    def step(name, command, timeout=15):
        nonlocal uncertain
        try:
            code, output = connection.run(command, timeout=timeout)
        except Exception as error:
            uncertain = True
            steps.append(dict(name=name, error=str(error)[:200]))
            raise
        steps.append(dict(name=name, exit=code, output=output[-600:]))
        return code, output

    try:
        result['attempt_marker'] = claim_attempt(session, metadata).name
        code, _ = step('mkdir', 'test ! -e %s && mkdir -m 700 -p %s/fw/apple' % (REMOTE, REMOTE))
        require(code == 0, 'Remote directory exists or cannot be created')
        for path in MODULES:
            connection.upload(BUILD / path, REMOTE + '/' + Path(path).name)
        local = session / 'pvr-t8011-gfx-t8011.bin.tmp'
        local.write_bytes(image)
        try:
            connection.upload(local, REMOTE_FIRMWARE)
        finally:
            local.unlink()
        code, _ = step('fw-path', 'printf %%s %s > %s' % (shlex.quote(REMOTE + '/fw'), FW_PATH))
        require(code == 0, 'firmware_class.path not set')

        for name, path, args in DEPS + OURS:
            if start['modules'][name]:
                continue
            code, _ = step('insmod-' + name, 'insmod %s/%s %s' % (REMOTE, Path(path).name, args),
                           timeout=90 if name == 'j208_powervr' else 20)
            require(code == 0, 'insmod ' + name + ' failed')
            loaded.append(name)
            if name == 'j208_rtkit':
                bound = state(connection)
                result['state_mailbox'] = bound
                require(bound['mbox_driver'] == 'j208-gfx-mailbox' and
                        bound['gpu_driver'] is None,
                        'Mailbox not bound to the private driver')

        probed = state(connection)
        result['state_probed'] = probed
        result['probe_bound'] = probed['gpu_driver'] == 'powervr'
        result['render_nodes'] = probed['render']

        # Runtime autosuspend: firmware stopped, GFX off (DWI stays held on).
        def wait_suspended():
            value = None
            for _ in range(SUSPEND_WAIT * 2):
                time.sleep(0.5)
                value = state(connection)
                if value['gpu_runtime'] == 'suspended' and gfx_off_dwi_on(value):
                    return value, True
            return value, False

        if result['probe_bound']:
            result['state_suspended'], result['autosuspended'] = wait_suspended()
        if MODE['job'] and result.get('autosuspended') and probed['render']:
            remote_job = REMOTE + '/' + JOB_SCRIPT.name
            connection.upload(JOB_SCRIPT, remote_job)
            if MODE['keep']:
                # resumes the GPU (the firmware boots) and keeps it on
                kept_on = True
                code, _ = step('power-on', 'echo on > %s/power/control' % GPU_DEV)
                require(code == 0, 'power/control not set')
                held = state(connection)
                result['state_held'] = held
                require(held['gpu_runtime'] == 'active', 'GPU did not resume: %r'
                        % held['gpu_runtime'])
            code, output = step('compute-job', 'python3 %s /dev/dri/%s' % (
                remote_job, probed['render'][0]), timeout=JOB_TIMEOUT)
            events = []
            for line in output.splitlines():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
            result['job_events'] = events
            summary = events[-1] if events and events[-1].get('event') == 'summary' else {}
            time.sleep(1)       # the follower's last lines
            log = (session / MODE['stream']).read_text(errors='replace')
            crashed = CRASHED in log
            result['fw_crashed'] = crashed
            result['job_timed_out'] = TIMED_OUT in log
            result['job_ok'] = (code == 0 and summary.get('ok') is True and not crashed
                                and not result['job_timed_out'])
            if crashed:
                result['state_after_crash'] = state(connection)
                raise RuntimeError('GFX firmware crashed: modules left loaded, reboot')
            if kept_on:
                result['state_after_job'] = state(connection)
                raise StopIteration
            result['state_after_job'], result['autosuspended_after_job'] = wait_suspended()
            if MODE['render'] and result['job_ok'] and result['autosuspended_after_job']:
                try:
                    render(connection, session, step, result)
                    loader = labwc_loader(result) if MODE['labwc'] else None
                    if loader is not None:
                        desktop_on_gpu(connection, session, step, result, probed['render'][0],
                                       loader, keep=MODE['keep_desktop'],
                                       bench=MODE['bench'])
                finally:
                    crashed = bool(result.get('fw_crashed'))  # then no unload
                if result.get('desktop_kept'):
                    kept_on = True      # the GPU desktop runs on: no unload
                    raise StopIteration
                result['state_after_render'], result['autosuspended_after_render'] = \
                    wait_suspended()

        for name, _path, _args in reversed(OURS):
            if name not in loaded:
                continue
            code, _ = step('rmmod-' + name, 'rmmod ' + name, timeout=30)
            require(code == 0, 'rmmod ' + name + ' failed')
            loaded.remove(name)
        end = state(connection)
        result['state_unloaded'] = end
        require(not (end['dt_gpu'] or end['dt_mbox'] or end['gpu_dev'] or end['mbox_dev']),
                'GPU nodes or devices remain')
        require(gfx_off_dwi_on(end), 'GFX left on or DWI not held: %r' % end.get('genpd'))
        result['domains_off_after'] = True     # GFX off; DWI on until reboot
    except StopIteration:
        pass                # --keep-powered: done, the GPU stays on
    except Exception as error:
        result['error'] = type(error).__name__ + ': ' + str(error)[:300]

    if not uncertain and not crashed and not kept_on:
        # after an error: the same unload, in order, once
        for name, _path, _args in reversed(OURS):
            if name not in loaded:
                continue
            try:
                code, _ = step('rmmod-' + name, 'rmmod ' + name, timeout=30)
            except Exception as error:
                result['cleanup_error'] = str(error)[:300]
                break
            if code:
                break
            loaded.remove(name)
    if not uncertain and not crashed and not kept_on:
        try:
            # an empty write is not delivered to the parameter; echo writes a newline
            connection.run('echo %s > %s' % (shlex.quote(old_fw_path), FW_PATH), timeout=10)
            for name in ('drm_gpuvm', 'drm_exec', 'gpu_sched'):
                if name in loaded:
                    code, _ = connection.run('rmmod ' + name, timeout=15)
                    if code == 0:
                        loaded.remove(name)
            code, _ = connection.run('rm -rf %s %s %s' % (REMOTE, REMOTE_MESA, REMOTE_ZINK),
                                     timeout=10)
            result['ram_files_removed'] = code == 0
        except Exception as error:
            uncertain = True
            result['cleanup_error'] = str(error)[:300]
    try:
        connection.run('echo %s > /dev/kmsg' % shlex.quote(DONE), timeout=5)
    except Exception:
        pass
    streamed = follower.finish(timeout=10)
    try:
        code, output = connection.run(
            'dmesg | awk -F"[][]" %s | grep -E %s | grep -v %s | tail -300' % (
                shlex.quote('$2+0 >= %.6f' % kernel_start),
                shlex.quote('j208|powervr|rtkit|genpd|BUG:|Oops|SError|Unable to handle|J208_PVR'),
                shlex.quote('ep 0x20: 0x42000000000000')), timeout=20)
        result['dmesg'] = output.splitlines()
    except Exception as error:
        result['dmesg_error'] = str(error)[:200]
    result['stream_tail'] = streamed.splitlines()[-80:]
    try:
        after = KFL.snapshot(connection, boot)
        result['faults_after'] = after.get('faults')
        require(after.get('faults') == before.get('faults'), 'New kernel fault signature')
        require(after.get('desktop') is True, 'Desktop stopped during test')
    except Exception as error:
        uncertain = True
        result['after_error'] = type(error).__name__ + ': ' + str(error)[:300]

    result['modules_left'] = loaded
    result['uncertain'] = uncertain
    if uncertain:
        outcome = 'uncertain'
    elif crashed:
        outcome = 'firmware-crashed-left-loaded'
    elif kept_on and result.get('desktop_kept'):
        outcome = 'labwc-gpu-kept'
    elif kept_on and result.get('error'):
        outcome = 'stopped-left-powered'
    elif kept_on:
        outcome = ('compute-jobs-done-left-powered' if result.get('job_ok')
                   else 'compute-job-failed-left-powered')
    elif result.get('error'):
        outcome = 'stopped'
    elif not result.get('probe_bound'):
        outcome = 'probe-failed-unloaded'
    elif not result.get('autosuspended'):
        outcome = 'probed-no-autosuspend-unloaded'
    elif not MODE['job']:
        outcome = 'probed-suspended-unloaded'
    elif not result.get('job_ok'):
        outcome = 'compute-job-failed-unloaded'
    elif not result.get('autosuspended_after_job'):
        outcome = 'compute-jobs-done-no-autosuspend-unloaded'
    elif not MODE['render']:
        outcome = 'compute-jobs-done-unloaded'
    elif MODE['labwc'] and result.get('desktop_restored') is False:
        outcome = 'desktop-not-restored-unloaded'
    elif MODE['labwc'] and result.get('labwc_gpu_ok') \
            and result.get('autosuspended_after_render'):
        outcome = 'labwc-gpu-done-unloaded'
    elif MODE['labwc'] and 'labwc_gpu_ok' in result:
        outcome = ('labwc-gpu-failed-unloaded' if result.get('autosuspended_after_render')
                   else 'labwc-gpu-failed-no-autosuspend-unloaded')
    elif MODE['gl'] and result.get('render_cases') and not result.get('render_stopped') \
            and result.get('autosuspended_after_render'):
        outcome = 'gl-done-unloaded'       # every case ran; render_ok: all passed
    elif MODE['suite'] and result.get('render_cases') and not result.get('render_stopped') \
            and result.get('autosuspended_after_render'):
        outcome = 'suite-done-unloaded'    # every case ran; render_ok: all passed
    elif not result.get('render_ok'):
        outcome = 'render-failed-unloaded'
    elif not result.get('autosuspended_after_render'):
        outcome = 'render-done-no-autosuspend-unloaded'
    else:
        outcome = 'render-done-unloaded'
    result['outcome'] = outcome
    result['completed'] = (outcome in ('probed-suspended-unloaded',
                                       'compute-jobs-done-unloaded',
                                       'render-done-unloaded',
                                       'suite-done-unloaded',
                                       'gl-done-unloaded',
                                       'labwc-gpu-done-unloaded') and not loaded
                           or outcome in ('compute-jobs-done-left-powered', 'labwc-gpu-kept'))
    result['finished_at'] = datetime.now().astimezone().isoformat()
    (session / MODE['result']).write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: result.get(k) for k in
                      ('outcome', 'probe_bound', 'render_nodes', 'autosuspended', 'job_ok',
                       'autosuspended_after_job', 'render_ok', 'render_passed',
                       'render_failed', 'render_stopped',
                       'autosuspended_after_render', 'labwc_gpu_ok',
                       'desktop_restored', 'desktop_kept', 'fw_crashed', 'job_timed_out',
                       'domains_off_after', 'error', 'uncertain',
                       'modules_left')}, indent=2))
    if result.get('job_events'):
        print(json.dumps(result['job_events'], indent=1))
    if result.get('labwc_gpu'):
        gpu = result['labwc_gpu']
        print(json.dumps(dict(
            {k: gpu.get(k) for k in ('loader_override', 'came_up', 'on_zink', 'renders_traced',
                                     'map_failures', 'map_reuses', 'gpu_runtime',
                                     'stop_pixman', 'stop_gpu', 'firefox_restarted', 'error',
                                     'bench_pixman', 'bench_gpu', 'log')},
            readback={k[9:]: {t: v.get(t) for t in ('tool', 'width', 'height', 'colours')}
                      for k, v in gpu.items() if k.startswith('readback_')}), indent=1))
    print('details:', session / MODE['result'], 'stream:', session / MODE['stream'])
    return 0 if result['completed'] else 1


def render(connection, session, step, result):
    """Mesa test programs: RENDER_CASES (--render) or GL_CASES (--gl) up to the
    first failure, or SUITE_CASES (--suite) up to the first firmware crash or
    job timeout."""
    cases = result['render_cases'] = []
    case_list = (SUITE_CASES if MODE['suite'] else GL_CASES if MODE['gl']
                 else LABWC_CASES if MODE['labwc'] else RENDER_CASES)
    for index, (name, program, env, args) in enumerate(case_list):
        image = '%s/%s.ppm' % (REMOTE_MESA, name) if index == 0 else ''
        code, output = step('render-' + name, 'cd %s && env %s %s timeout -k 5 %d ./%s %s' % (
            REMOTE_MESA, ZINK_ENV if program == 'gl-probe' else VK_ENV, env,
            RENDER_TIMEOUT - 10, program, args or image),
            timeout=RENDER_TIMEOUT)
        lines = output.strip().splitlines()
        # vk-tri prints PASS (TRI_DEPTH=loadfirst: "PASS (...)"), vk-compute-test
        # "NAME: N invocations: 0 wrong: PASS"; Mesa warnings may follow (stderr)
        passed = code == 0 and any(l == 'PASS' or l.startswith('PASS (') or
                                   l.endswith(': PASS') for l in lines) and \
            not any(MAP_FAILED in l for l in lines)
        time.sleep(1)
        # from the kernel log itself: the follower can lose its SSH session
        _, log = connection.run('dmesg | grep -E %s' % shlex.quote(
            '%s|%s' % (CRASHED, TIMED_OUT)), timeout=15)
        crashed = CRASHED in log or TIMED_OUT in log
        cases.append(dict(name=name, program=program, env=env, args=args, exit=code,
                          passed=passed, output=lines[-12:]))
        if image and passed:
            code, encoded = connection.run('base64 -w0 ' + image, timeout=15)
            if code == 0:
                (session / ('pvr-t8011-' + name + '.ppm')).write_bytes(
                    base64.b64decode(encoded.strip()))
        if crashed:
            result['fw_crashed'] = CRASHED in log
            result['job_timed_out'] = TIMED_OUT in log
            if result['fw_crashed']:
                result['state_after_crash'] = state(connection)
                raise RuntimeError('GFX firmware crashed in ' + name +
                                   ': modules left loaded, reboot')
        if crashed or (not passed and not MODE['suite'] and not MODE['gl']
                       and not MODE['labwc']):
            result['render_stopped'] = name
            break
    if MODE['gl'] and cases and cases[-1]['program'] == 'gl-probe' and not cases[-1]['passed'] \
            and not crashed:
        gl_diagnostics(connection, session, step, result)
    result['render_ok'] = len(cases) == len(case_list) and all(c['passed'] for c in cases)
    result['render_passed'] = sum(c['passed'] for c in cases)
    result['render_failed'] = [c['name'] for c in cases if not c['passed']]


def gl_diagnostics(connection, session, step, result):
    """GL_DIAG, each output kept whole; stops at a firmware crash or job timeout."""
    diag = result['gl_diag'] = []
    for name, program, env, args in GL_DIAG:
        out = '%s/diag-%s.txt' % (REMOTE_MESA, name)
        code, _ = step('diag-' + name, 'cd %s && env %s %s timeout -k 5 %d ./%s %s > %s 2>&1' % (
            REMOTE_MESA, ZINK_ENV if program == 'gl-probe' else VK_ENV, env,
            RENDER_TIMEOUT - 10, program, args, out), timeout=RENDER_TIMEOUT)
        entry = dict(name=name, program=program, env=env, exit=code)
        fetched, encoded = connection.run('gzip -c %s | base64 -w0' % out, timeout=60)
        if fetched == 0:
            text = gzip.decompress(base64.b64decode(encoded.strip()))
            (session / ('pvr-t8011-gl-diag-%s.txt' % name)).write_bytes(text)
            lines = text.decode(errors='replace').strip().splitlines()
            entry.update(bytes=len(text), tail=lines[-8:])
        diag.append(entry)
        time.sleep(1)
        _, log = connection.run('dmesg | grep -E %s' % shlex.quote(
            '%s|%s' % (CRASHED, TIMED_OUT)), timeout=15)
        if CRASHED in log or TIMED_OUT in log:
            result['fw_crashed'] = CRASHED in log
            result['job_timed_out'] = TIMED_OUT in log
            entry['stopped'] = True
            if result['fw_crashed']:
                result['state_after_crash'] = state(connection)
                raise RuntimeError('GFX firmware crashed in diagnostic ' + name +
                                   ': modules left loaded, reboot')
            break


# Read-only: the desktop's processes, and Labwc's command line and environment.
DESKTOP = r"""import json,os,pathlib
P=pathlib.Path
procs=[]
for d in P('/proc').iterdir():
    if not d.name.isdigit(): continue
    try:
        stat=(d/'stat').read_text(); cmd=(d/'cmdline').read_bytes().replace(b'\0',b' ').decode(errors='replace')
    except OSError: continue
    comm=stat.split('(',1)[1].rsplit(')',1)[0]
    if stat.rsplit(')',1)[1].split()[0]!='Z': procs.append((int(d.name),comm,cmd))
def pids(name=None,full=None):
    return sorted(p for p,c,a in procs if (name is None or c==name) and (full is None or full in a))
labwc=pids('labwc')
try: display=P('/run/wayland-display').read_text().strip()
except OSError: display=''
v=dict(labwc=labwc,foot=pids('foot'),qs=pids('qs'),firefox=pids('firefox-esr'),
       touch=pids(full='/run/j208-usable/session-touch.py'),wayland_display=display,
       cards={c.name:os.path.basename(os.readlink(c/'device/driver')) if (c/'device/driver').exists() else None
              for c in P('/sys/class/drm').glob('card[0-9]')})
if len(labwc)==1:
    pid=labwc[0]
    raw=lambda n:P('/proc/%d/%s'%(pid,n)).read_bytes().split(b'\0')
    v['cmdline']=[x.decode() for x in raw('cmdline') if x]
    v['environ']=dict(x.decode().split('=',1) for x in raw('environ') if b'=' in x)
    v['stdin']=os.readlink('/proc/%d/fd/0'%pid)
    fds=[]
    for f in P('/proc/%d/fd'%pid).iterdir():
        try: t=os.readlink(f)
        except OSError: continue
        if t.startswith('/dev/dri/'): fds.append(t)
    v['dri_fds']=sorted(set(fds))
print(json.dumps(v))
"""

# Labwc on tty1 in a session of its own, as peripherals/init starts it
# (setsid -c labwc ... </dev/tty1), detached from the SSH session. With
# "rotate", its output goes through a pipe to a logger of its own that keeps
# the log under that many bytes (log, then log.1): the GPU session traces
# every render, and /run is RAM.
LABWC_START = r"""import fcntl,json,os,sys,termios
a=json.loads(sys.argv[1])
r,w=os.pipe2(os.O_CLOEXEC)     # closed by a successful exec, else the error
if a.get('rotate'):
    lr,lw=os.pipe()
    if os.fork()==0:
        try:
            os.setsid(); os.close(lw); os.close(r); os.close(w)
            null=os.open('/dev/null',os.O_RDWR)
            for fd in (0,1,2): os.dup2(null,fd)
            os.closerange(3,lr); os.closerange(lr+1,4096); os.chdir('/')
            out=os.open(a['log'],os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
            size=os.fstat(out).st_size
            while True:
                b=os.read(lr,65536)
                if not b: break
                if size+len(b)>a['rotate']:
                    os.close(out); os.replace(a['log'],a['log']+'.1')
                    out=os.open(a['log'],os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600); size=0
                os.write(out,b); size+=len(b)
        finally: os._exit(0)
    os.close(lr)
pid=os.fork()
if pid==0:
    try:
        os.close(r); os.setsid()
        tty=os.open('/dev/tty1',os.O_RDWR|os.O_NOCTTY)
        try: fcntl.ioctl(tty,termios.TIOCSCTTY,0)
        except OSError: fcntl.ioctl(tty,termios.TIOCSCTTY,1)
        log=lw if a.get('rotate') else os.open(a['log'],os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
        os.dup2(tty,0); os.dup2(log,1); os.dup2(log,2)
        os.closerange(3,w); os.closerange(w+1,4096)
        os.chdir('/')
        os.execvpe(a['argv'][0],a['argv'],a['env'])
    except BaseException as e:
        os.write(w,repr(e).encode()); os._exit(127)
os.close(w)
if a.get('rotate'): os.close(lw)
error=os.read(r,4096).decode()
print(json.dumps(dict(pid=pid,error=error or None)))
sys.exit(1 if error else 0)
"""

# A desktop client detached from the SSH session (the benchmark's foot).
SPAWN = r"""import json,os,sys
a=json.loads(sys.argv[1])
pid=os.fork()
if pid==0:
    os.setsid(); null=os.open('/dev/null',os.O_RDWR)
    for fd in (0,1,2): os.dup2(null,fd)
    os.closerange(3,4096); os.chdir('/')
    try: os.execvpe(a['argv'][0],a['argv'],a['env'])
    finally: os._exit(127)
print(pid)
"""

# SIGTERM, then SIGKILL after the wait; a zombie counts as exited.
LABWC_STOP = r"""import json,os,signal,sys,time
pid,wait=int(sys.argv[1]),float(sys.argv[2])
def live():
    try:
        stat=open('/proc/%d/stat'%pid).read()
    except OSError: return False
    return stat.split('(',1)[1].split(')',1)[0]=='labwc' and stat.rsplit(')',1)[1].split()[0]!='Z'
def until(t):
    end=time.monotonic()+t
    while live() and time.monotonic()<end: time.sleep(0.2)
    return not live()
os.kill(pid,signal.SIGTERM)
done=until(wait); killed=False
if not done:
    os.kill(pid,signal.SIGKILL); killed=True; done=until(5)
print(json.dumps(dict(exited=done,killed=killed)))
"""

# Read-only: CPU time of the processes named, and of the whole system, over
# a period.
CPU = r"""import json,os,sys,time
pids=[int(p) for p in sys.argv[2].split(',')]
def ticks(pid):
    try: f=open('/proc/%d/stat'%pid).read().rsplit(')',1)[1].split()
    except OSError: return None
    return int(f[11])+int(f[12])
def system():
    f=[int(x) for x in open('/proc/stat').readline().split()[1:]]
    return sum(f),f[3]+f[4]
p0=[ticks(p) for p in pids]; s0=system(); t0=time.monotonic()
time.sleep(float(sys.argv[1]))
p1=[ticks(p) for p in pids]; s1=system(); t1=time.monotonic()
print(json.dumps(dict(seconds=round(t1-t0,2),hz=os.sysconf('SC_CLK_TCK'),cpus=os.cpu_count(),
    ticks={str(p):(b-a if a is not None and b is not None else None) for p,a,b in zip(pids,p0,p1)},
    system_busy=round(1-(s1[1]-s0[1])/max(1,s1[0]-s0[0]),4))))
"""
BENCH_SECONDS = 30          # per renderer, with a scrolling foot
BENCH_WORKLOAD = ['foot', '--app-id=j208-bench', 'sh', '-c', 'while :; do ls -lR /usr/share; done']
LABWC_LOG_CAP = 4 << 20     # the kept GPU session's log: two files of at most 4 MB


def ppm_summary(data):
    """Size and the number of distinct colours (every 7th pixel) of a binary PPM."""
    fields, pos = [], 2
    require(data[:2] == b'P6', 'not a binary PPM')
    while len(fields) < 3:
        while data[pos:pos + 1].isspace():
            pos += 1
        end = pos
        while not data[end:end + 1].isspace():
            end += 1
        fields.append(int(data[pos:end]))
        pos = end
    pixels = data[pos + 1:]
    width, height = fields[0], fields[1]
    colours = {pixels[i:i + 3] for i in range(0, min(len(pixels), width * height * 3) - 2, 21)}
    return dict(width=width, height=height, colours=len(colours))


def frame_stats(lines):
    """G9P_TRACE lines -> frame intervals. Renders less than 4 ms apart are one
    frame (as the A10's start-labwc.sh counts them). On simpledrm each render
    is a whole frame and Labwc runs unthrottled, so this counts about 10 %
    low; bench()'s frames_client (frame-count) counts each frame."""
    import re
    times = []
    for line in lines:
        m = re.search(r'g9p: trace \d+: .* at ([0-9.]+) ms', line)
        if m:
            times.append(float(m.group(1)))
    starts = []
    for t in times:
        if not starts or t - last >= 4:
            starts.append(t)
        last = t
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    if not gaps:
        return dict(renders=len(times), frames=len(starts))
    return dict(renders=len(times), frames=len(starts), intervals=len(gaps),
                mean_ms=round(sum(gaps) / len(gaps), 2),
                under_20_ms=sum(g < 20 for g in gaps),
                from_20_to_35_ms=sum(20 <= g < 35 for g in gaps),
                over_35_ms=sum(g >= 35 for g in gaps),
                fps=round(1000 * len(gaps) / (starts[-1] - starts[0]), 1))


def labwc_loader(result):
    """MESA_LOADER_DRIVER_OVERRIDE for Labwc ('' for none): that of the first
    scanout case that passed, after tri-64 and gl-probe; None: no Labwc."""
    passed = {c['name']: c['passed'] for c in result.get('render_cases', ())}
    if not (passed.get(RENDER_CASES[0][0]) and passed.get('gl-probe')):
        return None
    for name, override in LABWC_SCANOUT:
        if passed.get(name):
            return override
    return None


class Desktop:
    """The running desktop over SSH: its state, screen readbacks, Labwc
    stopped and started, a benchmark. Records into the dict `gpu`."""

    def __init__(self, connection, session, step, gpu, result):
        self.connection, self.session, self.step = connection, session, step
        self.gpu, self.result = gpu, result

    def state(self):
        code, output = self.connection.run('python3 -c ' + shlex.quote(DESKTOP), timeout=15)
        require(code == 0, 'Desktop state read failed')
        return json.loads(output)

    def readback(self, name):
        """What the display shows: kms-grab (simpledrm's scanout), else grim."""
        shot = {}
        for tool, command in (
                ('kms-grab', '%s/kms-grab %s/%s.ppm 2' % (REMOTE_MESA, REMOTE_LABWC, name)),
                ('grim', 'XDG_RUNTIME_DIR=/run/user/0 WAYLAND_DISPLAY=$(cat /run/wayland-display) '
                         'grim -s 0.5 -t ppm %s/%s.ppm' % (REMOTE_LABWC, name))):
            code, output = self.step('%s-%s' % (tool, name), command, timeout=30)
            shot[tool] = dict(exit=code, output=output.strip().splitlines()[-3:])
            if code:
                continue
            code, encoded = self.connection.run('gzip -c %s/%s.ppm | base64 -w0 && rm %s/%s.ppm' % (
                REMOTE_LABWC, name, REMOTE_LABWC, name), timeout=60)
            if code == 0:
                data = gzip.decompress(base64.b64decode(encoded.strip()))
                (self.session / ('pvr-t8011-labwc-%s.ppm' % name)).write_bytes(data)
                shot.update(tool=tool, **ppm_summary(data))
                break
        self.gpu['readback_' + name] = shot
        return shot

    def fault(self):
        _, log = self.connection.run('dmesg | grep -E %s' % shlex.quote(
            '%s|%s' % (CRASHED, TIMED_OUT)), timeout=15)
        self.result['fw_crashed'] = CRASHED in log
        self.result['job_timed_out'] = TIMED_OUT in log
        return CRASHED in log or TIMED_OUT in log

    def start(self, argv, env, log, rotate=0):
        code, output = self.step('start-' + log.rsplit('/', 1)[1], 'python3 -c %s %s' % (
            shlex.quote(LABWC_START), shlex.quote(json.dumps(dict(
                argv=argv, env=env, log=log, rotate=rotate)))))
        require(code == 0, 'Labwc did not start: ' + output.strip()[-200:])

    def stop(self, pid, name):
        code, output = self.step('stop-' + name, 'python3 -c %s %d %d' % (
            shlex.quote(LABWC_STOP), pid, LABWC_STOP_WAIT), timeout=LABWC_STOP_WAIT + 15)
        require(code == 0, 'Labwc stop failed')
        stopped = self.gpu['stop_' + name] = json.loads(output)
        require(stopped['exited'], 'Labwc did not exit')
        code, _ = self.step('clear-display-' + name, 'rm -f /run/wayland-display')
        require(code == 0, '/run/wayland-display not removed')

    def wait_up(self, old_foot):
        """Labwc running, its autostart done (/run/wayland-display) and a new foot."""
        value = None
        for _ in range(LABWC_UP_WAIT):
            time.sleep(1)
            value = self.state()
            if len(value['labwc']) == 1 and value['wayland_display'] and \
                    set(value['foot']) - set(old_foot):
                return value, True
            if not value['labwc']:
                break
        return value, False

    def firefox(self):
        code, _ = self.step('firefox', 'cd / && env XDG_RUNTIME_DIR=/run/user/0 '
                            'WAYLAND_DISPLAY=$(cat /run/wayland-display) '
                            'python3 /run/j208-usable/desktop-apps.py browser', timeout=30)
        self.gpu['firefox_restarted'] = code == 0

    def bench(self, name, client_env, log=None):
        """A scrolling foot for BENCH_SECONDS: CPU time of Labwc and foot, and
        over the same period Labwc's output frames as frame-count sees them
        (either renderer); with the GPU session's log, also its traced
        renders."""
        now = self.state()
        require(len(now['labwc']) == 1 and now['wayland_display'], 'No desktop to benchmark')
        env = dict(client_env, WAYLAND_DISPLAY=now['wayland_display'])
        code, output = self.step('bench-start-' + name, 'python3 -c %s %s' % (
            shlex.quote(SPAWN), shlex.quote(json.dumps(dict(argv=BENCH_WORKLOAD, env=env)))))
        require(code == 0, 'Benchmark foot not started')
        foot = int(output.strip())
        try:
            time.sleep(3)
            if log:
                lines0 = int(self.connection.run('wc -l < ' + log, timeout=10)[1].strip() or 0)
            frames = '%s/frame-count-%s.json' % (REMOTE_MESA, name)
            code, output = self.step('bench-' + name, (
                'cd / && (env -i %s %s/frame-count %d > %s 2>&1 &); python3 -c %s %d %s' % (
                    ' '.join(shlex.quote('%s=%s' % kv) for kv in sorted(env.items())),
                    REMOTE_MESA, BENCH_SECONDS, frames, shlex.quote(CPU), BENCH_SECONDS,
                    '%d,%d' % (now['labwc'][0], foot))),
                timeout=BENCH_SECONDS + 20)
            require(code == 0, 'Benchmark failed')
            cpu = json.loads(output)
            labwc, client = (cpu['ticks'][str(now['labwc'][0])], cpu['ticks'][str(foot)])
            bench = dict(seconds=cpu['seconds'], system_busy=cpu['system_busy'], cpus=cpu['cpus'],
                         labwc_ticks=labwc, foot_ticks=client, hz=cpu['hz'],
                         labwc_core_share=(round(labwc / cpu['hz'] / cpu['seconds'], 3)
                                           if labwc is not None else None),
                         foot_alive=client is not None)
            time.sleep(2)       # frame-count ends with the CPU period
            code, output = self.connection.run('cat %s && rm %s' % (frames, frames), timeout=10)
            try:
                bench['frames_client'] = json.loads(output.strip().splitlines()[-1])
            except (ValueError, IndexError):
                bench['frames_client'] = dict(error=output.strip()[-300:])
            presented = bench['frames_client'].get('presented')
            if presented and labwc is not None:
                bench['labwc_cpu_ms_per_frame'] = round(
                    labwc * 1000 / cpu['hz'] / presented, 3)
            if log:
                code, output = self.connection.run(
                    'tail -n +%d %s | grep "g9p: trace"' % (lines0 + 1, log), timeout=30)
                bench['frames'] = frame_stats(output.splitlines())
        finally:
            self.connection.run('kill %d' % foot, timeout=10)
        self.gpu['bench_' + name] = bench
        return bench

    def restore(self, before, restore_env, old_foot):
        """Labwc as it ran before (pixman), and Firefox if it was open."""
        now = self.state()
        if len(now['labwc']) == 1 and now.get('environ', {}).get('WLR_RENDERER') == 'pixman' \
                and now['wayland_display'] and now['labwc'] == before['labwc']:
            return True         # never stopped
        for pid in now['labwc']:
            self.stop(pid, 'gpu')
        self.start(LABWC_CMDLINE, restore_env, '/run/labwc.log')
        self.gpu['desktop_after'], restored = self.wait_up(now['foot'] + list(old_foot))
        if restored and before['firefox']:
            self.firefox()
        if restored:
            time.sleep(3)
            self.gpu['desktop_restored_state'] = self.state()
        return restored


def gpu_session_env(before, render_node, loader):
    """Labwc's environment for the GPU session, and for its clients (as before)."""
    kms = sorted(c for c, driver in before['cards'].items() if driver == 'simple-framebuffer')
    require(len(kms) == 1, 'No single simpledrm card: %r' % before['cards'])
    restore_env = {k: v for k, v in before['environ'].items() if k != 'WAYLAND_DISPLAY'}
    gpu_env = dict(restore_env, WLR_RENDERER='gles2',
                   WLR_RENDER_DRM_DEVICE='/dev/dri/' + render_node,
                   WLR_DRM_DEVICES='/dev/dri/' + kms[0], G9P_TRACE='1')
    gpu_env.update(item.split('=', 1) for item in ZINK_ENV.split())
    gpu_env.pop('LIBGL_ALWAYS_SOFTWARE', None)
    gpu_env.pop('MESA_LOADER_DRIVER_OVERRIDE')      # Mesa's own choice: kmsro
    if loader:
        gpu_env['MESA_LOADER_DRIVER_OVERRIDE'] = loader
    client_env = dict(restore_env, LIBGL_ALWAYS_SOFTWARE='1')
    return restore_env, gpu_env, client_env


def desktop_on_gpu(connection, session, step, result, render_node, loader, keep=False,
                   bench=True):
    """Labwc on the GPU for LABWC_SECONDS, then back as it ran before. With
    keep (--labwc-keep, --desktop): when the GPU session is good it stays
    (result['desktop_kept']); with bench too (--labwc-keep), a benchmark on
    pixman first and on the GPU after."""
    gpu = result['labwc_gpu'] = dict(loader_override=loader)
    result['labwc_gpu_ok'] = False
    desk = Desktop(connection, session, step, gpu, result)

    before = gpu['desktop_before'] = desk.state()
    require(len(before['labwc']) == 1, 'Not exactly one Labwc: %r' % before['labwc'])
    require(before.get('cmdline') == LABWC_CMDLINE, 'Labwc command line differs: %r'
            % before.get('cmdline'))
    require(before.get('stdin') == '/dev/tty1', 'Labwc is not on tty1')
    require(before['environ'].get('WLR_RENDERER') == 'pixman', 'Labwc is not on pixman')
    require(not before.get('dri_fds') or all(f.startswith('/dev/dri/card') for f in
                                             before['dri_fds']), 'Labwc has a render node open')
    restore_env, gpu_env, client_env = gpu_session_env(before, render_node, loader)
    code, _ = step('labwc-config', 'mkdir -m 700 %s && cp -a /etc/labwc %s/config' % (
        REMOTE_LABWC, REMOTE_LABWC))
    require(code == 0, 'GPU session config not prepared')
    # Labwc sets its config's environment file on itself before it creates
    # the renderer: Omarchy's LIBGL_ALWAYS_SOFTWARE=1 would put the GPU
    # session's EGL on software rendering. The copy goes without it.
    code, _ = step('labwc-environment', 'f=%s/config/environment; '
                   '{ test ! -e $f || sed -i "/^LIBGL_ALWAYS_SOFTWARE=/d" $f; } && '
                   '! grep -qs LIBGL_ALWAYS_SOFTWARE $f' % REMOTE_LABWC)
    require(code == 0, 'LIBGL_ALWAYS_SOFTWARE not removed from the GPU session environment')
    # the clients start as before: the same autostart, without the Zink
    # environment and with software GL as the environment file set it
    autostart = '#!/bin/sh\nexec env %s LIBGL_ALWAYS_SOFTWARE=1 sh /etc/labwc/autostart\n' % (
        ' '.join('-u ' + name for name in LABWC_CLIENT_UNSET))
    code, _ = step('labwc-autostart', 'printf %%s %s > %s/config/autostart' % (
        shlex.quote(autostart), REMOTE_LABWC))
    require(code == 0, 'GPU session autostart not written')
    desk.readback('pixman-before')
    if keep and bench:
        desk.bench('pixman', client_env)

    gpu['env'] = {k: gpu_env[k] for k in sorted(set(gpu_env) - set(restore_env)
                                                | {'WLR_RENDERER'})}
    gpu_log = REMOTE_LABWC + '/labwc-gpu.log'
    failure = None
    result['desktop_restored'] = False      # from here until Labwc is back on pixman
    try:
        desk.stop(before['labwc'][0], 'pixman')
        try:
            desk.start(['labwc', '-d', '-C', REMOTE_LABWC + '/config'], gpu_env, gpu_log,
                       rotate=LABWC_LOG_CAP if keep else 0)
            gpu['desktop_up'], gpu['came_up'] = desk.wait_up(before['foot'])
            if gpu['came_up']:
                time.sleep(LABWC_SECONDS)
                gpu['desktop_held'] = desk.state()
                gpu['gpu_runtime'] = state(connection)['gpu_runtime']   # recorded only
                desk.readback('gpu')
                if keep and bench:
                    desk.bench('gpu', client_env, gpu_log)
        finally:
            fault = desk.fault()
            code, output = connection.run(
                'grep -c %s %s; grep -c "g9p: trace" %s; grep -c %s %s; grep -E -m 5 %s %s; '
                'grep -E -m 12 %s %s; grep -E -i -m 20 %s %s' % (
                    shlex.quote(MAP_FAILED), gpu_log, gpu_log, shlex.quote(MAP_REUSED), gpu_log,
                    shlex.quote(MAP_DETAIL), gpu_log,
                    shlex.quote('GL (vendor|renderer|version)|EGL (vendor|version)|'
                                'Using .*renderer|gles2|GLES2'), gpu_log,
                    shlex.quote('error|fail|abort|assert|segfault'), gpu_log), timeout=20)
            lines = output.strip().splitlines()
            gpu['map_failures'] = int(lines[0]) if lines and lines[0].isdigit() else None
            gpu['renders_traced'] = (int(lines[1]) if len(lines) > 1 and lines[1].isdigit()
                                     else None)
            gpu['map_reuses'] = int(lines[2]) if len(lines) > 2 and lines[2].isdigit() else None
            gpu['log'] = [l[:200] for l in lines[3:]]
            code, encoded = connection.run('gzip -c %s | base64 -w0' % gpu_log, timeout=60)
            if code == 0:
                (session / 'pvr-t8011-labwc-gpu.log.gz').write_bytes(
                    base64.b64decode(encoded.strip()))
        held = gpu.get('desktop_held') or {}
        gpu['on_zink'] = any('GL renderer' in l and 'zink' in l.lower() for l in gpu['log'])
        # Labwc and its clients up on Zink, frames rendered by the GPU, no
        # crash or timeout, and the scanout is not a blank screen
        result['labwc_gpu_ok'] = bool(
            gpu['came_up'] and len(held.get('labwc', ())) == 1 and held.get('foot') and
            gpu['on_zink'] and (gpu['renders_traced'] or 0) > 0 and not fault and
            gpu['map_failures'] == 0 and
            (gpu.get('readback_gpu', {}).get('colours') or 0) >= 16)
        if keep and result['labwc_gpu_ok']:
            if before['firefox']:
                desk.firefox()
            time.sleep(3)
            kept = gpu['desktop_kept_state'] = desk.state()
            require(len(kept['labwc']) == 1 and kept['foot'] and not desk.fault(),
                    'GPU desktop not running before it is kept')
            result['desktop_kept'] = True
            result['desktop_restored'] = None   # not restored: kept on the GPU
            return
    except Exception as error:
        failure = error
        gpu['error'] = type(error).__name__ + ': ' + str(error)[:300]
    # back to Labwc as it ran before, whatever happened above (an SSH timeout
    # raises again here)
    restored = desk.restore(before, restore_env, before['foot'])
    if restored and 'desktop_after' in gpu:
        desk.readback('pixman-after')
    result['desktop_restored'] = restored
    if result.get('fw_crashed'):
        raise RuntimeError('GFX firmware crashed under the GPU desktop: modules left loaded, reboot')
    if failure is not None:
        raise failure


def main(session, *, confirmation, compute_job=False, keep_powered=False, render=False,
         suite=False, gl=False, labwc=False, labwc_keep=False, desktop=False):
    os.umask(0o077)
    require(sum((render, keep_powered, suite, gl, labwc, labwc_keep, desktop)) <= 1,
            '--render, --suite, --gl, --labwc, --labwc-keep, --desktop and --keep-powered '
            'exclude each other')
    if desktop:
        MODE.update(DESKTOP_MODE, job=True, render=True, labwc=True, keep_desktop=True)
    elif labwc_keep:
        MODE.update(LABWC_KEEP_MODE, job=True, render=True, labwc=True, keep_desktop=True,
                    bench=True)
    elif labwc:
        MODE.update(LABWC_MODE, job=True, render=True, labwc=True)
    elif gl:
        MODE.update(GL_MODE, job=True, render=True, gl=True)
    elif suite:
        MODE.update(SUITE_MODE, job=True, render=True, suite=True)
    elif render:
        MODE.update(RENDER_MODE, job=True, render=True)
    elif keep_powered:
        MODE.update(KEEP_MODE, job=True, keep=True)
    elif compute_job:
        MODE.update(JOB_MODE, job=True)
    require(confirmation == MODE['confirmation'],
            'Exact --confirmation ' + MODE['confirmation'] + ' is required')
    metadata = validate_session(session)
    session = metadata['session']
    client = load('j208_pvr_ssh', RAM / 'ssh-runtime/client.py')
    with client.Session(session) as connection:
        return execute(connection, session, metadata)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True, type=Path)
    parser.add_argument('--confirmation', required=True,
                        help='must be %s (%s with --compute-job, %s with --keep-powered, '
                        '%s with --render, %s with --suite, %s with --gl, %s with --labwc, '
                        '%s with --labwc-keep, %s with --desktop)' % (
                            CONFIRMATION, JOB_MODE['confirmation'], KEEP_MODE['confirmation'],
                            RENDER_MODE['confirmation'], SUITE_MODE['confirmation'],
                            GL_MODE['confirmation'], LABWC_MODE['confirmation'],
                            LABWC_KEEP_MODE['confirmation'], DESKTOP_MODE['confirmation']))
    parser.add_argument('--compute-job', action='store_true',
                        help='also submit terminate-only compute jobs')
    parser.add_argument('--render', action='store_true',
                        help='compute jobs, then vk-tri through Mesa (geometry and fragment)')
    parser.add_argument('--suite', action='store_true',
                        help='--render, then vk-tri variants and vk-compute-test shaders')
    parser.add_argument('--gl', action='store_true',
                        help='compute jobs, one vk-tri, then OpenGL ES through Zink (gl-probe)')
    parser.add_argument('--labwc', action='store_true',
                        help='--gl\'s checks, then the desktop (Labwc) on the GPU for a while, '
                        'then back on pixman')
    parser.add_argument('--labwc-keep', action='store_true',
                        help='--labwc with a benchmark, then the GPU desktop stays for use '
                        '(modules loaded until a reboot; restore-desktop.py for pixman)')
    parser.add_argument('--desktop', action='store_true',
                        help='--labwc-keep without the benchmark (gpu-desktop.py)')
    parser.add_argument('--keep-powered', action='store_true',
                        help='compute jobs with the GPU held on; no unload, reboot after')
    args = parser.parse_args()
    try:
        raise SystemExit(main(args.run, confirmation=args.confirmation,
                              compute_job=args.compute_job,
                              keep_powered=args.keep_powered, render=args.render,
                              suite=args.suite, gl=args.gl, labwc=args.labwc,
                              labwc_keep=args.labwc_keep, desktop=args.desktop))
    except (ValueError, FileExistsError) as error:
        print('pvr-t8011 run stopped: ' + str(error), file=sys.stderr)
        raise SystemExit(2)
