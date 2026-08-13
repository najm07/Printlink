# PrintLink Architecture

## Overview

PrintLink is a peer-to-peer LAN print relay. Every PC runs the same Python tray
agent (sender + receiver roles). PCs with physical printers share them by alias;
other PCs request access by ID, print through a virtual Windows printer, and jobs
are relayed over authenticated HTTP to the host, which prints locally.

No central server. Discovery via mDNS. Access via pairwise grants with pairing
tokens and automatic expiry (default 7 days, manually revocable).

## Components

```
+--------------------------- PC (any) ----------------------------+
|  tray.py        pystray icon + Tkinter dialogs (main thread)    |
|  server.py      Flask receiver API :9100      (daemon thread)   |
|  sender.py      HTTP client + retry queue     (daemon thread)   |
|  discovery.py   mDNS advertise/resolve        (zeroconf thread) |
|  pipe_reader.py named-pipe job intake         (daemon thread)   |
|  main.py        wiring, hourly expiry sweeper (daemon thread)   |
|  db.py          SQLite: shared_printers / grants / remote_printers |
|  identity.py    persistent 9-digit PC ID                        |
|  shares.py      grant lifecycle (pure logic)                    |
|  crypto.py      AES-GCM payload encryption keyed by token       |
|  printer_local.py  win32print: enumerate/status/print           |
|  config.py      all ports/paths/tunables                        |
+-----------------------------------------------------------------+

port-monitor/ (C++/C# shim, sender PCs only)
  PrintLinkMonitor.dll  MONITOR2 port monitor -> \\.\pipe\PrintLinkSender
  PrintLinkSetup.exe    registers monitor, port, "PrintLink Remote Printer"
```

## Data flows

### Share request (pairing)
1. Client tray: enter host ID + alias + days.
2. `sender.py` resolves ID via mDNS, verifies `/ping` ID matches.
3. POST `/request-share` -> host tray shows accept/refuse dialog.
4. On accept: `create_grant()` issues token + expiry; client stores it
   in `remote_printers`.

### Print job
```
Any app -> "PrintLink Remote Printer" (Microsoft XPS driver)
  -> spooler -> PrintLinkMonitor.dll
  -> \\.\pipe\PrintLinkSender (framed)
  -> pipe_reader.py -> %TEMP%/printlink_outbox/
  -> Sender.print_file(host_id, alias)  [tray-selected default]
  -> POST /print (X-Sender-ID, X-Token, AES-GCM payload)
  -> host authorize_print() (token + expiry + status)
  -> printer_local.print_via_shell() -> physical printer
```

### Expiry
- Host: hourly `sweep_expired_grants()` marks overdue grants `expired`;
  `authorize_print()` enforces on every job.
- Client: sweeper expires `remote_printers` rows locally so the tray
  shows honest status; revoked shares fail with 403.

## Key design decisions

- **One virtual printer, tray-selected destination** instead of one Windows
  printer per remote (avoids admin rights per share).
- **XPS as wire/render format** via the inbox Microsoft driver: no custom
  driver, no driver signing; host needs an XPS/PDF handler (SumatraPDF
  recommended).
- **Port monitor over custom driver**: spooler does all rendering; our native
  code is a ~150-line stateless pipe forwarder.
- **Token as encryption key**: pairing token doubles as AES-GCM key — no PKI,
  no key exchange, revoking the grant kills both auth and confidentiality.
- **Stateless C++ monitor**: crashes in Python cannot corrupt the spooler.
- **mDNS + IP cache fallback**: resolver updates `host_ip` on each success;
  stored IP used when mDNS is blocked (VLANs/switches).
- **User-session tray agent** (not a Windows Service): dialogs and tray icon
  require the interactive session; auto-start via HKCU Run key.

## Failure handling

| Failure | Behavior |
|---|---|
| Host offline | Sender retry queue: every 15s, 20 attempts (~5 min) |
| Share expired/revoked | 403; client pre-check shows "request it again" |
| Printer offline/paper-out | Host pre-check returns 503 with reason |
| Python agent down | Port monitor waits 20s on pipe, then job fails in queue |
| mDNS blocked | Stored host_ip fallback; ID verified via /ping |
| Reinstall | identity.json + DB preserved -> same PC ID, grants intact |
