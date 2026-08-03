; Inno Setup script for Dikte on Windows.
;
; Installed for one user, into %LOCALAPPDATA%, with no administrator prompt.
; Dikte holds a global shortcut and types into other people's windows, and
; Windows will not let a program running as administrator send input to one
; that is not — so an installation that needed elevation would be an
; application that could not do its job.
;
; What it removes on uninstall is the program. Settings, history, meetings and
; downloaded models are somebody's work and their API keys; they are left where
; they are unless the box is ticked.
;
;   iscc /DVersion=1.0.0 packaging\windows\dikte.iss

#ifndef Version
  #define Version "1.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\Dikte"
#endif

#define AppName "Dikte"
#define AppPublisher "Dikte"
#define AppUrl "https://github.com/yusufipk/dikte"
#define TrayExe "DikteApp.exe"
#define CliExe "dikte.exe"

[Setup]
AppId={{7C2F5C4E-6C1B-4F55-9B4A-1B8C2E5D9A31}
AppName={#AppName}
AppVersion={#Version}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}
VersionInfoVersion={#Version}

; Per user, everywhere. PrivilegesRequired=lowest is what keeps the elevation
; prompt away; without it the whole install would run as administrator and the
; shortcut would be registered for the wrong account.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#AppName}
UsePreviousAppDir=yes
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes

OutputDir=..\..\dist
OutputBaseFilename=Dikte-{#Version}-setup
SetupIconFile=dikte.ico
UninstallDisplayIcon={app}\{#TrayExe}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Windows 10 22H2 and later: WASAPI loopback and the shortcut work further
; back, but nothing older has been tested and saying so beats finding out.
MinVersion=10.0.19045
LicenseFile=..\..\LICENSE
CloseApplications=yes
CloseApplicationsFilter=*.exe

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "turkish"; MessagesFile: "compiler:Languages\Turkish.isl"

[CustomMessages]
english.AutostartTask=Start Dikte when I sign in
english.PathTask=Add the dikte command to my PATH
english.LaunchApp=Start Dikte
english.RemoveData=Also delete my settings, history, meetings and downloaded models
english.RemoveDataTitle=Removing Dikte
english.RemoveDataSubtitle=What should be left behind?
turkish.AutostartTask=Oturum açtığımda Dikte'yi başlat
turkish.PathTask=dikte komutunu PATH'e ekle
turkish.LaunchApp=Dikte'yi başlat
turkish.RemoveData=Ayarlarımı, geçmişimi, toplantılarımı ve indirdiğim modelleri de sil
turkish.RemoveDataTitle=Dikte kaldırılıyor
turkish.RemoveDataSubtitle=Geriye ne kalsın?

[Tasks]
Name: "autostart"; Description: "{cm:AutostartTask}"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked
Name: "addtopath"; Description: "{cm:PathTask}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#TrayExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#TrayExe}"; Tasks: desktopicon

[Registry]
; The Run key is how Windows starts a program when its user signs in. Under
; HKCU, so it is this account's business and needs no administrator.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "Dikte"; ValueData: """{app}\{#TrayExe}"""; \
    Flags: uninsdeletevalue; Tasks: autostart
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: none; ValueName: "Dikte"; Flags: deletevalue uninsdeletevalue; \
    Tasks: not autostart

[Run]
Filename: "{app}\{#TrayExe}"; Description: "{cm:LaunchApp}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The one thing under {app} that is not ours to leave: PyInstaller's caches.
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]

// --- PATH, for the console half ------------------------------------------

function PathHas(const Needle: string): Boolean;
var
  Existing: string;
begin
  Result := False;
  if RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Existing) then
    Result := Pos(';' + Uppercase(Needle) + ';', ';' + Uppercase(Existing) + ';') > 0;
end;

procedure AddToPath(const Folder: string);
var
  Existing: string;
begin
  if PathHas(Folder) then
    Exit;
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Existing) then
    Existing := '';
  if (Existing <> '') and (Existing[Length(Existing)] <> ';') then
    Existing := Existing + ';';
  RegWriteExpandStringValue(HKEY_CURRENT_USER, 'Environment', 'Path',
                            Existing + Folder);
end;

procedure RemoveFromPath(const Folder: string);
var
  Existing: string;
  At: Integer;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Existing) then
    Exit;
  At := Pos(Uppercase(Folder) + ';', Uppercase(Existing + ';'));
  if At = 0 then
    Exit;
  Delete(Existing, At, Length(Folder) + 1);
  if (Existing <> '') and (Existing[Length(Existing)] = ';') then
    Delete(Existing, Length(Existing), 1);
  RegWriteExpandStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Existing);
end;

// --- uninstalling ---------------------------------------------------------

procedure DeleteUserData();
begin
  // %APPDATA%\Dikte holds config.json, %LOCALAPPDATA%\Dikte the models,
  // the history, the meetings and the recordings.
  DelTree(ExpandConstant('{userappdata}\Dikte'), True, True, True);
  DelTree(ExpandConstant('{localappdata}\Dikte'), True, True, True);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    RemoveFromPath(ExpandConstant('{app}'));
    // Asked rather than assumed, and answered no by default: what is in there
    // is somebody's dictation history and their API keys.
    if MsgBox(CustomMessage('RemoveData'), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DeleteUserData();
  end;
end;

// --- installing -----------------------------------------------------------

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if WizardIsTaskSelected('addtopath') then
      AddToPath(ExpandConstant('{app}'))
    else
      RemoveFromPath(ExpandConstant('{app}'));
  end;
end;
