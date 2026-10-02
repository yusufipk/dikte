#!/usr/bin/env python3
"""Exercise installer-generated launchers on disposable macOS runners.

Does not run the installer, register login items, access audio, or grant TCC.
The controller uses inert windows and settings. QApplication, Cocoa, startup
dispatch, instance locking, IPC, restart, and installer launchers are real.
Collection-behavior checks do not simulate or claim a real Spaces transition.
"""
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import site
import subprocess
import sys
import sysconfig
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def run(argv, **kwargs):
    return subprocess.run(argv, check=True, timeout=60, **kwargs)


def wait_json(path, process=None):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if path.exists():
            try:
                return json.loads(path.read_text())
            except json.JSONDecodeError:
                # A newly created response may still be being written.
                time.sleep(.1)
                continue
        if process is not None and process.poll() is not None:
            raise AssertionError(f"Native process exited: {process.returncode}")
        time.sleep(.1)
    raise AssertionError(f"No native response: {path.name}")


# This runs inside each generated launcher, after the isolated paths below have
# been installed. Replacing only the controller avoids microphone, hotkey,
# provider/configuration, and model-server startup while retaining run_app.
PRODUCTION_FIXTURE = r'''
import ctypes
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QWidget, QDialog, QSystemTrayIcon, QVBoxLayout, QLabel
from dikte import app as application, mac_window, ipc
from dikte.trayicon import app_icon

assert '--gui' in sys.argv, sys.argv
ipc.SERVER_NAME = application.SERVER_NAME = server_name
application.integrate.ensure = lambda: []
production_restart = application.Dikte.restart

class ReadyConfiguration:
    def transcribe_ready(self):
        return True
    def __getitem__(self, key):
        assert key == 'start_in_tray', key
        return True

class Controller:
    def __init__(self, app):
        assert app.platformName() == 'cocoa', app.platformName()
        self.app = app
        self.conf = ReadyConfiguration()
        self.server = self.instance_lock = None
        self.opened = []
        self.home_window = self.window('Home')
        self.settings_window = self.window('Settings')
        self.tray = QSystemTrayIcon(app_icon())
        self.tray.show()
        QTimer.singleShot(750, self.report)
        # Covers failures of the driver too, including a detached restart.
        QTimer.singleShot(30000, app.quit)

    def window(self, name):
        window = QDialog() if name == 'Settings' else QWidget()
        window.resize(400, 200)
        window.setWindowTitle('Dikte native startup smoke: ' + name)
        layout = QVBoxLayout(window)
        layout.addWidget(QLabel('Production startup dispatch / isolated ' + name))
        return window

    def open_home(self):
        self.opened.append('home')
        application._present(self.home_window)

    def open_settings(self):
        self.opened.append('settings')
        application._present(self.settings_window)

    def snapshot(self):
        def state(window):
            handle = window.windowHandle()
            return dict(visible=window.isVisible(), minimized=window.isMinimized(),
                        exposed=bool(handle and handle.isExposed()))
        return dict(ok=True, pid=os.getpid(), argv=sys.argv[1:],
                    executable=sys.executable, bundle=ipc.macos_bundle(),
                    platform=self.app.platformName(), home=state(self.home_window),
                    settings=state(self.settings_window), opened=list(self.opened),
                    tray_available=QSystemTrayIcon.isSystemTrayAvailable(),
                    tray_api_visible=self.tray.isVisible())

    def report(self):
        temporary = response.with_name('response-' + str(os.getpid()) + '.tmp')
        temporary.write_text(json.dumps(self.snapshot()))
        temporary.replace(response)

    def collection_policy(self):
        api = mac_window._appkit()
        details = []
        for name, widget in (('home', self.home_window), ('settings', self.settings_window)):
            def native_window():
                view = ctypes.c_void_p(int(widget.winId()))
                native = ctypes.c_void_p(api.ask(view, api.selector(b'window')))
                assert native.value, name
                return native
            def read():
                return int(api.ask_unsigned(native_window(), api.selector(b'collectionBehavior')))
            before = read()
            # These two Spaces policies conflict. Seed the opposite policy and
            # an unrelated bit, so a no-op or replacing the whole mask fails.
            seeded = ((before & ~mac_window.MOVE_TO_ACTIVE_SPACE)
                      | mac_window.CAN_JOIN_ALL_SPACES | mac_window.IGNORES_CYCLE)
            api.tell_unsigned(native_window(), api.selector(b'setCollectionBehavior:'), seeded)
            widget.hide()
            application._present(widget)
            self.app.processEvents()
            after_show = read()
            assert after_show & mac_window.MOVE_TO_ACTIVE_SPACE, after_show
            assert not after_show & mac_window.CAN_JOIN_ALL_SPACES, after_show
            assert after_show & mac_window.IGNORES_CYCLE, after_show
            preserved = seeded & ~(mac_window.CAN_JOIN_ALL_SPACES | mac_window.MOVE_TO_ACTIVE_SPACE)
            assert after_show & preserved == preserved, (seeded, after_show)
            widget.showMinimized()
            self.app.processEvents()
            assert widget.isMinimized(), name
            application._present(widget)
            self.app.processEvents()
            after_restore = read()
            assert widget.isVisible() and not widget.isMinimized(), name
            assert after_restore & mac_window.MOVE_TO_ACTIVE_SPACE, after_restore
            assert not after_restore & mac_window.CAN_JOIN_ALL_SPACES, after_restore
            assert after_restore & preserved == preserved, (seeded, after_restore)
            assert widget.grab().save(str(artifact / ('production-' + name + '.png')))
            details.append(dict(window=name, before=before, seeded=seeded,
                                after_show=after_show, after_restore=after_restore,
                                visible=widget.isVisible(), minimized=widget.isMinimized()))
        return details

    def handle(self, request, reply):
        command = request.get('cmd')
        if command == 'home':
            self.open_home()
        elif command == 'settings':
            self.open_settings()
        elif command == 'smoke-hide':
            self.home_window.hide()
            self.settings_window.hide()
        elif command == 'smoke-collection-policy':
            reply(dict(ok=True, pid=os.getpid(), checks=self.collection_policy(),
                       real_spaces_transition_tested=False))
            return
        elif command == 'restart':
            # Shell fallback restarts resolve a checkout entry differently;
            # only the native bundle is asked to exercise LaunchServices here.
            assert ipc.macos_bundle() is not None
            reply(self.snapshot())
            QTimer.singleShot(100, lambda: production_restart(self))
            return
        elif command == 'quit':
            reply(self.snapshot())
            QTimer.singleShot(50, self.app.quit)
            return
        else:
            assert command == 'status', request
        reply(self.snapshot())

    def shutdown(self):
        self.home_window.hide()
        self.settings_window.hide()
        self.tray.hide()
        if self.server is not None:
            self.server.close()

application.Dikte = Controller
raise SystemExit(application.main())
'''


