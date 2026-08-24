# Troubleshooting

Common PrintLink problems, in the order they usually show up.

## Printing fails on the host PC

**"SumatraPDF not found on this host; PDF printing unavailable"**
Install SumatraPDF (winget install SumatraPDF or sumatrapdfreader.org).
Only needed on hosts that receive PDFs. Word documents need Microsoft
Word installed on the host.

**Job spooled but nothing prints**
Check the printer itself: paused queue, offline after driver update, or a
USB printer asleep. The tray toast tells you what the host saw — open
`Manage grants...` / the sender's `Print jobs...` view for the recorded
error.

## Discovery / connection problems

**"Host not found on the LAN"**
- Is the tray agent running on the host (printer icon in its system tray)?
- Both PCs on the same subnet? mDNS (UDP 5353) must be allowed — corporate
  VLANs often block multicast. PrintLink falls back to the last known IP,
  which appears once at least one successful pairing/connection happened.
- Firewall: the installer opens TCP 9100 + UDP 5353 on the **Private**
  profile only. On Domain networks adjust via GPO.

**"ID mismatch: that IP answers as ..."**
Another machine owns that IP now (DHCP churn) or an mDNS spoof. Wait for
the lease to change, re-check the ID you entered, and retry — PrintLink
verifies identity before sending anything.

**"Host certificate changed during pairing / fingerprint mismatch"**
The host's TLS identity changed: someone reinstalled Windows/PrintLink on
the host, or something is impersonating it. If a reinstall is expected,
remove and re-add the printer on your side (re-pins the new certificate);
otherwise treat it as suspicious.

## Pairing problems

**No accept dialog appears on the host**
The dialog auto-declines after 60 s and pairing requests are rate-limited
to 5 per 15 minutes per PC — wait a minute and try again.

**Grant expired — "share overdue"**
Just send again: the host re-checks every job. If the host extended the
grant, your next print succeeds without re-pairing.

## Install / startup problems

**Two tray icons after installing (pre-1.0)**
Fixed in 1.0 — the post-install checkbox no longer launches a second
agent. Update all PCs.

**Agent doesn't start with Windows**
The installer registers a per-user Run key (`HKCU\...\Run\PrintLink`) —
it must be installed under the account that should auto-start it, or run
once per account.

**SmartScreen warning ("Windows protected your PC")**
PrintLink installers are not code-signed. Click *More info → Run anyway*
after verifying the SHA256 checksum published with each release.

## Where the data lives

| What | Where |
|---|---|
| Shared printers, PC identity | `%PROGRAMDATA%\PrintLink` |
| Grants/tokens (per Windows account) | `%LOCALAPPDATA%\PrintLink\printlink-private.db` |
| Logs | `%PROGRAMDATA%\PrintLink\printlink.log` |

Deleting `%PROGRAMDATA%\PrintLink\identity.json` gives the PC a new ID but
orphans every existing grant — avoid unless starting over deliberately.
