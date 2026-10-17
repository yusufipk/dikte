"""Run generated wrappers with adversarial paths, without running the installer."""
import os
from pathlib import Path
import re
import site
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(os.name == 'posix', 'macOS shell wrappers require POSIX paths')
class ShellWrappers(unittest.TestCase):
    def test_generated_command_treats_paths_and_arguments_as_data(self):
        installer = (ROOT / 'scripts/install-mac.sh').read_text()
        helper = re.search(r'shell_literal\(\) \{.*?\n\}', installer, re.S).group()
        cli = re.search(r'cat > "\$BIN_DIR/dikte" <<EOF\n.*?\nEOF', installer, re.S).group()
        gui = re.search(r'cat > "\$APP/Contents/MacOS/Dikte" <<EOF\n.*?\nEOF', installer, re.S).group()
        with tempfile.TemporaryDirectory(prefix='dikte-shell-test-') as temp:
            root = Path(temp)
            python = root / 'python \r\n $(printf BAD) `printf BAD` \' " \\'
            python.symlink_to(sys.executable)
            entry = root / 'entry $(printf BAD) `printf BAD` \' " \\.py'
            entry.write_text('import sys; print(repr(sys.argv[1:]))')
            bin_dir = root / 'bin'; bin_dir.mkdir()
            app = root / 'Dikte.app'; (app / 'Contents/MacOS').mkdir(parents=True)
            (app / 'Contents/MacOS/python3').symlink_to(sys.executable)
            env = dict(os.environ, PY=str(python), ENTRY=str(entry),
                       PY_HOME=sys.base_prefix, PY_SITE='$(printf BAD) `printf BAD` \' " \\',
                       BIN_DIR=str(bin_dir), APP=str(app))
            generate = helper + '\n' + '\n'.join(
                f'SHELL_{name}="$(shell_literal "${name}")"'
                for name in ('PY', 'ENTRY', 'PY_HOME', 'PY_SITE')) + '\n' + cli + '\n' + gui
            subprocess.run(['bash', '-c', generate], env=env, check=True)
            argument = 'argument $(printf BAD) `printf BAD` \' " \\'
            output = subprocess.check_output(['sh', str(bin_dir / 'dikte'), argument], text=True)
            self.assertEqual(output.strip(), repr([argument]))
            output = subprocess.check_output(['sh', str(app / 'Contents/MacOS/Dikte'), argument], text=True)
            self.assertEqual(output.strip(), repr(['--gui', argument]))
            # Exercise the actual generated shell with production dispatch,
            # not just a reconstruction of the launcher's argv in a mock.
            entry.write_text('import sys\n'
                             + f'sys.path[:0] = {[str(ROOT), *site.getsitepackages()]!r}\n'
                             + 'from unittest.mock import patch\n'
                             + 'from dikte import app\n'
                             + 'with patch.object(app, "run_app", '
                             + 'side_effect=lambda args: print(repr(args))):\n'
                             + '    app.main()\n')
            for args, dispatched in (([], ['home']), (['--gui'], []),
                                     (['--autostart'], ['autostart']),
                                     (['settings'], ['settings']),
                                     (['home'], ['home'])):
                with self.subTest(args=args):
                    output = subprocess.check_output(
                        ['sh', str(app / 'Contents/MacOS/Dikte'), *args], text=True)
                    self.assertEqual(output.strip(), repr(dispatched))
            for name in ('update.sh', 'uninstall.sh'):
                source = (ROOT / 'scripts' / name).read_text()
                parser = re.search(r"PY=\"\$\(python3 -c '([^']+)'", source).group(1)
                parsed = subprocess.check_output([sys.executable, '-c', parser, str(bin_dir / 'dikte')]).decode()
                self.assertEqual(parsed.strip(), str(python))
