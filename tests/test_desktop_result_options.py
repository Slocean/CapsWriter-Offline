"""Compile the real WPF code; use hidden controls and a fake clipboard, no input hooks/backend."""
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSC = pathlib.Path('C:/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe')


@unittest.skipUnless(CSC.exists(), 'Windows .NET Framework required')
class DesktopResultOptionsTests(unittest.TestCase):
    def test_result_and_options(self):
        with tempfile.TemporaryDirectory() as directory:
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
