#!/usr/bin/env python3
"""Device side: the smallest compute jobs through the powervr uAPI.

Opens the render node, reads the GPU and heap info, creates a VM, one BO for
the CDM context-state buffer (mapped at the general heap base) and a compute
context with no static state. Then it submits jobs whose CDM control stream
is pvr_apple's terminate-only stream at PVR_APPLE_GPU_DATA (0x200000,
mapped into every userspace VM): no shader, no PDS program of ours. One job
first, then two back to back, then BURST back to back: past the 16 job slots,
the firmware's 128 scheduling entries and the 256 values of the stamp's low
byte. Each job signals its own syncobj; each phase is waited for with a
timeout. Prints one JSON object per line; the last is the summary. A fence
that ends with an error still counts as signalled here: the kernel log
tells a timeout.

Usage: pvr-compute-job.py /dev/dri/renderDNNN
"""
import ctypes
import fcntl
import json
import os
import struct
import sys
import time

GPU_DATA = 0x200000          # PVR_APPLE_GPU_DATA: terminate-only CDM stream at +0
WAIT_S = 10
BURST = 300
BURST_WAIT_S = 30


def ioc(direction, nr, size):
    return (direction << 30) | (size << 16) | (ord('d') << 8) | nr


def iowr(nr, size):
    return ioc(3, nr, size)


def iow(nr, size):
    return ioc(1, nr, size)


PVR = 0x40  # DRM_COMMAND_BASE
DEV_QUERY = iowr(PVR + 0x00, 16)
CREATE_BO = iowr(PVR + 0x01, 24)
CREATE_VM = iowr(PVR + 0x03, 8)
DESTROY_VM = iow(PVR + 0x04, 8)
VM_MAP = iow(PVR + 0x05, 40)
VM_UNMAP = iow(PVR + 0x06, 24)
CREATE_CTX = iowr(PVR + 0x07, 40)
DESTROY_CTX = iow(PVR + 0x08, 8)
SUBMIT = iow(PVR + 0x0d, 16)
SYNCOBJ_CREATE = iowr(0xBF, 8)
SYNCOBJ_DESTROY = iowr(0xC0, 8)
SYNCOBJ_WAIT = iowr(0xC3, 40)
GEM_CLOSE = iow(0x09, 8)

QUERY_GPU_INFO, QUERY_HEAP_INFO = 0, 4
CTX_COMPUTE, JOB_COMPUTE = 1, 2
SYNC_SIGNAL = 1 << 31
WAIT_ALL = 1


def out(**kw):
    print(json.dumps(kw), flush=True)


class Dev:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)

    def ioctl(self, request, buf):
        fcntl.ioctl(self.fd, request, buf, True)
        return buf


def query(dev, kind, payload):
    pbuf = ctypes.create_string_buffer(payload, len(payload))
    args = bytearray(struct.pack('<IIQ', kind, len(payload), ctypes.addressof(pbuf)))
    dev.ioctl(DEV_QUERY, args)
    return pbuf.raw


def heaps(dev):
    probe = query(dev, QUERY_HEAP_INFO, struct.pack('<IIQ', 24, 0, 0))
    count = struct.unpack_from('<II', probe)[1]
    array = ctypes.create_string_buffer(24 * count)
    query(dev, QUERY_HEAP_INFO, struct.pack('<IIQ', 24, count, ctypes.addressof(array)))
    return [struct.unpack_from('<QQII', array.raw, 24 * i) for i in range(count)]


def compute_stream(ctx_state):
    # main stream: length, padding, tpu_border_colour_table, cdm_ctrl_stream_base,
    # cdm_context_state_base_addr, cdm_resume_pds1 (Apple: no optional features)
    body = struct.pack('<QQQI', 0, GPU_DATA, ctx_state, 0)
    return struct.pack('<II', 8 + len(body), 0) + body


def submit(dev, ctx, stream_buf, syncobj):
    sync = ctypes.create_string_buffer(struct.pack('<IIQ', syncobj, SYNC_SIGNAL, 0), 16)
    job = ctypes.create_string_buffer(struct.pack(
        '<IIIIQIIQII', JOB_COMPUTE, ctx, 0, len(stream_buf.raw),
        ctypes.addressof(stream_buf), 16, 1, ctypes.addressof(sync), 0, 0), 48)
    args = bytearray(struct.pack('<IIQ', 48, 1, ctypes.addressof(job)))
    dev.ioctl(SUBMIT, args)


