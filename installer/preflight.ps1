param([Parameter(Mandatory=$true)][string]$AppRoot)
$ErrorActionPreference='Stop'
try {
    $target=[IO.Path]::GetFullPath($AppRoot).TrimEnd('\')
    if ($target -eq [IO.Path]::GetPathRoot($target).TrimEnd('\') -or (Test-Path -LiteralPath (Join-Path $target '.git'))) { throw 'Choose a dedicated application directory outside a source repository.' }
    if (Test-Path -LiteralPath $target) {
        $item=Get-Item -LiteralPath $target -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'The application directory cannot be a link.' }
        $contents=@(Get-ChildItem -LiteralPath $target -Force)
        if ($contents.Count -and -not ((Test-Path -LiteralPath (Join-Path $target 'app.py')) -and (Test-Path -LiteralPath (Join-Path $target 'install.ps1')) -and (Test-Path -LiteralPath (Join-Path $target 'build.json')))) { throw 'This folder contains unrelated files. Choose an empty application directory.' }
    }
    $profile=$env:VEYNUQ_DATA_DIR
    if (-not $profile) { $profile=$env:VEYQ_DATA_DIR }
    $anchor=Join-Path $target 'profile.json'
    if (-not $profile -and (Test-Path -LiteralPath $anchor)) { $profile=(Get-Content -LiteralPath $anchor -Raw | ConvertFrom-Json).data_dir }
    if (-not $profile) {
        $profile=Join-Path $env:LOCALAPPDATA 'Veynuq'
        if (Test-Path -LiteralPath (Join-Path $env:LOCALAPPDATA 'Veyq\state.json')) { $profile=Join-Path $env:LOCALAPPDATA 'Veyq' }
    }
    $lock=Join-Path $profile 'instance.lock'
    if (Test-Path -LiteralPath $lock) {
        $stream=[IO.File]::Open($lock,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::ReadWrite)
        try { $stream.Lock(0,1); $stream.Unlock(0,1) } finally { $stream.Dispose() }
    }
    exit 0
} catch { Write-Output ('Close Veynuq before installing, and choose an empty folder or an existing managed installation. '+$_.Exception.Message); exit 1 }
