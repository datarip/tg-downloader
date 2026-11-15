; Inno Setup script for the per-user TDM installer.
;
; Built by packaging/windows/package.ps1, which exports TDM_VERSION.
; The AppId is a fixed GUID: Inno Setup uses it to recognise its own
; installations, so an upgrade replaces the previous version instead of
; installing a second copy. Never change it once a release has shipped.
;
; PrivilegesRequired=lowest means no UAC prompt: the app goes into the user's
; own %LOCALAPPDATA%\Programs, and the uninstaller is per user as well.

#define AppName "TDM"
#define AppVersion GetEnv("TDM_VERSION")
#define AppExe "TDM.exe"

[Setup]
AppId={{DBA49416-9124-4D6B-9B63-A7BD3244FF33}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Datarip
AppPublisherURL=https://github.com/datarip/tg-downloader
AppSupportURL=https://github.com/datarip/tg-downloader/issues
DefaultDirName={localappdata}\Programs\TDM
DefaultGroupName=TDM
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist\release
OutputBaseFilename=TDM-{#AppVersion}-windows-x64-Setup
SetupIconFile=..\icons\tdm.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
; Everything PyInstaller produced, including LICENSE and THIRD_PARTY_NOTICES.md
; which packaging/build.py copies in beside the executable.
Source: "..\..\dist\TDM\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
