# PrintLink port monitor — debugging notes

Reconstruction of the reverse-engineering saga behind
`port-monitor/PrintLinkMonitor/monitor.cpp`. This knowledge lived only in a
local REPORT.md and the dev session — this file is the durable record for the
next time a Windows update breaks printing.

**Bottom line:** the whole MONITOR2 path is a reverse-engineered, unsupported
contract on Windows 11 24H2 / build 26100. If jobs fail after an OS update,
assume the contract changed first. See the checklist at the end.

## Background

Sender PCs install "PrintLink Remote Printer" (Microsoft XPS driver) whose
port is a custom local port monitor. The monitor's job: take whatever the
spooler hands it and stream it into `\\.\pipe\PrintLinkSender`, where the
Python agent reads framed jobs. The monitor must stay stateless — all real
logic lives in Python; crashes in Python must never corrupt the spooler.

## The Event 350 saga (crash discovery)

Initial symptom: jobs failed with spooler error events (Event 350 /
job-failure path) and the spooler was unstable. The monitor was first built
against the documented MONITOR2 API: `InitializePrintMonitor2` returning a
`MONITOR2` struct and `BOOL` status. On 26100 that crashes `spoolsv.exe` with
an access violation in `localspl!CreateMonitorEntry+0x6d4`
(`cmp [rax],0B8h`) — localspl does NOT accept the documented form.

Contract discovered via crash dumps (WinDbg on localspl):

- `localspl!CreateMonitorEntry` calls `ApphelpIsPortMonAllowed` as a gate,
  then `GetProcAddress(hDll, "InitializePrintMonitor2")`.
- The **return value is treated as a pointer to a monitor object**, not a
  status:
  - `obj[0]` = any user-space pointer (>= 0xB8 as DWORD) — sanity check
  - `obj[8..0x90]` = 17-entry MONITOR2 dispatch table (winsplp.h order)
  - `obj[0x90]` = copy of `pMonitorInit` (localspl gate: `[entry+0xE8]`
    must be non-zero)
  - localspl memcpy's up to 0xB8 bytes into its entry and builds final
    dispatch via `localspl!InitializeUMonitor` (`gafpDlStub[i] ?: obj[8+i*8]`).
- Returning BOOL (1) from `InitializePrintMonitor2` crashes with that AV —
  the fix is the `PL_MONITOR_OBJECT` layout in monitor.cpp (static_assert
  0xB8).

## Probe semantics (why ReadPort/WritePort look insane)

The 26100 despool (`PrintingDirectlyToPort`) does not behave like the docs:

1. **StartDoc probe.** `WritePort` carries only a tiny UTF-16LE prefix of the
   port name ('P\0', 'P\0r', ...), then `ReadPort(262144)`. The despool
   verifies the readback equals what it wrote — count AND content:
   - a 0-byte answer => retry forever
   - an accumulated answer => verify fails => despool re-runs the whole job
     with a longer prefix (up to 13 attempts)
   - the exact, one-shot echo of the last write passes. `lastWrite` + one-shot
     `pendingEcho` implement this.
2. **Payload never arrives via WritePort.** The despool pre-fills the
   ReadPort buffer with the chunk (buffer size == chunk size, observed
   262144/262148) and counts `*pcbRead` as bytes written to the port. So
   ReadPort is the payload channel: forward the buffer to the pipe and report
   the count.
3. **Mangled trailing args.** The 26100 dispatch mangles `cbBuf`/`pcbWritten`
   on some calls (observed cbBuf 4294967295, 4294705150/4294705151 = -16385,
   -16387; pcbWritten == 1). Never trust them: clamp cbBuf to 262144, and
   probe the readable extent of pBuf with `IsBadReadPtr` (binary search) —
   a `WriteFile` over unmapped memory causes ERROR_NOACCESS, kills the pipe,
   and leaves the despool spinning in a 10s retry loop forever.
4. **EOF poll.** ReadPort with `pBuf == NULL` (cbBuf ~4294705150) is the
   despool checking the port drained — answer 0/TRUE so the job completes.
5. **Job start marker.** The despool reuses ONE port handle across jobs and
   marks each job's start with the probe WritePort — that is also the reset
   signal for per-job state (close the previous SPL handle, clear flags).

## Pipe contention discovery

`pipe_reader.py` is a single-threaded reader; the monitor connects per
port-open with lazy reconnect. Findings:

- **Never return FALSE from a valid-buffer ReadPort when the pipe is dead.**
  The 26100 despool treats it as "retry forever" (observed 10s loop for 25+
  minutes, port wedged, every later job errors, ClosePort never called).
  Instead drain: report 0/TRUE and drop the pipe.
- **Drop the pipe on failed writes**, so the agent's read unblocks (broken
  pipe = end of job) and the next job reconnects via `EnsureConnected`.
- **EndDocPort/ClosePort**: flush the pipe so the reader sees EOF. 26100
  also calls ClosePort between probe cycles while keeping the same handle —
  ClosePort must NOT invalidate the struct, only drop the pipe
  (observed: err=6 ERROR_INVALID_HANDLE forever when it did).

## SPL payload discovery

On 26100 the despool never streams the rendered document through the monitor
— ReadPort carries zeroed staging and print-pipeline artifacts (PrintConfig
tickets). The real EMF lives in `%SystemRoot%\System32\spool\PRINTERS\<job>.SPL`.
`OpenSplPayload` finds the newest *.SPL, positions past the " EMF" signature
(EMF header offset 40; RAW-spooled jobs have no signature -> serve from 0),
and serves it through ReadPort while mirroring to the pipe. The despool
counts served bytes as written to the port, so filling its buffer with SPL
content satisfies both the count and the payload transport.

## Probe-echo head loss

The echo overwrites the head of the despool's read buffer, but the despool
counts those bytes as written — if they are not forwarded, every job comes
out `probe_len` bytes too long (observed: received.bin started with probe
bytes). The `stash` mechanism preserves the overwritten head and prepends it
to the next forward, and `*pcbRead` must include it (262141 vs 262144 ->
retry -> job error).

## Checklist when a Windows update breaks printing

1. Check the spooler crash first: is it the `cmp [rax],0B8h` AV? -> the
   monitor-object contract changed (`PL_MONITOR_OBJECT`, dispatch order,
   gate field at +0x90).
2. Dump a job with a debugger: does WritePort carry only the probe? Does
   ReadPort receive the payload (buffer pre-filled)? -> probe/echo semantics
   changed.
3. Look at `%SystemRoot%\System32\spool\PRINTERS\` — is the EMF still there,
   and does ReadPort still get called with a pre-filled buffer? -> if the
   despool streams through WritePort now, the payload path needs to move.
4. Is there a new spooler operational log repeating with 10s retries? ->
   likely a dead-pipe FALSE return or an echo mismatch.
5. The v1 (ANSI) interface (`PL1_*`) is the fallback path — the despool also
   probes it; keep it working if the MONITOR2 contract gets too unstable.
6. `C:\Windows\Temp\plmon.log` has verbose traces for all of the above —
   that is the primary evidence source.
