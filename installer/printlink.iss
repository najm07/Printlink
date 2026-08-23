; PrintLink installer — Inno Setup 6 script
; Single install type: the tray agent.
;
; The version is defined by agent/config.py (single source of truth).
; scripts/build.ps1 passes /DMyAppVersion=<v>; the fallback below only exists
; so `iscc printlink.iss` still works for quick local builds.
;
; Prerequisites on build machine:
;   dist\PrintLinkAgent.exe   (PyInstaller onefile bundle, see PrintLinkAgent.spec)
;
; Build:  pwsh scripts\build.ps1      (or: iscc printlink.iss)

#ifndef MyAppVersion
  #define MyAppVersion "0.4.0"
#endif
#define MyAppName      "PrintLink"
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
; Replace any running (stale) agent with the freshly installed binary:
; a reinstall alone does NOT unload the old process from memory.
; The Start-Process here is the ONE guaranteed launch (it also covers
; silent upgrades, where the postinstall entry below is skipped).
Filename: "powershell"; Parameters: "-NoProfile -Command ""Stop-Process -Name PrintLinkAgent -Force -ErrorAction SilentlyContinue; Start-Process '{app}\{#MyAppExeName}'"""; \
    Flags: runhidden waituntilterminated; StatusMsg: "Restarting PrintLink agent..."
; Register the right-click "Print with PrintLink" verb (HKLM, all users)
Filename: "{app}\{#MyAppExeName}"; Parameters: "--install-verbs"; \
    Flags: runhidden waituntilterminated; StatusMsg: "Registering Print with PrintLink..."
; Shared machine-wide data dir: all user accounts see the same printers.
; Grant Users write access + migrate the installing admin's existing data.
Filename: "powershell"; Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""New-Item -ItemType Directory -Force -Path $env:ProgramData\PrintLink | Out-Null; icacls $env:ProgramData\PrintLink /grant Users:(OI)(CI)M /T /Q; $src = Join-Path $env:LOCALAPPDATA 'PrintLink'; if ((Test-Path $src) -and -not (Test-Path (Join-Path $env:ProgramData 'PrintLink\printlink.db'))) Copy-Item (Join-Path $src '*') (Join-Path $env:ProgramData 'PrintLink') -Recurse -Force -ErrorAction SilentlyContinue"""; \
    Flags: runhidden waituntilterminated; StatusMsg: "Preparing shared PrintLink data..."
; Open the agent's TCP port in Windows Firewall (inbound, Private profile)
Filename: "netsh"; Parameters: "advfirewall firewall add rule name=""PrintLink Agent"" dir=in action=allow protocol=TCP localport=9100 profile=private"; \
    Flags: runhidden waituntilterminated; StatusMsg: "Opening firewall port 9100..."
; mDNS (UDP 5353) is usually allowed by default on Private profile, but be explicit
Filename: "netsh"; Parameters: "advfirewall firewall add rule name=""PrintLink mDNS"" dir=in action=allow protocol=UDP localport=5353 profile=private"; \
    Flags: runhidden waituntilterminated
; Manual launch is OPT-IN: the agent was already started above — leaving
; this ticked used to run a SECOND instance (duplicate tray icon, mDNS
; name collision, port bind fight).
Filename: "{app}\{#MyAppExeName}"; Description: "Start PrintLink now (already running — only tick this if you stopped it)"; \
    Flags: nowait postinstall skipifsilent unchecked

[UninstallRun]
; Remove the right-click verb (HKLM first, then HKCU fallback — same order
; the installer's --install-verbs wrote them)
Filename: "{app}\{#MyAppExeName}"; Parameters: "--uninstall-verbs"; \
    Flags: runhidden waituntilterminated
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""PrintLink Agent"""; \
    Flags: runhidden waituntilterminated
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""PrintLink mDNS"""; \
    Flags: runhidden waituntilterminated

[UninstallDelete]
; Remove the agent binary; keep user data (identity + DB) so re-installs
; keep the same PC ID — delete manually from %PROGRAMDATA%\PrintLink if desired
Type: files; Name: "{app}\{#MyAppExeName}"
; Drop the persisted send-target preference; it is a per-user setting again
; since 0.3 (lives next to the private token db)
Type: files; Name: "{localappdata}\PrintLink\target.json"

