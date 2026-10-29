#ifndef BundleDir
  #error BundleDir must contain a verified release bundle
#endif
#ifndef OutputDir
  #define OutputDir "..\release"
#endif
#ifndef AppVersion
  #define AppVersion "4.6.1"
#endif

[Setup]
AppId={{DAD53E66-1068-45C6-B8D4-57D0D269076A}
AppName=Veynuq
AppVersion={#AppVersion}
AppPublisher=Veynuq
AppPublisherURL=https://github.com/gabriele-gaudissard/Veynuq-Agent-App
DefaultDirName={code:DefaultDirectory}
DefaultGroupName=Veynuq
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19045
OutputDir={#OutputDir}
#ifdef TestBuild
OutputBaseFilename=Veynuq-Setup-TEST
Uninstallable=no
#else
OutputBaseFilename=Veynuq-Setup
Uninstallable=yes
#endif
SetupIconFile={#BundleDir}\assets\brand\veynuq-dark.ico
UninstallDisplayIcon={app}\assets\brand\veynuq-dark.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
CloseApplications=no
RestartApplications=no
ExtraDiskSpaceRequired=18000000000
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "italian"; MessagesFile: "compiler:Languages\Italian.isl"

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "preflight.ps1"; Flags: dontcopy

[Icons]
#ifndef TestBuild
Name: "{autodesktop}\Veynuq"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\Launcher.ps1"""; WorkingDir: "{app}"; IconFilename: "{app}\assets\brand\veynuq-dark.ico"
Name: "{autoprograms}\Veynuq\Veynuq"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\Launcher.ps1"""; WorkingDir: "{app}"; IconFilename: "{app}\assets\brand\veynuq-dark.ico"
#endif

[Run]
#ifndef TestBuild
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\Launcher.ps1"""; Description: "Launch Veynuq"; Flags: postinstall nowait skipifsilent; Check: ConfigurationSucceeded
#endif

[Code]
var ConfigurationReady: Boolean;

function ConfigurationSucceeded: Boolean;
begin
  Result := ConfigurationReady;
end;

function GetCustomSetupExitCode: Integer;
begin
  if not ConfigurationReady then Result := 1 else Result := 0;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and not ConfigurationReady then begin
    WizardForm.FinishedHeadingLabel.Caption := 'Configuration did not finish';
    WizardForm.FinishedLabel.Caption := 'Veynuq is not ready yet. Check setup-install.log in the application folder, then run Veynuq-Setup.exe again. Existing profiles and downloaded components are preserved.';
  end;
end;

function DefaultDirectory(Param: String): String;
begin
  Result := ExpandConstant('{localappdata}\Programs\Veynuq');
  if FileExists(ExpandConstant('{localappdata}\Programs\Veyq\profile.json')) then
    Result := ExpandConstant('{localappdata}\Programs\Veyq');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var ExitCode: Integer;
begin
  ExtractTemporaryFile('preflight.ps1');
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{tmp}\preflight.ps1') + '" -AppRoot "' + ExpandConstant('{app}') + '"',
    '', SW_HIDE, ewWaitUntilTerminated, ExitCode) or (ExitCode <> 0) then
    Result := 'Close Veynuq before installing. Select an empty folder or the existing managed installation; source repositories and unrelated folders are not accepted.';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var ExitCode: Integer;
begin
  if CurStep = ssPostInstall then begin
    WizardForm.StatusLabel.Caption := 'Preparing Python, dependencies, local models and configuration...';
    ConfigurationReady := Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\install.ps1') + '" -Complete',
      ExpandConstant('{app}'), SW_SHOWNORMAL, ewWaitUntilTerminated, ExitCode) and (ExitCode = 0);
    if not ConfigurationReady then
      Log('Configuration failed; app launch disabled, incomplete status and nonzero setup exit code required.');
  end;
end;