def production_dispatch(source, root, bundle, entry, env, artifact, results):
    """Run the exact native and fallback launchers through app.main/run_app."""
    from dikte import ipc
    from PyQt6.QtCore import QCoreApplication

    client_app = QCoreApplication.instance() or QCoreApplication([])
    response = root / 'production-response.json'
    server_name = 'dikte-startup-' + uuid.uuid4().hex
    home = root / 'production-home'
    home.mkdir()
    isolated = dict(HOME=str(home), XDG_CONFIG_HOME=str(home / 'config'),
                    XDG_DATA_HOME=str(home / 'data'), XDG_CACHE_HOME=str(home / 'cache'),
                    QT_QPA_PLATFORM='cocoa')
    entry.write_text('import sys, json, os\nfrom pathlib import Path\n'
                    + f'os.environ.update({isolated!r})\n'
                    + 'for key in tuple(os.environ):\n'
                    + '    if key.endswith("_API_KEY"): os.environ.pop(key)\n'
                    + f'sys.path.insert(0, {str(ROOT)!r})\n'
                    + f'response = Path({str(response)!r})\n'
                    + f'artifact = Path({str(artifact)!r})\n'
                    + f'server_name = {server_name!r}\n' + PRODUCTION_FIXTURE)

    # Extract the actual shell fallback, including its no-argument handling.
    shell_bundle = root / 'Shell fallback.app'
    shell_executable = shell_bundle / 'Contents/MacOS/Dikte'
    shell_executable.parent.mkdir(parents=True)
    shutil.copy2(Path(getattr(sys, '_base_executable', sys.executable)).resolve(),
                 shell_executable.with_name('python3'))
    shell_helper = re.search(r'shell_literal\(\) \{.*?\n\}', source, re.S).group()
    shell_template = re.search(r'cat > "\$APP/Contents/MacOS/Dikte" <<EOF\n.*?\nEOF', source, re.S).group()
    generate = shell_helper + '\n' + '\n'.join(
        f'SHELL_{name}="$(shell_literal "${name}")"'
        for name in ('PY_HOME', 'PY_SITE', 'ENTRY')) + '\n' + shell_template
    run(['bash', '-c', generate], env=dict(env, APP=str(shell_bundle)), cwd=root)
    shell_executable.chmod(0o700)
    ipc.SERVER_NAME = server_name
    processes = []
    evidence = []

    def request(command):
        answer = ipc.send(command)
        assert answer and answer.get('ok'), (command, answer)
        return answer

    def start(kind, arguments, repeat=False):
        if not repeat:
            response.unlink(missing_ok=True)
        argv = (['/usr/bin/open', '-n', '-W', '-a', str(bundle)]
                + (['--args', *arguments] if arguments else [])
                if kind == 'native' else [str(shell_executable), *arguments])
        process = subprocess.Popen(argv, env=dict(env, **isolated), cwd=root)
        processes.append(process)
        if repeat:
            assert process.wait(timeout=15) == 0
            return request('status')
        data = wait_json(response, process)
        assert data['platform'] == 'cocoa', data
        assert data['tray_api_visible'], data
        assert data['argv'] == ['--gui', *(arguments or ['home'])], data
        if kind == 'native':
            assert data['bundle'] == str(bundle), data
        return data

    def visibility(data, home=False, settings=False):
        assert data['home']['visible'] == home, data
        assert data['settings']['visible'] == settings, data
        for name, visible in (('home', home), ('settings', settings)):
            if visible:
                assert not data[name]['minimized'], data
                assert data[name]['exposed'], data

    def stop():
        answer = ipc.send('quit')
        if answer is None:
            return
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if ipc.send('status') is None:
                break
            time.sleep(.1)
        else:
            raise AssertionError('Isolated production instance did not quit')
        for process in processes:
            assert process.wait(timeout=10) == 0
        processes.clear()

    try:
        for kind in ('native', 'shell'):
            for arguments, home_visible, settings_visible in (
                ([], True, False), (['--gui'], False, False),
                (['--autostart'], False, False),
                (['home'], True, False), (['settings'], False, True),
            ):
                data = start(kind, arguments)
                visibility(data, home_visible, settings_visible)
                evidence.append(dict(launcher=kind, arguments=arguments, startup=data))
                if not arguments:
                    pid = data['pid']
                    request('smoke-hide')
                    repeated = start(kind, ['--autostart'], repeat=True)
                    assert repeated['pid'] == pid, repeated
                    visibility(repeated)
                    repeated = start(kind, [], repeat=True)
                    assert repeated['pid'] == pid, repeated
                    visibility(repeated, home=True)
                    evidence[-1]['repeat_manual_pid'] = repeated['pid']
                    evidence[-1]['repeat_autostart_quiet'] = True
                    if kind == 'native':
                        policy = request('smoke-collection-policy')
                        assert not policy.get('legacy'), policy
                        assert [check['window'] for check in policy.get('checks', [])] == ['home', 'settings'], policy
                        assert policy['pid'] == pid, policy
                        results['cocoa_collection_policy'] = policy
                        response.unlink(missing_ok=True)
                        request('restart')
                        restarted = wait_json(response)
                        assert restarted['pid'] != pid, restarted
                        assert restarted['bundle'] == str(bundle), restarted
                        assert restarted['argv'] and all(arg == '--gui' for arg in restarted['argv']), restarted
                        visibility(restarted)
                        assert request('status')['pid'] == restarted['pid']
                        evidence[-1]['production_restart_quiet'] = restarted
                stop()
        results['production_startup_dispatch'] = evidence
        results['real_spaces_transition_tested'] = False
    finally:
        try:
            stop()
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
    # Keep the Qt client alive throughout all synchronous local IPC calls.
    assert client_app is not None


