import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent


@unittest.skipUnless(os.name=='nt','Windows bootstrap')
class PythonSetupTests(unittest.TestCase):
    def script(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'check.ps1'
            path.write_text("$ErrorActionPreference='Stop'\n. '"+str(ROOT/'PythonSetup.ps1').replace("'","''")+"'\n"+text,encoding='utf-8')
            result=subprocess.run(['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',str(path)],capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            return result.stdout

    def test_real_interpreter_probe_preserves_quotes_and_architecture(self):
        import sys
        value=self.script("Get-PythonInfo '"+sys.executable.replace("'","''")+"' | ConvertTo-Json")
        info=json.loads(value);self.assertEqual(info['version'],'.'.join(map(str,sys.version_info[:3])));self.assertTrue(info['stable']);self.assertIn(info['bits'],(32,64))

    def test_latest_stable_parser_skips_prereleases_and_manager(self):
        value=self.script("""
function Invoke-WebRequest { return @{Content='Latest Python install manager - Python install manager 26.4 <a>Latest Python 3 Release - Python 3.15.0</a> Python 3.16.0a1'} }
Get-LatestPythonRelease | ConvertTo-Json
""")
        info=json.loads(value);self.assertEqual(info['version'],'3.15.0');self.assertIn('https://www.python.org/ftp/python/3.15.0/',info['url']);self.assertNotIn('3.16',info['url'])

    def test_current_python_skips_all_download_and_install_steps(self):
        value=self.script("""
function Get-LatestPythonRelease { return @{version='3.15.0';architecture='AMD64';url='https://www.python.org/ftp/python/3.15.0/python-3.15.0-amd64.exe'} }
function Find-CurrentPython { return 'C:\\Controlled\\python.exe' }
function Invoke-WebRequest { throw 'Unexpected download' }
function Start-Process { throw 'Unexpected install' }
$result=Install-CurrentPython $PSScriptRoot
if($result -ne 'C:\\Controlled\\python.exe'){throw 'Wrong existing interpreter'}
""")
        self.assertIn('Skipping Python installation',value)

    def test_compatible_patch_parser_excludes_preview_releases(self):
        value=self.script("""
function Invoke-WebRequest { return @{Content='Python 3.13.9 Python 3.13.16 Python 3.13.17rc1 Python 3.15.0'} }
Get-LatestPythonRelease '3.13' | ConvertTo-Json
""")
        self.assertEqual(json.loads(value)['version'],'3.13.16')

    def test_patch_upgrade_validates_registered_interpreter(self):
        value=self.script("""
function Get-LatestPythonRelease { return @{version='3.13.16';architecture='AMD64';url='https://www.python.org/fixture.exe'} }
$script:lookups=0
function Find-CurrentPython { $script:lookups++; if($script:lookups -gt 1){ return 'C:\\Controlled\\ExistingPython313\\python.exe' } }
function Get-PythonInfo { param($Executable); if($Executable -eq 'C:\\Controlled\\ExistingPython313\\python.exe'){ return @{version='3.13.16';stable=$true;architecture='AMD64';bits=64;executable=$Executable} } }
function Invoke-WebRequest { param($Uri,$OutFile,[switch]$UseBasicParsing,$TimeoutSec); Set-Content -LiteralPath $OutFile -Value 'signed fixture' }
function Get-AuthenticodeSignature { return @{Status='Valid';SignerCertificate=@{Subject='CN=Python Software Foundation'}} }
function Start-Process { $process=[pscustomobject]@{ExitCode=0}; $process | Add-Member ScriptMethod WaitForExit { return $true }; return $process }
$result=Install-CurrentPython $PSScriptRoot '3.13'
if($result -ne 'C:\\Controlled\\ExistingPython313\\python.exe'){throw 'Existing patch-updated installation was not resolved'}
""")
        self.assertIn('Downloading Python 3.13.16',value)

    def test_invalid_signature_never_executes_downloaded_file(self):
        value=self.script("""
function Get-LatestPythonRelease { return @{version='3.15.0';architecture='AMD64';url='https://www.python.org/ftp/python/3.15.0/python-3.15.0-amd64.exe'} }
function Find-CurrentPython { return $null }
function Invoke-WebRequest { param($Uri,$OutFile,[switch]$UseBasicParsing,$TimeoutSec); Set-Content -LiteralPath $OutFile -Value 'controlled unsigned fixture';$script:downloaded=$OutFile }
function Get-AuthenticodeSignature { return @{Status='NotSigned';SignerCertificate=@{Subject='Not Python'}} }
function Start-Process { throw 'Unexpected execution' }
try { Install-CurrentPython $PSScriptRoot; throw 'Unsigned installer was accepted' }
catch { if($_.Exception.Message -notmatch 'signature is invalid'){throw} }
if(Test-Path -LiteralPath $script:downloaded){throw 'Temporary download was not cleaned'}
""")
        self.assertIn('Downloading Python',value)

    def test_unrecognized_release_stops_before_installation(self):
        self.script("""
function Invoke-WebRequest { return @{Content='Unrecognized official page'} }
try { Get-LatestPythonRelease;throw 'Unknown version accepted' }
catch { if($_.Exception.Message -notmatch 'could not be identified'){throw} }
""")
