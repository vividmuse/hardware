#!/usr/bin/env python3
"""Labwc back on pixman after run-pvr-t8011.py --labwc-keep or --desktop
(gpu-desktop.py), on the same boot.

  python3 restore-desktop.py --run RUN > log 2>&1

Reads the kept run's result for Labwc's command line and environment as they
were before the GPU session, stops the GPU Labwc, and starts Labwc as before,
with Firefox again if it was open. The desktop is down for a moment. The
GPU modules stay loaded (the GPU suspends once Labwc lets go of it); a
reboot unloads them. Once per kept session (its own marker).
"""
import argparse
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
RAM = HERE.parents[2]
MARKER = 'pvr-t8011-desktop-restore-attempted.json'
RESULT = 'pvr-t8011-desktop-restore-result.json'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNNER = load('j208_pvr_runner', HERE / 'run-pvr-t8011.py')
require = RUNNER.require


def main(session):
    os.umask(0o077)
    session = Path(session).resolve()
    results = [session / mode['result'] for mode in (RUNNER.LABWC_KEEP_MODE, RUNNER.DESKTOP_MODE)
               if (session / mode['result']).exists()]
    require(len(results) == 1, 'No kept GPU desktop result in this run')
    kept = json.loads(results[0].read_text())
    require(kept.get('outcome') == 'labwc-gpu-kept', 'The run did not keep a GPU desktop')
    before = kept['labwc_gpu']['desktop_before']
    restore_env = {k: v for k, v in before['environ'].items() if k != 'WAYLAND_DISPLAY'}
    require(restore_env.get('WLR_RENDERER') == 'pixman', 'The recorded Labwc was not on pixman')

    steps = []
    gpu = {}
    result = dict(schema_version=1, boot_id=kept['boot_id'], steps=steps, gpu=gpu,
                  started_at=datetime.now().astimezone().isoformat())
    client = load('j208_pvr_restore_ssh', RAM / 'ssh-runtime/client.py')
    with client.Session(session) as connection:
        require(connection.value['boot_id'] == kept['boot_id'],
                'Another boot than the kept run; nothing to restore')
        descriptor = os.open(session / MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(dict(boot_id=kept['boot_id'], created_at=result['started_at']), stream)

        def step(name, command, timeout=15):
            code, output = connection.run(command, timeout=timeout)
            steps.append(dict(name=name, exit=code, output=output[-600:]))
            return code, output

        desk = RUNNER.Desktop(connection, session, step, gpu, result)
        try:
            now = gpu['desktop_gpu'] = desk.state()
            require(len(now['labwc']) == 1 and
                    now.get('environ', {}).get('WLR_RENDERER') == 'gles2',
                    'The running Labwc is not the GPU session: %r' % now['labwc'])
            result['fault_before'] = desk.fault()
            result['restored'] = desk.restore(before, restore_env, now['foot'])
            if result['restored']:
                desk.readback('restored')
            time.sleep(5)
            result['gpu_runtime'] = RUNNER.state(connection)['gpu_runtime']
        except Exception as error:
            result['error'] = type(error).__name__ + ': ' + str(error)[:300]
    result['finished_at'] = datetime.now().astimezone().isoformat()
    (session / RESULT).write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: result.get(k) for k in ('restored', 'gpu_runtime', 'fault_before',
                                                 'error')}, indent=2))
    print(json.dumps({k: gpu.get(k) for k in ('stop_gpu', 'firefox_restarted')}, indent=1))
    print('details:', session / RESULT)
    return 0 if result.get('restored') else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True, type=Path)
    args = parser.parse_args()
    try:
        raise SystemExit(main(args.run))
    except (ValueError, FileExistsError) as error:
        print('restore stopped: ' + str(error), file=sys.stderr)
        raise SystemExit(2)