def wait(dev, handles, seconds=WAIT_S):
    arr = ctypes.create_string_buffer(struct.pack('<%dI' % len(handles), *handles))
    deadline = time.clock_gettime_ns(time.CLOCK_MONOTONIC) + seconds * 10**9
    args = bytearray(struct.pack('<QqIIIIQ', ctypes.addressof(arr), deadline,
                                 len(handles), WAIT_ALL, 0, 0, 0))
    start = time.monotonic()
    try:
        dev.ioctl(SYNCOBJ_WAIT, args)
        return dict(ok=True, ms=round((time.monotonic() - start) * 1000, 3))
    except OSError as error:
        return dict(ok=False, errno=error.errno, ms=round((time.monotonic() - start) * 1000, 3))


def main(path):
    summary = dict(ok=False, stage='open')
    dev = Dev(path)
    try:
        summary['stage'] = 'query'
        gpu = query(dev, QUERY_GPU_INFO, bytes(16))
        gpu_id, phantoms = struct.unpack_from('<QI', gpu)
        bvnc = [(gpu_id >> s) & 0xffff for s in (48, 32, 16, 0)]
        out(event='gpu', bvnc=bvnc, phantoms=phantoms)
        heap_list = heaps(dev)
        out(event='heaps', heaps=[[hex(b), hex(s), f, p] for b, s, f, p in heap_list])
        general = heap_list[0][0]

        summary['stage'] = 'vm'
        vm = struct.unpack_from('<I', dev.ioctl(CREATE_VM, bytearray(8)))[0]
        size = os.sysconf('SC_PAGE_SIZE')
        bo = struct.unpack_from('<I', dev.ioctl(
            CREATE_BO, bytearray(struct.pack('<QIIQ', size, 0, 0, 0))), 8)[0]
        dev.ioctl(VM_MAP, bytearray(struct.pack('<IIQIIQQ', vm, 0, general, bo, 0, 0, size)))
        out(event='vm', vm=vm, bo=bo, ctx_state=hex(general), size=size)

        summary['stage'] = 'context'
        args = dev.ioctl(CREATE_CTX, bytearray(struct.pack(
            '<IIiIQIIQ', CTX_COMPUTE, 0, 0, 0, 0, 0, vm, 0)))
        ctx = struct.unpack_from('<I', args, 12)[0]
        out(event='context', ctx=ctx)

        stream = ctypes.create_string_buffer(compute_stream(general), 36)
        syncs = [struct.unpack_from('<I', dev.ioctl(SYNCOBJ_CREATE, bytearray(8)))[0]
                 for _ in range(3 + BURST)]

        summary['stage'] = 'job1'
        submit(dev, ctx, stream, syncs[0])
        first = wait(dev, syncs[:1])
        out(event='job1', **first)
        summary['job1'] = first
        if first['ok']:
            summary['stage'] = 'job2-3'
            submit(dev, ctx, stream, syncs[1])
            submit(dev, ctx, stream, syncs[2])
            more = wait(dev, syncs[1:3])
            out(event='job2-3', **more)
            summary['job2_3'] = more
            if more['ok']:
                summary['stage'] = 'burst'
                for handle in syncs[3:]:
                    submit(dev, ctx, stream, handle)
                burst = wait(dev, syncs[3:], BURST_WAIT_S)
                out(event='burst', jobs=BURST, **burst)
                summary['burst'] = burst
                summary['ok'] = burst['ok']
        summary['stage'] = 'cleanup'
        for handle in syncs:
            dev.ioctl(SYNCOBJ_DESTROY, bytearray(struct.pack('<II', handle, 0)))
        dev.ioctl(DESTROY_CTX, bytearray(struct.pack('<II', ctx, 0)))
        dev.ioctl(VM_UNMAP, bytearray(struct.pack('<IIQQ', vm, 0, general, size)))
        dev.ioctl(GEM_CLOSE, bytearray(struct.pack('<II', bo, 0)))
        dev.ioctl(DESTROY_VM, bytearray(struct.pack('<II', vm, 0)))
        summary['stage'] = 'done'
    except OSError as error:
        summary['errno'] = error.errno
        summary['error'] = str(error)
    finally:
        os.close(dev.fd)
    out(event='summary', **summary)
    return 0 if summary['ok'] and summary['stage'] == 'done' else 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1]))
