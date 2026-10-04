; Inno Setup script for PC Cleaner.  Build with build_installer.bat (or: ISCC installer\PCCleaner.iss)
; Installs per-user by default (no admin prompt); the first page lets you choose "all users" instead.

#define AppName "PC Cleaner"
#define AppExe "PCCleaner.exe"
#define SourceExe AddBackslash(SourcePath) + "..\dist\" + AppExe
#define AppVersion GetStringFileInfo(SourceExe, "ProductVersion")

[Setup]
AppId={{6F1C2B7E-4A3D-4C8E-9B21-5D7E0A9C3F48}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=Output
OutputBaseFilename=PCCleaner-Setup-{#AppVersion}
SetupIconFile=..\assets\pccleaner.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/ultra
SolidCompression=yes
CloseApplications=yes
VersionInfoVersion={#GetVersionNumbersString(SourceExe)}
VersionInfoDescription={#AppName} Setup

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Remove the scheduled cleaning task, if one was set up.
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN PCCleaner /F"; Flags: runhidden; RunOnceId: "DeleteTask"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{userappdata}\PCCleaner');
    if DirExists(DataDir) and not UninstallSilent then
      if MsgBox('Also delete your PC Cleaner settings, cleaning reports and downloaded winapp2.ini?' + #13#10 +
                '(' + DataDir + ')', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
