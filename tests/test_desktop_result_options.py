"""Compile the real WPF code; use hidden controls and a fake clipboard, no input hooks/backend."""
import pathlib
import hashlib
import runpy
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSC = pathlib.Path('C:/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe')


@unittest.skipUnless(CSC.exists(), 'Windows .NET Framework required')
class DesktopResultOptionsTests(unittest.TestCase):
    def test_result_and_options(self):
        with tempfile.TemporaryDirectory() as directory:
            shutil.copytree(ROOT / 'assets/fonts', pathlib.Path(directory) / 'assets/fonts')
            shutil.copyfile(ROOT / 'assets/brand-mark.png', pathlib.Path(directory) / 'assets/brand-mark.png')
            exe = pathlib.Path(directory) / 'DesktopFeatureTests.exe'
            version = pathlib.Path(directory) / 'Version.cs'
            version.write_text('internal static partial class Desktop { internal const string AppVersion = "'
                               + (ROOT / 'installer/version.txt').read_text().strip() + '"; }')
            refs = ['WPF/PresentationFramework.dll', 'WPF/PresentationCore.dll',
                    'WPF/WindowsBase.dll', 'System.Xaml.dll']
            args = [str(CSC), '/nologo', '/target:exe', '/main:DesktopFeatureTests', f'/out:{exe}']
            args += [f'/reference:{CSC.parent / ref}' for ref in refs]
            args += ['/reference:' + ref for ref in ['System.Drawing.dll', 'System.Security.dll',
                                                    'System.Windows.Forms.dll', 'System.Web.Extensions.dll']]
            args += [str(ROOT / 'desktop' / name) for name in
                     ['CapsWriterDesktop.cs', 'Visuals.cs', 'Shortcuts.cs', 'Update.cs']]
            args += [str(version), str(ROOT / 'tests/desktop_result_options.cs')]
            built = subprocess.run(args, capture_output=True, text=True, timeout=120)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            ran = subprocess.run([str(exe)], capture_output=True, timeout=60)
            self.assertEqual(ran.returncode, 0, (ran.stdout + ran.stderr).decode('utf-8', 'replace'))
            self.assertIn(b'PASS:', ran.stdout)


    def test_ui_assets_staged_and_required(self):
        """Real payload staging carries local fonts and logo; no EXEs are launched."""
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            collect = root / 'collect'
            notice = collect / 'internal/_sounddevice_data/portaudio-binaries/README.md'
            notice.parent.mkdir(parents=True)
            notice.write_text('isolated packaging fixture')
            (collect / 'start_client.exe').write_bytes(b'not executed')
            gui = root / 'gui.exe'
            gui.write_bytes(b'not executed')
            payload = root / 'payload'
            import sys
            staged = subprocess.run([sys.executable, str(ROOT / 'installer/stage_payload.py'),
                                     '--repo', str(ROOT), '--collect', str(collect),
                                     '--gui-exe', str(gui), '--out', str(payload)],
                                    capture_output=True, timeout=60)
            self.assertEqual(staged.returncode, 0, (staged.stdout + staged.stderr).decode('utf-8', 'replace'))
            stage = runpy.run_path(str(ROOT / 'installer/stage_payload.py'))
            for name in stage['ASSET_FILES']:
                expected = hashlib.sha256((ROOT / 'assets' / name).read_bytes()).digest()
                self.assertEqual(hashlib.sha256((payload / 'assets' / name).read_bytes()).digest(), expected)
            (payload / 'assets/fonts/NotoSansSC-Regular.otf').unlink()
            with self.assertRaises(SystemExit):
                stage['verify_payload'](payload)
