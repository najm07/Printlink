# PrintLink

[![CI](https://github.com/najm07/Printlink/actions/workflows/ci.yml/badge.svg)](https://github.com/najm07/Printlink/actions/workflows/ci.yml)

Website: <https://najm07.github.io/Printlink/>

Peer-to-peer LAN printer sharing for small offices — no Windows printer
sharing, no cloud, no accounts. Print from any PC to a printer attached to
another PC on the LAN, with per-PC access grants that expire automatically.

## How it works

- Every PC runs the PrintLink tray agent.
- On the PC with the printer: share it under a friendly alias.
- On any other PC: add a remote printer by entering that PC's 9-digit ID.
  The host gets an accept/refuse dialog. Access lasts 7 days (configurable)
  and can be revoked anytime. When you add a printer you give it a friendly
  name (shown as `alias @ name`, e.g. `CANON @ Lina's PC`), so the lists stay
  readable even with many printers — rename or remove entries any time from
  "Manage remote printers..." (removing also tells the host to revoke your
  grant).
- Send a document with **right-click → "Print with PrintLink"** or the tray
  menu → "Send document...". A preview dialog lets you pick the target
  printer and print options before anything leaves your machine.

```
Your document -> preview dialog (printer, copies, pages, paper, color,
                 duplex, orientation, fit)
   -> sender agent -> AES-GCM over HTTP :9100 -> host agent -> printer
```

No virtual printer and no port monitor: jobs are delivered directly between
agents and handed to the physical printer by format:

| Format        | Receiver handling                                   |
| ------------- | --------------------------------------------------- |
| PDF           | PyMuPDF page-range subset + SumatraPDF (or printto) |
| Word (docx/doc)| Word COM automation (copies, page range)            |
| Excel/PPTX    | shell printto verb                                  |
| PNG/JPEG/GIF/BMP/WEBP/TIFF/EMF | GDI+ via PowerShell            |
| Text          | spooler TEXT datatype                               |

Jobs travel encrypted (AES-GCM keyed by the pairing token) directly PC-to-PC.

## Repository layout

```
agent/         Python tray agent (all PCs)
installer/     Inno Setup script (agent-only install)
docs/          architecture.md, protocol.md, security.md, debugging-notes.md
tests/         pytest suite
```

## Development setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python agent/main.py
```

Requires Windows for real use (pywin32, spooler/COM/GDI+ printing). The
share/identity logic and HTTP API are platform-independent and covered by
tests that run on any OS.

## Testing

```powershell
python -m pytest tests/
```

## Building the installer

One command (reads the version from `agent/config.py` — no sync needed):

```powershell
pwsh scripts/build.ps1
```

Manual equivalent:

1. Bundle the agent:

   ```powershell
   python -m PyInstaller --noconfirm PrintLinkAgent.spec
   ```

   Produces `dist\PrintLinkAgent.exe` (onefile, windowed, ~52 MB; the spec
   pins the excludes so the build stays fast and small).

2. Build the installer:

   ```powershell
   & "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DMyAppVersion=<version> installer\printlink.iss
   ```

   Produces `installer\output\PrintLinkSetup-<version>.exe`.

The installer registers the tray agent at login, opens firewall rules for
TCP 9100 (API) and UDP 5353 (mDNS) on the Private profile, and calls the
agent's `--install-verbs` to add the right-click verb. On domain networks
the firewall rules are per-PC; adjust via GPO.

Since 0.2.3 the shared printer list and PC identity live machine-wide in
`%PROGRAMDATA%\PrintLink`. Since 0.3, pairing tokens moved to a per-user
private db (`%LOCALAPPDATA%\PrintLink\printlink-private.db`) — see
`docs/security.md` for the threat model.

## Security model

- Host-controlled access: nothing prints without an accepted grant.
- Grants: 7-day expiry by default (server-clamped to 1–90 days), hourly
  enforcement, manual revocation or extension ("Manage grants..." on the
  host).
- Pairing token (64 hex chars) = AES key + HMAC auth key. Since 0.3 it is
  **never transmitted**: each request proves possession via
  `hmac_sha256(sha256(token), one-time-nonce)`, so sniffing a job yields
  neither the credential nor the payload key. Pre-0.3 peers are tolerated
  behind `LEGACY_TOKEN_AUTH` until every PC is updated.
- Identity verification: senders `/ping`-check that the machine at the
  resolved IP really owns the dialed ID **before every** sensitive call
  (pairing, printing, revocation) — stale-IP and mDNS spoofing attempts
  fail closed.
- Payloads encrypted AES-256-GCM; tampering fails decryption (401).
- Pairing tokens live in a per-user private db (`%LOCALAPPDATA%`), so one
  Windows account can no longer read another's tokens or inject grants
  into its host. The shared printer list stays machine-wide.
- Pairing endpoint is rate-limited (5/15 min per IP); accept dialogs
  auto-decline after 60 s.
- `POST /revoke-grant` lets a remote revoke its own access (HMAC-proven);
  unsharing a printer revokes all its grants.

See `docs/protocol.md` for wire formats, `docs/architecture.md` for design,
and `docs/security.md` for the threat model.

## Licensing

PrintLink itself is MIT-licensed (see LICENSE). The preview thumbnails and
PDF page-range rendering use **PyMuPDF, which is AGPL-3.0 / commercial dual
license**. Anyone redistributing PrintLink commercially must swap in
pypdfium2 or drop the preview (the receiver falls back to printing the whole
file). Pillow (thumbnails) and PyInstaller (bundling) are similarly bundled
under their respective licenses.
