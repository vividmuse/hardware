#!/usr/bin/env python3
"""The GPU desktop after a boot, in one command: stage A, then
run-pvr-t8011.py --desktop.

  python3 gpu-desktop.py --run RUN --confirmation gpu-desktop-once > log 2>&1

For a fresh audio-image boot with SSH up (desktop-session.py, then w and x).
- It waits up to SSH_WAIT_SECONDS for SSH to be ready (x's
  ssh-runtime-ready.json), which stage A requires.
- Stage A (../gfx-power/run-gfx-power.py) runs first. If it already ran on
  this boot, its result is used instead, and it has to be a success.
- The runner then runs, once per boot (its own marker), with all of
  --labwc-keep's checks but no benchmark. If they pass, Labwc stays on the
  GPU with Firefox reopened.
- Each step's output goes to its own log in the run directory
  (gpu-desktop-stage-a.log, gpu-desktop-runner.log).
- restore-desktop.py brings Labwc back on pixman; a reboot clears it all.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
GPU = HERE.parent
STAGE_A = GPU / 'gfx-power/run-gfx-power.py'
STAGE_A_MARKER = 'gfx-power-attempted.json'
STAGE_A_RESULT = 'gfx-power-result.json'
STAGE_A_CONFIRMATION = 'power-gfx-once'
RUNNER = HERE / 'run-pvr-t8011.py'
RUNNER_RESULT = 'pvr-t8011-desktop-result.json'
RUNNER_CONFIRMATION = 'desktop-pvr-t8011-once'
CONFIRMATION = 'gpu-desktop-once'
SSH_READY = 'ssh-runtime-ready.json'
SSH_WAIT_SECONDS = 300


def outcome(path):
    try:
        return json.loads(path.read_text()).get('outcome')
    except (OSError, ValueError):
        return None


def step(argv, log):
    """Run one step with its output to log; its exit code."""
    with open(log, 'w') as stream:
        return subprocess.run([sys.executable] + argv, stdout=stream, stderr=subprocess.STDOUT,
                              cwd=GPU.parents[1]).returncode


def main(session):
    session = session.resolve()
    if not session.is_dir():
        raise ValueError('No run directory %s' % session)
    summary = dict(run=str(session))

    # x writes the ready record only once the SSH client is verified, a while
    # after the launch; stage A refuses to start before it
    waited = 0
    while not (session / SSH_READY).is_file():
        if waited >= SSH_WAIT_SECONDS:
            raise ValueError('SSH is not ready after %d s (press x first)' % waited)
        time.sleep(1)
        waited += 1
    if waited:
        summary['ssh_waited_seconds'] = waited

    if (session / STAGE_A_MARKER).exists():
        summary['stage_a'] = dict(ran='before', outcome=outcome(session / STAGE_A_RESULT))
    else:
        code = step([str(STAGE_A), '--run', str(session), '--confirmation',
                     STAGE_A_CONFIRMATION], session / 'gpu-desktop-stage-a.log')
        summary['stage_a'] = dict(ran='now', exit=code, outcome=outcome(session / STAGE_A_RESULT))
    if summary['stage_a']['outcome'] != 'success':
        print(json.dumps(summary, indent=1))
        print('stage A did not succeed; the GPU desktop is not started', file=sys.stderr)
        return 1

    code = step([str(RUNNER), '--run', str(session), '--desktop', '--confirmation',
                 RUNNER_CONFIRMATION], session / 'gpu-desktop-runner.log')
    result = {}
    try:
        result = json.loads((session / RUNNER_RESULT).read_text())
    except (OSError, ValueError):
        pass
    gpu = result.get('labwc_gpu') or {}
    summary['desktop'] = dict(exit=code, outcome=result.get('outcome'),
                              render_passed=result.get('render_passed'),
                              render_failed=result.get('render_failed'),
                              loader_override=gpu.get('loader_override'),
                              map_failures=gpu.get('map_failures'),
                              firefox_restarted=gpu.get('firefox_restarted'),
                              error=gpu.get('error'))
    print(json.dumps(summary, indent=1))
    print('logs:', session / 'gpu-desktop-stage-a.log', session / 'gpu-desktop-runner.log')
    return 0 if result.get('outcome') == 'labwc-gpu-kept' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--run', required=True, type=Path)
    parser.add_argument('--confirmation', required=True, help='must be ' + CONFIRMATION)
    args = parser.parse_args()
    if args.confirmation != CONFIRMATION:
        print('gpu-desktop stopped: exact --confirmation %s is required' % CONFIRMATION,
              file=sys.stderr)
        raise SystemExit(2)
    try:
        raise SystemExit(main(args.run))
    except ValueError as error:
        print('gpu-desktop stopped: ' + str(error), file=sys.stderr)
        raise SystemExit(2)
