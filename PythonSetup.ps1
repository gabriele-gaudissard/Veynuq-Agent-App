param([switch]$ProbeOnly, [string]$TargetRoot = $PSScriptRoot)
$ErrorActionPreference = 'Stop'

function Get-LatestPythonRelease([string]$Series = '') {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $page = Invoke-WebRequest -Uri 'https://www.python.org/downloads/windows/' -UseBasicParsing -TimeoutSec 30
    if ($Series) {
        if ($Series -ne '3.13') { throw 'Unsupported application runtime series.' }
        $versions = [regex]::Matches($page.Content, 'Python\s+(3\.13\.\d+)(?![\da-z])') | ForEach-Object { $_.Groups[1].Value } | Sort-Object { [version]$_ } -Descending -Unique
        if (-not $versions) { throw 'The official compatible Python release could not be identified.' }
        $version = @($versions)[0]
    } else {
        $match = [regex]::Match($page.Content, 'Latest Python 3 Release\s*-\s*Python\s+(\d+\.\d+\.\d+)')
        if (-not $match.Success) { throw 'The official stable Python release could not be identified. Nothing was installed.' }
        $version = $match.Groups[1].Value
    }
    $architecture = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    $suffix = switch ($architecture) { 'ARM64' { '-arm64' } 'AMD64' { '-amd64' } 'x86' { '' } default { throw 'Unsupported Windows architecture.' } }
    return @{ version = $version; architecture = $architecture; url = "https://www.python.org/ftp/python/$version/python-$version$suffix.exe" }
}

function Get-PythonInfo([string]$Executable) {
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) { return $null }
    try {
        $probe = "import sys,json,platform,struct; print(json.dumps(dict(version='.'.join(map(str,sys.version_info[:3])),stable=sys.version_info.releaselevel=='final',architecture=platform.machine().upper(),bits=struct.calcsize('P')*8,executable=sys.executable)))"
        $result = & $Executable -I -c $probe 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        return ($result | ConvertFrom-Json)
    } catch { return $null }
}

function Find-CurrentPython($Release, [string]$Root) {
    $candidates = [Collections.Generic.List[string]]::new()
    $candidates.Add((Join-Path $Root ('.veyq-python\' + $Release.version + '\python.exe')))
    foreach ($name in @('python.exe','python3.exe')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command -and $command.Source -notmatch '\\WindowsApps\\') { $candidates.Add($command.Source) }
    }
    foreach ($registryRoot in @('HKCU:\Software\Python\PythonCore','HKLM:\Software\Python\PythonCore')) {
        if (Test-Path -LiteralPath $registryRoot) {
            foreach ($entry in Get-ChildItem -LiteralPath $registryRoot) {
                $install = Get-Item -LiteralPath ($entry.PSPath + '\InstallPath') -ErrorAction SilentlyContinue
                if ($install) {
                    $exe = $install.GetValue('ExecutablePath')
                    if ($exe) { $candidates.Add($exe) }
                    elseif ($install.GetValue('')) { $candidates.Add((Join-Path $install.GetValue('') 'python.exe')) }
                }
            }
        }
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        $info = Get-PythonInfo $candidate
        $seriesMatches = -not $Release.series -or ($info -and $info.version.StartsWith($Release.series + '.'))
        if ($info -and $info.stable -and $seriesMatches -and [version]$info.version -ge [version]$Release.version -and $info.architecture -eq $Release.architecture -and $info.bits -eq $(if ($Release.architecture -eq 'x86') {32} else {64})) {
            return $info.executable
        }
    }
    return $null
}

function Install-CurrentPython([string]$Root, [string]$Series = '') {
    Write-Host '[1/4] Checking the latest stable Python release...'
    $release = Get-LatestPythonRelease $Series
    if ($Series) { $release.series = $Series }
    $existing = Find-CurrentPython $release $Root
    if ($existing) { Write-Host 'Python is already up to date. Skipping Python installation.'; return $existing }
    $destination = [IO.Path]::GetFullPath((Join-Path $Root ('.veyq-python\' + $release.version)))
    $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    if (-not $destination.StartsWith($rootPath + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid Python installation destination.' }
    $download = Join-Path ([IO.Path]::GetTempPath()) ('veynuq-python-' + [guid]::NewGuid().ToString('N') + '.exe')
    try {
        Write-Host ('Downloading Python ' + $release.version + ' from python.org...')
        Invoke-WebRequest -Uri $release.url -OutFile $download -UseBasicParsing -TimeoutSec 180
        # A parent PowerShell 7 process can pass its PSModulePath to Windows
        # PowerShell. Load the matching OS module rather than that inherited path.
        if ($PSVersionTable.PSEdition -eq 'Desktop') {
            Import-Module (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
        }
        $signature = Get-AuthenticodeSignature -LiteralPath $download
        if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') { throw 'Python installer signature is invalid or has an unexpected publisher.' }
        # The PSF installer may upgrade an existing installation in the same
        # minor series. Keep other series, PATH, associations and launcher.
        $arguments = '/quiet InstallAllUsers=0 TargetDir="' + $destination + '" Include_launcher=0 InstallLauncherAllUsers=0 AssociateFiles=0 PrependPath=0 Include_test=0 Shortcuts=0 Include_doc=0'
        $process = Start-Process -FilePath $download -ArgumentList $arguments -WindowStyle Hidden -PassThru
        if (-not $process.WaitForExit(600000)) { Stop-Process -Id $process.Id -ErrorAction SilentlyContinue; throw 'Python installation timed out. No app runtime was switched.' }
        if ($process.ExitCode -notin @(0,3010)) { throw ('Python installation failed: ' + $process.ExitCode) }
        $python = Join-Path $destination 'python.exe'
        $info = Get-PythonInfo $python
        if (-not $info) {
            # Same-series patch upgrades can retain the registered install path
            # instead of TargetDir. Probe the actual installed interpreter.
            $python = Find-CurrentPython $release $Root
            if ($python) { $info = Get-PythonInfo $python }
        }
        if (-not $info -or -not $info.stable -or $info.version -ne $release.version -or $info.architecture -ne $release.architecture) { throw 'Installed Python validation failed.' }
        return $python
    } finally {
        if (Test-Path -LiteralPath $download) { Remove-Item -LiteralPath $download }
    }
}

if ($MyInvocation.InvocationName -eq '.') { return }
if ($ProbeOnly) {
    $release = Get-LatestPythonRelease
    @{ latest = $release.version; architecture = $release.architecture; existing = (Find-CurrentPython $release $TargetRoot); download = $release.url } | ConvertTo-Json
} else {
    Install-CurrentPython $TargetRoot
}
