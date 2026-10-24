; Inno Setup script for the FalconSDR Windows installer - compiled by
; build.ps1 (which passes AppVersion, SourceDir and OutputDir); not meant to
; be compiled by hand from the IDE without those defines.
;
; Per-user install by default (no administrator prompt, like VS Code's user
; installer): {autopf} = %LOCALAPPDATA%\Programs. "Install for all users" is
; still offered on the first page and needs administrator rights.
; Settings and recordings live in the user's profile (~\.config\FalconSDR,
; ~\FalconSDR\recordings) and are never touched by install or uninstall.

#ifndef AppVersion
  #error AppVersion not defined - build with build.ps1
#endif

[Setup]
; Never change AppId - it is how upgrades and the uninstaller find an existing install.
AppId={{68209D1F-1233-4307-A078-67E003D19892}
AppName=FalconSDR
AppVersion={#AppVersion}
AppVerName=FalconSDR {#AppVersion}
AppPublisher=rafafunt
AppPublisherURL=https://github.com/rafafunt/FalconSDR
AppSupportURL=https://github.com/rafafunt/FalconSDR/issues
AppUpdatesURL=https://github.com/rafafunt/FalconSDR/releases
VersionInfoVersion={#AppVersion}
VersionInfoProductName=FalconSDR
DefaultDirName={autopf}\FalconSDR
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; Shown for information only (Next, nothing to accept): the GPL is not a
; contract the user has to agree to in order to install or run the program.
InfoBeforeFile={#SourcePath}\..\..\LICENSE
SetupIconFile={#SourcePath}\..\icons\falconsdr.ico
UninstallDisplayIcon={app}\falconsdr.exe
UninstallDisplayName=FalconSDR
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
; Files in use: the [Code] section below first asks a running FalconSDR to
; quit cleanly (open recordings get closed); Restart Manager is the fallback.
CloseApplications=yes
RestartApplications=no
OutputDir={#OutputDir}
OutputBaseFilename=FalconSDR-{#AppVersion}-windows-x64-setup

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"; InfoAfterFile: "{#SourcePath}\DRIVER-en.txt"
Name: "polish"; MessagesFile: "compiler:Languages\Polish.isl"; InfoAfterFile: "{#SourcePath}\DRIVER-pl.txt"

[CustomMessages]
english.OpenZadig=Open the Zadig website (RTL-SDR driver - not needed if SDR++/SDR# already works)
polish.OpenZadig=Otwórz stronę Zadig (sterownik RTL-SDR - niepotrzebny, jeśli SDR++/SDR# już działa)
english.DriverHelp=RTL-SDR driver (Zadig) - instructions
polish.DriverHelp=Sterownik RTL-SDR (Zadig) - instrukcja

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; An upgrade must not run on a mix of old and new bundled libraries.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourcePath}\DRIVER-en.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourcePath}\DRIVER-pl.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourcePath}\..\..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\FalconSDR"; Filename: "{app}\falconsdr.exe"
Name: "{autodesktop}\FalconSDR"; Filename: "{app}\falconsdr.exe"; Tasks: desktopicon
Name: "{autoprograms}\{cm:DriverHelp}"; Filename: "{app}\DRIVER-en.txt"; Languages: english
Name: "{autoprograms}\{cm:DriverHelp}"; Filename: "{app}\DRIVER-pl.txt"; Languages: polish

[Registry]
; The app's own "Start with the system" option (Settings -> Advanced) writes
; this value; never created here, only removed with the program.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "FalconSDR"; ValueType: none; Flags: uninsdeletevalue dontcreatekey

[Run]
; Started through the shell, exactly like the Start menu shortcut: a process
; Setup starts directly inherits its RedirectionTrust mitigation and then can't
; follow the Hugging Face model cache's symlinks ("Unable to open file
; 'model.bin'") until restarted. Also never runs the app elevated.
Filename: "{win}\explorer.exe"; Parameters: """{app}\falconsdr.exe"""; Description: "{cm:LaunchProgram,FalconSDR}"; Flags: nowait postinstall skipifsilent
Filename: "https://zadig.akeo.ie/"; Description: "{cm:OpenZadig}"; Flags: shellexec nowait postinstall skipifsilent unchecked

[Code]
const
  AppUrl = 'http://127.0.0.1:8765';

function AppResponds(): Boolean;
var
  Http: Variant;
begin
  Result := False;
  try
    Http := CreateOleObject('WinHttp.WinHttpRequest.5.1');
    Http.SetTimeouts(500, 500, 1000, 1000);
    Http.Open('GET', AppUrl + '/api/session', False);
    Http.Send('');
    Result := Http.Status = 200;
  except
  end;
end;

{ A running FalconSDR has no window to close - ask it over its own API, the
  same as the UI's Quit, so open recordings are finished properly
  (a password-protected install refuses; Restart Manager then takes over). }
procedure QuitRunningApp();
var
  Http: Variant;
  I: Integer;
begin
  if not AppResponds() then
    Exit;
  try
    Http := CreateOleObject('WinHttp.WinHttpRequest.5.1');
    Http.SetTimeouts(1000, 1000, 2000, 2000);
    Http.Open('POST', AppUrl + '/api/quit', False);
    Http.SetRequestHeader('X-FalconSDR', '1');
    Http.SetRequestHeader('Content-Type', 'application/json');
    Http.Send('{}');
  except
    Exit;
  end;
  for I := 1 to 40 do
  begin
    Sleep(250);
    if not AppResponds() then
      Break;
  end;
  Sleep(1000);  // the process exits a moment after its web server stops
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  QuitRunningApp();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  QuitRunningApp();
  Result := True;
end;
