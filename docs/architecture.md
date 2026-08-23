# PrintLink Architecture

## Overview

PrintLink is a peer-to-peer LAN print relay. Every PC runs the same Python tray
agent (sender + receiver roles). PCs with physical printers share them by
alias; other PCs request access by ID. To print, a user either right-clicks a
document ("Print with PrintLink") or picks "Send document..." in the tray; a
preview dialog chooses the target remote printer and print options, and the
file is delivered directly to the host agent over authenticated HTTP, which
prints it locally by format.

No central server, no virtual printer, no port monitor. Discovery via mDNS.
Access via pairwise grants with pairing tokens and automatic expiry (default
7 days, manually revocable).

## Components

```
+--------------------------- PC (any) ----------------------------+
|  tray.py        pystray icon + Tkinter dialogs (main thread)    |
|  preview.py     print-options + target picker dialog (Tk)       |
|  server.py      Flask receiver API :9100      (daemon thread)   |
|  sender.py      HTTP client + retry queue     (daemon thread)   |
|  discovery.py   mDNS advertise/resolve        (zeroconf thread) |
|  main.py        wiring, hourly expiry sweeper (daemon thread)   |
|  db.py          SQLite: shared_printers / grants / remote_printers |
|  identity.py    persistent 9-digit PC ID                        |
|  shares.py      grant lifecycle (pure logic)                    |
|  crypto.py      AES-GCM payload encryption keyed by token       |
|  printer_local.py  host-side printing (pywin32, SumatraPDF,     |
|                    Word COM, GDI+); win32 imports are lazy so   |
|                    the module imports on any OS                 |
|  config.py      all ports/paths/tunables                        |
+-----------------------------------------------------------------+
```

Since 0.2.3 all agent data lives machine-wide in `%PROGRAMDATA%\PrintLink` so
every Windows account on a PC sees the same printers, grants, and identity
(falls back to per-user `%LOCALAPPDATA%` when not writable). See
`docs/security.md` for the threat model.

## Data flows

### Share request (pairing)
1. Client tray: enter host ID + alias + friendly name + days.
2. `sender.py` resolves ID via mDNS, verifies `/ping` ID matches.
3. POST `/request-share` -> host tray shows accept/refuse dialog.
4. On accept: `create_grant()` issues token + expiry; client stores it
   in `remote_printers`.
5. Either side can manage entries: rename/remove remote printers (removing
   tells the host to revoke), rename/unshare shared printers, revoke/extend
   grants — all from tray menus.

### Print job (direct send)
```
Any app -> "Print with PrintLink" verb or tray -> "Send document..."
   -> preview dialog: target printer + copies/pages/paper/color/duplex/fit
   -> Sender.print_file(host_id, alias)  [persisted last target as default]
   -> POST /print (X-Sender-ID, X-Token, AES-GCM payload)
   -> host authorize_print() (token + expiry + status)
   -> host dispatches by format:
        PDF    -> PyMuPDF page-range subset + SumatraPDF -print-settings
        Word   -> Word COM automation (copies, page range)
        XLSX/PPTX -> shell "printto" verb
        images/EMF -> GDI+ via PowerShell (System.Drawing)
        text   -> spooler TEXT datatype
```

### Expiry
- Host: hourly `sweep_expired_grants()` marks overdue grants `expired`;
  `authorize_print()` enforces on every job.
- Client: sweeper expires `remote_printers` rows locally so the tray
  shows honest status; revoked shares fail with 403.

## Key design decisions

- **Direct send with a preview dialog** instead of a virtual printer: no
  drivers, no admin rights per share, and the user picks target + options
  before anything leaves the machine.
- **Host-side per-format rendering** (PyMuPDF/SumatraPDF, Word COM, GDI+,
  spooler TEXT) — the receiver applies the job's options natively.
- **Token as encryption key**: pairing token doubles as AES-GCM key — no PKI,
  no key exchange; revoking the grant kills both auth and confidentiality.
- **mDNS + IP cache fallback**: resolver updates `host_ip` on each success;
  stored IP used when mDNS is blocked (VLANs/switches).
- **User-session tray agent** (not a Windows Service): dialogs and tray icon
  require the interactive session; auto-start via Run key.
- **Machine-wide shared data dir** (`%PROGRAMDATA%\PrintLink`): one identity
  and one set of printers per PC, so any user account can print with the
  right-click verb. Per-user fallback preserves correctness if unwritable.
- **Shell-verb in HKLM** (with HKCU fallback) so the verb exists for every
  account, registered by the elevated installer.

## Failure handling

| Failure | Behavior |
|---|---|
| Host offline | Sender retry queue: every 15s, 20 attempts (~5 min) |
| Share expired/revoked | 403; client pre-check shows "request it again" |
| Printer offline/paper-out | Host pre-check returns 503 with reason |
| No target/printers resolved | Verb shows a warning dialog (no silent failure) |
| mDNS blocked | Stored host_ip fallback; ID verified via /ping |
| Reinstall | identity.json + DB preserved -> same PC ID, grants intact |