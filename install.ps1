param([switch]$ShortcutOnly, [string]$ShortcutDirectory = '', [switch]$Complete)
$ErrorActionPreference = 'Stop'
$appRoot = $PSScriptRoot
Set-Location -LiteralPath $appRoot
if ($Complete) { Start-Transcript -Path (Join-Path $appRoot 'setup-install.log') -Append -Force | Out-Null }
trap {
    if ($Complete) { Stop-Transcript -ErrorAction SilentlyContinue | Out-Null }
    throw
}
function New-AppShortcuts {
    $iconPath = Join-Path $appRoot 'assets\brand\veynuq-dark.ico'
    if (-not (Test-Path -LiteralPath $iconPath -PathType Leaf)) { throw 'App icon is missing. Extract the complete release archive and run the installer again.' }
    $shortcutShell = New-Object -ComObject WScript.Shell
    $locations = if ($ShortcutDirectory) { @($ShortcutDirectory) } else { @([Environment]::GetFolderPath('Desktop'), (Join-Path ([Environment]::GetFolderPath('Programs')) 'Veynuq')) }
    foreach ($location in $locations) {
        New-Item -ItemType Directory -Path $location -Force | Out-Null
        $shortcut = $shortcutShell.CreateShortcut((Join-Path $location 'Veynuq.lnk'))
        $shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $shortcut.Arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + (Join-Path $appRoot 'Launcher.ps1') + '"'
        $shortcut.WorkingDirectory = $appRoot
        $shortcut.Description = 'Veynuq desktop AI agent'
        $shortcut.IconLocation = $iconPath + ',0'
        $shortcut.Save()
        $oldPath = Join-Path $location 'Veyq.lnk'
        if (Test-Path -LiteralPath $oldPath) {
            $old = $shortcutShell.CreateShortcut($oldPath)
            if ($old.WorkingDirectory -eq $appRoot) { Remove-Item -LiteralPath $oldPath }
        }
    }
}
if ($ShortcutOnly) { New-AppShortcuts; exit 0 }
. (Join-Path $appRoot 'PythonSetup.ps1')
$pythonExecutable = Install-CurrentPython $appRoot
# Current speech/CPU inference wheels do not support Python 3.15 on Windows.
# Keep the newest stable interpreter available, and use the newest patch of
# the tested 3.13 series for the complete application and all its built-in tools.
if (-not (Get-PythonInfo $pythonExecutable).version.StartsWith('3.13.')) {
    Write-Host 'Preparing the latest compatible Python 3.13 runtime for all tools, including offline dictation...'
    $pythonExecutable = Install-CurrentPython $appRoot '3.13'
}
Write-Host '[2/5] Preparing the application runtime...'
$runtimeName = 'install-' + [guid]::NewGuid().ToString('N')
$environment = Join-Path $appRoot ('.veyq-runtime\' + $runtimeName)
& $pythonExecutable -m venv $environment
if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
$runtimePython = Join-Path $environment 'Scripts\python.exe'
Write-Host '[3/5] Installing all built-in tool dependencies...'
& $runtimePython -m pip install -r (Join-Path $appRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. The previous runtime is preserved.' }
& $runtimePython (Join-Path $appRoot 'app.py') --install
if ($LASTEXITCODE -ne 0) { throw 'Application setup failed. The previous runtime is preserved.' }
Write-Host '[4/5] Preparing the default local image generator (SD 1.5, CPU, no account)...'
Write-Host 'First download: approximately 4.3 GB. Allow approximately 5 GB of free disk space. Verified existing files are reused.'
& $runtimePython (Join-Path $appRoot 'app.py') --prepare-images
if ($LASTEXITCODE -ne 0) { throw 'Image engine setup failed. Retry Installer.bat, or use Prepare local image engine in Settings. The previous runtime is preserved.' }
Write-Host '[5/5] Creating shortcuts with the Veynuq icon...'
if ($Complete) {
    Write-Host 'Preparing the chat model, dictation, interface and coding tools...'
    & $runtimePython (Join-Path $appRoot 'app.py') --prepare-all
    if ($LASTEXITCODE -ne 0) { throw 'Complete configuration failed. Retry Veynuq-Setup.exe. Downloaded components are reused.' }
}
$runtimeFile = Join-Path $appRoot 'runtime.json'
$runtimeTemp = $runtimeFile + '.tmp'
[IO.File]::WriteAllText($runtimeTemp, (@{ python = $runtimePython } | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $runtimeTemp -Destination $runtimeFile -Force
New-AppShortcuts
Write-Host 'Veynuq is installed. Open the Desktop shortcut to start.'
Write-Host 'Choose a local model or a compatible provider in Settings. Existing engines are reused.'
if ($Complete) { Stop-Transcript | Out-Null }
