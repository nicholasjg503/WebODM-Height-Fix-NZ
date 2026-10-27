; Inno Setup script - builds dist\WebODM-Height-Fix-NZ-Setup-<version>.exe from dist\WebODMHeightFixNZ\.
; Run through scripts\build.ps1, which passes /DAppVersion=<version>.

#define AppName "WebODM Height Fix NZ"
#define AppExe "WebODMHeightFixNZ.exe"
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{C3D5B695-C319-4AE6-8381-07DD0F03B39A}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=WebODM Height Fix NZ contributors
AppPublisherURL=https://github.com/nicholasjg503/WebODM-Height-Fix-NZ
AppSupportURL=https://github.com/nicholasjg503/WebODM-Height-Fix-NZ/issues
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Installs for the current user without needing admin; the user can choose all users instead.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=WebODM-Height-Fix-NZ-Setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; clear out the previous version's libraries so upgrades start clean
Type: filesandordirs; Name: "{app}\_internal"
; leftovers from before the app was renamed (it was "NZ Height Fix", NZHeightFix.exe)
Type: files; Name: "{app}\NZHeightFix.exe"
Type: files; Name: "{autoprograms}\NZ Height Fix.lnk"
Type: files; Name: "{autodesktop}\NZ Height Fix.lnk"

[Files]
Source: "..\dist\WebODMHeightFixNZ\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
; the app's own updater runs this installer silently with /RELAUNCH=1: reopen the app after
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: RelaunchAfterUpdate

[Code]
function RelaunchAfterUpdate: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:RELAUNCH|0}') = '1');
end;
