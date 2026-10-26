"""Check isolated AAX module inspection; this does not validate an AAX audio host.

Without --plugin, build local ABI fixtures and verify export/version handling,
stdout isolation, failure and timeout cleanup. With --plugin, inspect only the
explicitly selected package. No plugin installation or audio processing occurs.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, type=Path)
    parser.add_argument('--plugin', type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--timeout', type=float, default=3)
    args = parser.parse_args()
    if not .5 <= args.timeout <= 30:
        parser.error('--timeout must be between 0.5 and 30 seconds')
    host = args.host.resolve(strict=True)
    root = args.output_dir.resolve() / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    root.mkdir(parents=True, exist_ok=False)
    environment = os.environ.copy()
    environment['PYMSS_STUDIO_DATA_ROOT'] = str(root / 'unused-desktop-data')
    results = []

    def invoke(name, arguments, behavior='', input_text=None):
        stdout = root / f'{name}-stdout.json'
        stderr = root / f'{name}-stderr.log'
        env = {**environment, 'PYMSS_AAX_FIXTURE_BEHAVIOR': behavior}
        options = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
        with stdout.open('wb') as output, stderr.open('wb') as errors:
            process = subprocess.Popen([str(host), *map(str, arguments)], env=env,
                stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                stdout=output, stderr=errors, **options)
            if input_text is not None:
                process.stdin.write(input_text.encode('utf-8'))
                process.stdin.close()
            timed_out = False
            try:
                process.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
        assert process.poll() is not None, 'The inspection helper must be reaped'
        assert stdout.stat().st_size <= 1024 * 1024, 'Unexpected inspection output size'
        return process.returncode, timed_out, stdout.read_text(encoding='utf-8')

    def inspect(name, package, behavior=''):
        code, timed_out, text = invoke(name, ['--aax-probe', package], behavior)
        assert code == 0 and not timed_out, (name, code, timed_out, str(root / f'{name}-stderr.log'))
        rows = text.splitlines()
        assert len(rows) == 1, 'Module stdout must not corrupt the private JSON channel'
        report = json.loads(rows[0])
        assert report['moduleLoaded'] is True
        assert report['audioProcessingAvailable'] is False
        return report

    if args.plugin:
        report = inspect('selected-package', args.plugin.resolve(strict=True))
        results.append({'case': 'selected-module-inspection', 'report': report})
    elif sys.platform not in ('win32', 'darwin'):
        code, timed_out, _ = invoke('unsupported-platform', ['--aax-probe', root / 'Unused.aaxplugin'])
        assert code != 0 and not timed_out
        results.append({'case': 'unsupported-platform-rejected', 'passed': True})
    else:
        source = Path(__file__).resolve().parents[2] / 'native/aax-probe/tests/fixture/module.rs'

        def build(name, missing=False):
            package = root / f'{name}.aaxplugin'
            directory = package / ('Contents/x64' if os.name == 'nt' else 'Contents/MacOS')
            directory.mkdir(parents=True)
            binary = directory / (f'{name}.aaxplugin' if os.name == 'nt' else name)
            command = ['rustc', '--edition=2021', '--crate-type=cdylib', '--crate-name', name,
                '--cfg', 'manual_behaviors', str(source), '-o', str(binary)]
            if missing:
                command += ['--cfg', 'missing_sdk']
            output = subprocess.run(command, env=environment, capture_output=True, timeout=60)
            assert output.returncode == 0, output.stderr.decode('utf-8', errors='replace')[-4000:]
            return package

        package = build('Probe')
        missing = build('Missing', True)
        for name, behavior, valid, status in [
            ('standard', '', True, '0x00000000'),
            ('positive-success', 'positive', True, '0x00000001'),
            ('failed-version-query', 'negative', False, '0x80004005'),
            ('zero-version', 'zero', False, '0x00000000'),
        ]:
            report = inspect(name, package, behavior)
            assert report['sdkVersionValid'] is valid
            assert report['sdkVersionResult'] == status
            assert report['missingExports'] == []
            if valid:
                assert report['sdkVersion'] == '0x0208000100000002'
            else:
                assert report['sdkVersion'] is None
            results.append({'case': name, 'passed': True})
        report = inspect('missing-version-export', missing)
        assert report['missingExports'] == ['ACFGetSDKVersion'] and not report['sdkVersionValid']
        results.append({'case': 'missing-version-export', 'passed': True})
        code, timeout, _ = invoke('crash', ['--aax-probe', package], 'crash')
        assert code != 0 and not timeout
        results.append({'case': 'module-crash-isolated', 'passed': True})
        code, timeout, _ = invoke('hang', ['--aax-probe', package], 'hang')
        assert timeout
        results.append({'case': 'module-hang-helper-terminated', 'passed': True})
        inspect('healthy-after-failure', package)
        results.append({'case': 'new-helper-healthy-after-crash-and-timeout', 'passed': True})
        code, timeout, _ = invoke('ordinary-scan', ['--plugin-scan', package])
        assert code != 0 and not timeout
        project = {'id': 'aax-admission', 'name': 'Module', 'sources': [], 'tracks': [],
            'pluginChain': [{'id': 'slot', 'format': 'aax', 'pluginId': 'probe', 'name': 'Module',
                'path': str(package), 'bypassed': False}]}
        request = {'id': 1, 'op': 'load', 'offline': True, 'project': project, 'ffmpeg': str(host)}
        code, timeout, text = invoke('ordinary-load', ['--audio-host'], input_text=json.dumps(request)+'\n')
        responses = [json.loads(row) for row in text.splitlines()]
        rejected = next(row for row in responses if row.get('id') == 1)
        assert code == 0 and not timeout and rejected['ok'] is False
        assert 'aax' in rejected['error'].lower()
        results.append({'case': 'ordinary-scan-and-audio-load-remain-unsupported', 'passed': True})
    assert not (root / 'unused-desktop-data').exists(), 'Inspection must not initialize desktop storage'
    report = {'host': str(host), 'results': results, 'passed': len(results),
        'scope': 'Module/ABI inspection only; no AAX audio processor or commercial plugin acceptance.'}
    (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
