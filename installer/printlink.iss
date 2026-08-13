; PrintLink installer — Inno Setup 6 script
; Builds two installation modes from a single script:
;   - "Full"   : agent + port monitor + virtual printer  (sender PCs)
;   - "Agent only" : tray agent only                     (printer host PCs)
;
; Prerequisites on build machine:
;   dist\PrintLinkAgent\PrintLinkAgent.exe   (PyInstaller --onefile bundle of agent/)
;   port-monitor\PrintLinkMonitor\x64\Release\PrintLinkMonitor.dll
;   port-monitor\PrintLinkSetup\bin\Release\net48\PrintLinkSetup.exe
;
; Build:  iscc printlink.iss

#define MyAppName      "PrintLink"
#define MyAppVersion   "0.1.1"
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

[Types]
Name: "full";  Description: "Full installation (agent + virtual printer)"
Name: "agent"; Description: "Agent only (PC with a physical printer)"

[Components]
Name: "agent";   Description: "PrintLink tray agent (required)"; Types: full agent; Flags: fixed
Name: "monitor"; Description: "Virtual printer (PrintLink Remote Printer)"; Types: full

[Files]
; --- tray agent (PyInstaller bundle) ---
Source: "..\dist\PrintLinkAgent\{#MyAppExeName}"; DestDir: "{app}"; Components: agent; Flags: ignoreversion
Source: "assets\icon.ico";                        DestDir: "{app}"; Components: agent; Flags: ignoreversion

; --- port monitor: DLL goes to System32 (spooler requirement) ---
; PrintLinkSetup.exe needs the DLL next to itself: it copies it to System32
; before calling AddMonitor (the spooler only loads monitors from there).
; The System32 copy is done BY PrintLinkSetup.exe with the spooler stopped,
; because spoolsv locks the DLL while it is loaded — a raw [Files] copy into
; {sys} would abort with "file in use" on upgrade/repair.
Source: "..\port-monitor\PrintLinkMonitor\x64\Release\PrintLinkMonitor.dll"; DestDir: "{app}\setup"; Components: monitor; Flags: ignoreversion
Source: "..\port-monitor\PrintLinkSetup\bin\Release\net48\PrintLinkSetup.exe"; DestDir: "{app}\setup"; Components: monitor; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

[Registry]
; Auto-start the tray agent at login (per-user, no admin needed at runtime)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "PrintLink"; ValueData: """{app}\{#MyAppExeName}"""; \
    Components: agent; Flags: uninsdeletevalue

[Run]
; Register port monitor + create the virtual printer (needs admin; we have it)
Filename: "{app}\setup\PrintLinkSetup.exe"; Parameters: "install"; \
    Components: monitor; Flags: runhidden waituntilterminated; \
    StatusMsg: "Registering PrintLink virtual printer..."
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
; Remove the virtual printer + monitor first (spooler may need a restart)
Filename: "{app}\setup\PrintLinkSetup.exe"; Parameters: "uninstall"; \
    Flags: runhidden waituntilterminated; Components: monitor
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""PrintLink Agent"""; \
    Flags: runhidden waituntilterminated
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""PrintLink mDNS"""; \
    Flags: runhidden waituntilterminated

[UninstallDelete]
; Remove the agent binary; keep user data (identity + DB) so re-installs
; keep the same PC ID — delete manually from %LOCALAPPDATA%\PrintLink if desired
Type: files; Name: "{app}\{#MyAppExeName}"

[Code]
// Printer/monitor cleanup is done by PrintLinkSetup.exe (see [UninstallRun]),
// which needs the spooler RUNNING to delete the printer and stops the spooler
// itself for the monitor registration + System32 DLL removal.
// Here we only make a final best-effort pass at the DLL once everything else
// is gone and the app dir is being removed.
function InitializeUninstall(): Boolean;
begin
    Result := MsgBox('Remove PrintLink? The virtual printer will be deleted ' +
                     'and the print spooler will be restarted.',
                     mbConfirmation, MB_YESNO) = IDYES;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var ResultCode: Integer;
begin
    if CurUninstallStep = usPostUninstall then
    begin
        Exec('net', 'stop spooler /y', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
        DeleteFile(ExpandConstant('{sys}\PrintLinkMonitor.dll'));
        Exec('net', 'start spooler', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    end;
end;