# Changelog

All notable changes. Format loosely follows Keep a Changelog; versions are
tagged on GitHub with installers attached.

## 1.0.0 — 2026-08

First stable release: safe to recommend to a non-technical small office.

### Added
- **TLS transport**: the API serves HTTPS only, using a persistent
  self-signed host certificate; senders pin its SHA256 fingerprint at
  pairing (TOFU, protected by both users' confirmations) and refuse any
  other certificate. Active man-in-the-middles can now only disconnect,
  not read or alter jobs.
- **In-app updates** (`Check for updates...` in the tray + a throttled
  daily check): downloads the new installer, verifies its SHA256 against
  the release's `checksums.txt`, and installs after explicit consent.
- **Print jobs view** in the tray: status/attempt/error per job, cancel of
  queued jobs, retry of failed ones while the document still exists.
- Unique shared-printer aliases (case-insensitive), enforced server-side;
  existing databases migrate and colliding aliases get renamed.
- Published SHA256 checksums for every release asset.

### Changed
- The wire protocol is HMAC-only: the pre-0.3 plaintext `X-Token` request
  path was removed from sender and receiver (mixed fleets must update all
  PCs together).
- The receiver runs on a stdlib threaded WSGI server over TLS instead of
  the Werkzeug development server.

### Fixed
- Extending an expired grant on the host re-enables senders immediately;
  local staleness no longer locks clients out (the host is authoritative).
- Re-adding a printer after expiry no longer requires the host to revoke.
- Installer no longer launches two agent instances after setup.

## 0.4.0 — 2026-08
Self-updater, expiry repair flow (host-authoritative), preview dialog
polish, landing page redesign, installer double-launch fix.

## 0.3.0 — 2026-08
HMAC challenge-response auth (token never crosses the wire), per-send
identity verification, per-user private token database, share-request
rate limiting, hardened error bodies.

## 0.2.x — 2026-08
Direct-send architecture replacing the port monitor, print preview,
machine-wide printer list, per-user fallbacks, CI.
