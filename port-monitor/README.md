# PrintLink

Peer-to-peer printer sharing for small offices — without Windows printer
sharing. Print from any PC to a printer attached to another PC on the LAN,
with per-PC access grants that expire automatically.

## How it works

- Every PC runs the PrintLink tray agent (Python).
- On the PC with the printer: share it under a friendly alias.
- On any other PC: add a remote printer by entering that PC's 9-digit ID.
  The host gets an accept/refuse dialog. Access lasts 7 days (configurable)
  and can be revoked anytime.
- Sender PCs also get a **"PrintLink Remote Printer"** in the Windows print
  dialog. Pick your default remote printer in the tray, then print from any
  application as usual.

Jobs travel encrypted (AES-GCM keyed by the pairing token) directly PC-to-PC.
No cloud, no server, no accounts.

## Repository layout

```
agent/         Python tray agent (all PCs)
port-monitor/  C++ port monitor + C# registrar (sender PCs)
installer/     Inno Setup script (full / agent-only install types)
docs/          architecture.md, protocol.md
tests/         pytest suite
```

## Development setup

```bash
pip install -r agent/requirements.txt
python agent/main.py
```

Requires Windows for real use (pywin32, named pipes, port monitor). The share
logic and HTTP API are platform-independent and tested on Linux.

## Testing

```bash
pip install pytest cryptography flask requests zeroconf
python -m pytest tests/ -v
```

## Building the installer

1. `pyinstaller --onefile --noconsole --name PrintLinkAgent --icon installer/assets/icon.ico agent/main.py`
2. Build `PrintLinkMonitor.dll` (VS, x64 Release, links winspool.lib)
3. Build `PrintLinkSetup.exe` (.NET Framework 4.8)
4. `iscc installer/printlink.iss`

## Firewall

The installer opens TCP 9100 (API) and UDP 5353 (mDNS) on the Private profile
only. On domain networks, adjust via GPO.

## Security model

- Host-controlled access: nothing prints without an accepted grant.
- Grants: 7-day expiry by default, hourly enforcement, manual revocation.
- Pairing token (64 hex chars) = auth credential + encryption key.
- Payloads encrypted AES-256-GCM; tampering fails decryption.
- `/ping` ID verification prevents stale-IP impersonation after DHCP churn.

See `docs/protocol.md` for wire formats and `docs/architecture.md` for design.
