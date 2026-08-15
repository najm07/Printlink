; PrintLink installer — Inno Setup 6 script
; Single install type: the tray agent. The port monitor / virtual printer
; (0.1.x) was removed — jobs are now delivered directly by the agent via the
; right-click verb + preview dialog.
;
; Prerequisites on build machine:
;   dist\PrintLinkAgent.exe   (PyInstaller onefile bundle, see PrintLinkAgent.spec)
;
; Build:  iscc printlink.iss

#define MyAppName      "PrintLink"
#define MyAppVersion   "0.2.0"
#define MyAppPublisher "PrintLink"
#define MyAppExeName   "PrintLinkAgent.exe"

[Setup]
AppId={{A08329F2-0C45-4288-94A7-532B183C453B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
PrivilegesRequired=admin
OutputDir=..\installer\output
OutputBaseFilename=PrintLinkSetup-{#MyAppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
WizardStyle=modern
CloseApplications=force
RestartApplications=yes

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "assets\icon.ico";         DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

[Registry]
; Auto-start the tray agent at login (per-user, no admin needed at runtime)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "PrintLink"; ValueData: """{app}\{#MyAppExeName}"""; \
    Flags: uninsdeletevalue

[Run]
; Register the right-click "Print with PrintLink" verb (HKCU, per-user)
Filename: "{app}\{#MyAppExeName}"; Parameters: "--install-verbs"; \
    Flags: runhidden waituntilterminated; StatusMsg: "Registering Print with PrintLink..."
; Open the agent's TCP port in Windows Firewall (inbound, Private profile)
Filename: "netsh"; Parameters: "advfirewall firewall add rule name=""PrintLink Agent"" dir=in action=allow protocol=TCP localport=9100 profile=private"; \
    Flags: runhidden waituntilterminated; StatusMsg: "Opening firewall port 9100..."
; mDNS (UDP 5353) is usually allowed by default on Private profile, but be explicit
Filename: "netsh"; Parameters: "advfirewall firewall add rule name=""PrintLink mDNS"" dir=in action=allow protocol=UDP localport=5353 profile=private"; \
    Flags: runhidden waituntilterminated
; Launch the agent after install
Filename: "{app}\{#MyAppExeName}"; Description: "Start PrintLink now"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
; Remove the right-click verb (HKCU, per-user)
Filename: "{app}\{#MyAppExeName}"; Parameters: "--uninstall-verbs"; \
    Flags: runhidden waituntilterminated
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""PrintLink Agent"""; \
    Flags: runhidden waituntilterminated
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""PrintLink mDNS"""; \
    Flags: runhidden waituntilterminated

[UninstallDelete]
; Remove the agent binary; keep user data (identity + DB) so re-installs
; keep the same PC ID — delete manually from %LOCALAPPDATA%\PrintLink if desired
Type: files; Name: "{app}\{#MyAppExeName}"
; Drop the persisted send-target preference (re-pick in the tray after reinstall)
Type: files; Name: "{localappdata}\PrintLink\target.json"
