; What the Windows download is: the directory PyInstaller built, wrapped in the
; setup program Windows expects. Run it through build-windows.ps1, which draws
; the icon, builds that directory, puts an ffmpeg in it and passes the version
; in; ISCC on its own has none of that.
;
; Per user rather than per machine. It keeps the whole thing out of the way of
; the administrator prompt, which for something a person is trying out is the
; difference between a download and a phone call to whoever owns the laptop,
; and nothing here writes outside the account anyway.

#ifndef Version
  #define Version "0.0.0"
#endif
#ifndef Source
  #define Source "..\build\dist\dikte"
#endif
#ifndef Icon
  #define Icon "..\build\Dikte.ico"
#endif

[Setup]
; The identifier Add/Remove Programs files this under, and what an update
; recognises the older installation by. The same one the Mac's login item and
; the bundle use, and like those it never changes.
AppId=io.github.yusufipk.dikte
AppName=Dikte
AppVersion={#Version}
AppPublisher=Yusuf Ipek
AppSupportURL=https://github.com/yusufipk/dikte
DefaultDirName={localappdata}\Programs\Dikte
DefaultGroupName=Dikte
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=Dikte-{#Version}-x64-setup
SetupIconFile={#Icon}
UninstallDisplayIcon={app}\Dikte.exe
WizardStyle=modern
; Most of the download is Qt and ffmpeg, both of which compress well, and the
; slower setting is a minute of a build machine's time against a smaller file
; for everybody who downloads it.
Compression=lzma2/max
SolidCompression=yes
; An update over a running Dikte would otherwise fail on the executable it
; cannot replace. Restart Manager closes it and starts it again afterwards.
CloseApplications=yes
RestartApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; On by default: Dikte is a tray application holding a global shortcut, and one
; that is not running when you press the key is one that does nothing.
Name: "autostart"; Description: "Start Dikte when I sign in"

[Files]
Source: "{#Source}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[InstallDelete]
; A checkout's install.ps1 -Autostart is a shortcut in the Startup folder. The
; registry entry this setup writes replaces it, and both left in place would be
; two Diktes at every sign-in. Only under the autostart task: somebody who
; unticked the box has not asked for their checkout's entry to go.
Type: files; Name: "{userstartup}\Dikte.lnk"; Tasks: autostart

[Icons]
Name: "{autoprograms}\Dikte"; Filename: "{app}\Dikte.exe"

[Registry]
; Starting at sign-in, as a registry value rather than a shortcut in the
; Startup folder: it is the one place the setup program, the uninstaller and
; `dikte integrate` can all read and write without a COM library between them.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "Dikte"; ValueData: """{app}\Dikte.exe"""; \
    Flags: uninsdeletevalue; Tasks: autostart
; And taking it away again, for an update where the box was unticked. Both
; lines delete on uninstall, so an entry `dikte integrate` wrote later goes
; too, whichever way it got there.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: none; ValueName: "Dikte"; \
    Flags: deletevalue uninsdeletevalue; Tasks: not autostart

[Run]
Filename: "{app}\Dikte.exe"; Description: "Start Dikte"; \
    Flags: nowait postinstall skipifsilent

[Code]
{ The `dikte` command. WindowsApps is already on the user's PATH, so a .cmd
  left there runs from any terminal without touching the PATH and without an
  administrator; the alternative is an environment variable edit that every
  open terminal misses. It names dikte-cli.exe, the console executable, which
  is the one that can print to the terminal it was typed in. }

function ShimDir(): String;
begin
  Result := ExpandConstant('{localappdata}\Microsoft\WindowsApps');
end;

function ShimPath(): String;
begin
  Result := ShimDir() + '\dikte.cmd';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Shim: String;
begin
  if CurStep = ssPostInstall then begin
    if DirExists(ShimDir()) then begin
      Shim := '@echo off' + #13#10
            + '"' + ExpandConstant('{app}\dikte-cli.exe') + '" %*' + #13#10;
      SaveStringToFile(ShimPath(), Shim, False);
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Shim: AnsiString;
begin
  { Only the shim this setup wrote, which is the one naming its dikte-cli.exe.
    install.ps1 writes the same file for a checkout, naming that checkout's
    Python, and a shim somebody else wrote is not this uninstaller's to take. }
  if CurUninstallStep = usUninstall then
    if LoadStringFromFile(ShimPath(), Shim)
       and (Pos(ExpandConstant('{app}\dikte-cli.exe'), Shim) > 0) then
      DeleteFile(ShimPath());
end;