def main():
    if sys.platform != 'darwin':
        raise SystemExit('This probe requires a real macOS runner')
    artifact = ROOT / 'native-macos-results'
    artifact.mkdir(exist_ok=True)
    source = (ROOT / 'scripts/install-mac.sh').read_text()
    helper = re.search(r'c_string_literal\(\) \{.*?\n\}', source, re.S).group()
    template = re.search(r'cat > "\$LAUNCHER_SRC" <<EOF\n.*?\nEOF', source, re.S).group()
    cv = sysconfig.get_config_var
    library = (Path(sys.base_prefix) / 'Python' if cv('PYTHONFRAMEWORK') else
               Path(cv('LIBDIR')) / cv('LDLIBRARY')).resolve()
    assert library.is_file() and library.suffix != '.a', library
    results = {'platform': subprocess.check_output(['sw_vers'], text=True),
               'architecture': os.uname().machine, 'python': sys.version}
    with tempfile.TemporaryDirectory(prefix='dikte-native-') as temp:
        # LaunchServices canonicalizes macOS's /var -> /private/var alias.
        root = Path(temp).resolve()
        hostile = root / "paths ' \" $(touch INJECTED) `touch INJECTED2` \\\n"
        hostile.mkdir()
        bundle = hostile / 'Dikte.app'
        executable = bundle / 'Contents/MacOS/Dikte'
        executable.parent.mkdir(parents=True)
        entry = hostile / 'entry.py'
        response = root / 'response.json'
        restarted = root / 'restarted.json'
        server_name = 'dikte-native-' + uuid.uuid4().hex
        # Paths are embedded as Python literals, never interpolated shell source.
        entry.write_text('import sys, json, os\n'
            + f'os.environ.update({dict(HOME=str(root / "home"), XDG_CONFIG_HOME=str(root / "home/config"), XDG_DATA_HOME=str(root / "home/data"), XDG_CACHE_HOME=str(root / "home/cache"), QT_QPA_PLATFORM="cocoa")!r})\n'
            + f'sys.path.insert(0, {str(ROOT)!r})\n'
            + 'from pathlib import Path\nfrom dikte import ipc\n'
            + f'output=Path({str(response)!r})\n'
            + f'restarted=Path({str(restarted)!r})\n'
            + f'name={server_name!r}\n'
            + f'screenshot={str(artifact / "cocoa-widget.png")!r}\n'
            + '''
payload = dict(argv=sys.argv[1:], executable=sys.executable, bundle=ipc.macos_bundle())
if any(mode in sys.argv for mode in ('serve', 'gui-probe', 'restarted')):
    from PyQt6.QtWidgets import QApplication, QWidget, QSystemTrayIcon, QVBoxLayout, QLabel
    from PyQt6.QtCore import QTimer
    from PyQt6.QtNetwork import QLocalServer
    from dikte.app import _present
    from dikte.trayicon import app_icon
    app = QApplication([])
    assert app.platformName() == 'cocoa', app.platformName()
    app.setQuitOnLastWindowClosed(False)
    widget = QWidget()
    widget.resize(360, 180)
    widget.setWindowTitle('Dikte isolated native smoke')
    layout = QVBoxLayout(widget)
    layout.addWidget(QLabel('Synthetic native Cocoa launcher / IPC check'))
    tray = QSystemTrayIcon(app_icon())
    tray.show()
    _present(widget)
    app.processEvents()
    assert widget.isVisible()
    widget.showMinimized()
    app.processEvents()
    _present(widget)
    app.processEvents()
    assert not widget.isMinimized()
    def capture():
        assert widget.grab().save(screenshot)
        payload.update(platform=app.platformName(), visible=widget.isVisible(),
                       exposed=widget.windowHandle().isExposed(),
                       screens=len(app.screens()), tray_available=QSystemTrayIcon.isSystemTrayAvailable(),
                       tray_api_visible=tray.isVisible(),
                       tray_geometry=[tray.geometry().x(), tray.geometry().y(),
                                      tray.geometry().width(), tray.geometry().height()])
        (restarted if 'restarted' in sys.argv else output).write_text(json.dumps(payload))
        if 'serve' not in sys.argv:
            app.quit()
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    assert server.listen(name), server.errorString()
    def connection():
        sock = server.nextPendingConnection()
        pending = bytearray()
        def read():
            pending.extend(bytes(sock.readAll()))
            if b'\\n' not in pending:
                return
            text=bytes(pending).decode().strip()
            try:
                data=json.loads(text)
            except json.JSONDecodeError:
                data={'cmd':text}
            sock.write((json.dumps(dict(ok=True, command=data['cmd'], bundle=ipc.macos_bundle()))+'\\n').encode())
            sock.flush()
            sock.waitForBytesWritten(1000)
            sock.disconnectFromServer()
            if data['cmd']=='restart':
                QTimer.singleShot(100, restart)
        sock.readyRead.connect(read)
    def restart():
        ipc.respawn(['restarted'])
        app.quit()
    server.newConnection.connect(connection)
    QTimer.singleShot(1000, capture)
    QTimer.singleShot(30000, app.quit)
    app.exec()
    tray.hide()
    server.close()
else:
    output.write_text(json.dumps(payload))
''')
        home = root / 'home'
        home.mkdir()
        env = dict(os.environ, PY=sys.executable, PY_HOME=sys.base_prefix,
                   PY_SITE=site.getsitepackages()[0], PY_LIBRARY=str(library),
                   ENTRY=str(entry), LAUNCHER_SRC=str(root / 'launcher.c'),
                   HOME=str(home), XDG_CONFIG_HOME=str(home / 'config'),
                   XDG_DATA_HOME=str(home / 'data'), XDG_CACHE_HOME=str(home / 'cache'),
                   QT_QPA_PLATFORM='cocoa')
        generate = helper + '\n' + '\n'.join(
            f'C_{name}="$(c_string_literal "${name}")"'
            for name in ('PY_HOME', 'PY_SITE', 'PY_LIBRARY', 'ENTRY')) + '\n' + template
        run(['bash', '-c', generate], env=env, cwd=root)
        run(['clang', '-x', 'c', str(root / 'launcher.c'), '-o', str(executable)])
        metadata = {'CFBundleIdentifier':'io.github.yusufipk.dikte',
                    'CFBundleExecutable':'Dikte', 'CFBundleName':'Dikte',
                    'CFBundlePackageType':'APPL', 'CFBundleVersion':'1',
                    'LSUIElement':True}
        plist = bundle / 'Contents/Info.plist'
        plist.write_bytes(plistlib.dumps(metadata))
        run(['codesign', '--force', '--sign', '-', '--identifier', metadata['CFBundleIdentifier'], str(bundle)])
        run(['codesign', '--verify', '--deep', '--strict', str(bundle)])
        for args in [['probe'], [str(entry), '--gui', 'probe']]:
            response.unlink(missing_ok=True)
            run([str(executable), *args], env=env, cwd=root)
            data = wait_json(response)
            assert data['argv'] == ['--gui', 'probe'], data
            assert data['bundle'] == str(bundle), data
            assert Path(data['executable']) == executable, data
        results['compiled_launcher_and_reexec'] = 'passed'
        # Actual malformed/on-disk identity checks, not patched file reads.
        for content in [b'<?xml', plistlib.dumps(dict(metadata, CFBundleIdentifier='org.python.python'))]:
            plist.write_bytes(content)
            response.unlink()
            run([str(executable), 'probe'], env=env, cwd=root)
            assert wait_json(response)['bundle'] is None
        plist.write_bytes(plistlib.dumps(metadata))
        run(['codesign', '--force', '--sign', '-', '--identifier', metadata['CFBundleIdentifier'], str(bundle)])
        run(['codesign', '--verify', '--deep', '--strict', str(bundle)])
        results['plist_identity_and_codesign'] = 'passed'
        response.unlink()
        # Native Cocoa plus real QLocalServer/QLocalSocket; no app controller/audio.
        process = subprocess.Popen(['/usr/bin/open', '-n', '-W', '-a', str(bundle),
                                    '--args', 'serve'], env=env, cwd=root)
        try:
            results['cocoa'] = wait_json(response, process)
            sys.path.insert(0, str(ROOT))
            from dikte import ipc
            ipc.SERVER_NAME = server_name
            status = ipc.send('status')
            assert status and status['ok'] and status['bundle'] == str(bundle), status
            result = ipc.send('restart')
            assert result and result['ok'], result
            data = wait_json(restarted)
            assert data['argv'] == ['--gui', 'restarted'] and data['bundle'] == str(bundle), data
            assert data['platform'] == 'cocoa' and data['visible'] and data['exposed'], data
            results['cocoa_after_launchservices_restart'] = data
            process.wait(timeout=10)
            assert process.returncode == 0
            results['native_ipc_and_launchservices_restart'] = 'passed'
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        production_dispatch(source, root, bundle, entry, env, artifact, results)
        assert not (root / 'INJECTED').exists()
        assert not (root / 'INJECTED2').exists()
        results['path_injection_sentinels'] = 'absent'
    (artifact / 'result.json').write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
