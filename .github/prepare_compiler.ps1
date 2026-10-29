$ErrorActionPreference='Stop'
$version='6.7.4'
$destination=Join-Path $env:RUNNER_TEMP 'veynuq-inno'
$download=Join-Path $env:RUNNER_TEMP 'veynuq-inno-setup.exe'
Invoke-WebRequest "https://github.com/jrsoftware/issrc/releases/download/is-6_7_4/innosetup-$version.exe" -OutFile $download
if ((Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash -ne 'A549929319F9120716677C685535CA4671E4B937B5FDC87E2585F32D8EB30E8C') { throw 'Compiler checksum mismatch.' }
$signature=Get-AuthenticodeSignature -LiteralPath $download
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Pyrsys B.V.') { throw 'Compiler publisher verification failed.' }
$process=Start-Process $download -ArgumentList ('/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER /DIR="'+$destination+'"') -WindowStyle Hidden -Wait -PassThru
if ($process.ExitCode -ne 0) { throw 'Compiler installation failed.' }
('VEYNUQ_ISCC='+(Join-Path $destination 'ISCC.exe')) | Out-File -LiteralPath $env:GITHUB_ENV -Append -Encoding utf8
