// PrintLinkMonitor.cpp — minimal local port monitor.
// Registered port: "PrintLink:" — all jobs spooled to this port are streamed
// into the named pipe \\.\pipe\PrintLinkSender, framed as:
//   [4 bytes little-endian job name length][job name UTF-8]
//   [8 bytes little-endian payload length][payload bytes]
// The Python agent (pipe_reader.py) owns the pipe; this DLL stays stateless.
//
// IMPORTANT (Windows 11 24H2 / build 26100 contract, reverse-engineered from
// localspl.dll via crash dumps):
//  - localspl!CreateMonitorEntry loads the DLL, calls
//    "ApphelpIsPortMonAllowed" (apphelp) as a gate, then calls
//    GetProcAddress(hDll, "InitializePrintMonitor2") and treats the RETURN
//    VALUE as a pointer to a monitor object:
//        obj[0]       = any user-space pointer (>= 0xB8 as DWORD) — sanity check
//        obj[8..0x90] = 17-entry MONITOR2 dispatch table (winsplp.h order)
//    localspl memcpy's up to 0xB8 bytes into its entry and builds the final
//    dispatch via localspl!InitializeUMonitor from these entries
//    (gafpDlStub[i] ?: obj[8+i*8]).
//  - Returning BOOL (1) from InitializePrintMonitor2 crashes spoolsv with
//    AV in localspl!CreateMonitorEntry+0x6d4 (cmp [rax],0B8h).
//  - All dispatch functions are called with a leading HANDLE first parameter
//    (MONITOR2 signatures). windows.h pulls in winspool.h whose PUBLIC API
//    declarations (EnumPortsW, OpenPortW, ...) conflict with those, so the
//    implementations use PL_* names and PrintLinkMonitor.def maps the export
//    names (EnumPortsW = PL_EnumPortsW, ...) to them.
//
// Build: Visual Studio, DLL, x64, static CRT.
// Install: copy to %SystemRoot%\System32, register via AddMonitor
// (see PrintLinkSetup) and create HKLM\...\Monitors\PrintLinkMonitor\Ports\PrintLink:
#include <windows.h>
#include <strsafe.h>
#include <stdarg.h>

static void Trace(const wchar_t* msg);
static void Tracef(const wchar_t* fmt, ...);
static void TraceBytes(const wchar_t* label, const void* p, DWORD cb);

// Minimal MONITOR2 support types (avoiding winsplp.h). PORT_INFO_1W comes
// from winspool.h (pulled in by windows.h) and is compatible.
typedef struct _MONITORINIT {
    DWORD cbSize;
    HANDLE hSpooler;
    HANDLE hckRegistryRoot;
    PBYTE pMonitorReg;
    BOOL bLocal;
    LPTSTR pszServerName;
} MONITORINIT, *PMONITORINIT;

typedef struct _PORT_TIMEOUTS {
    LONG dwMultipleOpen;
    LONG dwFirstOpen;
    LONG dwWait;
} PORTTIMEOUTS, *PPORTTIMOUTS;

#define PIPE_NAME  L"\\\\.\\pipe\\PrintLinkSender"
#define MONITOR_NAME L"PrintLinkMonitor"
#define PORTS_KEY  L"SYSTEM\\CurrentControlSet\\Control\\Print\\Monitors\\PrintLinkMonitor\\Ports"

// Test override: set env PLMONPIPE (machine scope, full pipe path) before the
// spooler loads this DLL to use a unique pipe name and avoid the production
// PrintLinkAgent instances; resolved once in DllMain.
static wchar_t g_pipeName[300];

typedef struct {
    HANDLE hPipe;      // NULL when not yet connected (lazy connect on write)
    DWORD  jobId;
    DWORD  nameLen;    // header: job-name length + bytes (UTF-8)
    char   nameUtf8[512];
    WCHAR  wideName[512];  // port name in UTF-16LE bytes: the despool probe
    DWORD  wideNameLen;    //   writes this name incrementally and reads it back
    BYTE  lastWrite[64];  // exact bytes of the last successful WritePort
    DWORD lastWriteLen;   //   (the despool's probe, one-shot echo)
    BOOL  pendingEcho;    // one-shot: next ReadPort returns the probe, then forward
    BYTE  stash[64];      // payload head the probe-echo overwrote; prepended to
    DWORD stashLen;       //   the next forward so the agent gets the exact payload
// Job payload source: the spool file. On 26100 the despool never streams the
// rendered document through the monitor — ReadPort carries only zeroed staging
// and print-pipeline artifacts (PrintConfig tickets). The real EMF lives in
// %SystemRoot%\System32\spool\PRINTERS\<job>.SPL, so ReadPort serves it.
    HANDLE     hSpl;      // open job spool file (payload source)
    ULONGLONG  splOff;    // next byte to serve
    ULONGLONG  splEnd;    // end of payload area
    BOOL       splReady;  // located once per job
} PL_PORT, *PPL_PORT;

static HANDLE ConnectPipe(DWORD timeoutMs) {
    DWORD waited = 0;
    while (true) {
        HANDLE h = CreateFileW(g_pipeName, GENERIC_WRITE, 0, NULL,
                               OPEN_EXISTING, 0, NULL);
        Tracef(L"  CreateFileW(%ls) -> %p err=%u waited=%u", g_pipeName, h,
               GetLastError(), waited);
        if (h != INVALID_HANDLE_VALUE) return h;
        if (GetLastError() != ERROR_PIPE_BUSY || waited >= timeoutMs) return NULL;
        if (!WaitNamedPipeW(PIPE_NAME, 1000)) return NULL;
        waited += 1000;
    }
}

static BOOL WriteAll(HANDLE h, const void* buf, DWORD len) {
    DWORD written;
    return WriteFile(h, buf, len, &written, NULL) && written == len;
}

// Write the frame header (job name + placeholder payload length).
static BOOL WriteHeader(PPL_PORT p) {
    ULONGLONG payloadLen = 0; // streamed: length unknown; agent reads until pipe closes
    return WriteAll(p->hPipe, &p->nameLen, 4) &&
           WriteAll(p->hPipe, p->nameUtf8, p->nameLen) &&
           WriteAll(p->hPipe, &payloadLen, 8);
}

// Connect the pipe and (if connected) write the job header; shared by the
// MONITOR2 and v1 OpenPort paths. When allowDisconnected is set, a struct
// with hPipe == NULL is returned on connection failure (the spooler probes
// the port during AddPrinter and must succeed whether or not the agent is
// running; WritePort then lazily connects).
// Keep the most recent bytes written via WritePort so the next ReadPort can
// echo them back to the despool probe (localspl verifies its own write by
// reading it back exactly once; returning nothing makes it retry forever,
// returning an accumulated buffer fails the verify and the despool re-runs
// the whole job with a growing port-name probe).
static void SetLastWrite(PPL_PORT p, const void* buf, DWORD len) {
    if (!p || !buf || !len) { p->lastWriteLen = 0; return; }
    DWORD n = min(len, (DWORD)sizeof(p->lastWrite));
    memcpy(p->lastWrite, buf, n);
    p->lastWriteLen = n;
    p->pendingEcho = TRUE;
}

static PPL_PORT OpenPortInternal(LPCWSTR pPortName, LPCWSTR pPrinterName,
                                 DWORD timeoutMs, BOOL allowDisconnected) {
    PPL_PORT p = (PPL_PORT)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(PL_PORT));
    if (!p) return NULL;

    // frame header: job name (printer name here) + placeholder payload length
    char nameUtf8[512]; DWORD nameLen;
    nameLen = WideCharToMultiByte(CP_UTF8, 0, pPrinterName ? pPrinterName : L"job",
                                  -1, nameUtf8, sizeof(nameUtf8) - 1, NULL, NULL);
    if (nameLen == 0 || nameLen == 0xFFFFFFFF) nameLen = 0;  // conversion failed
    nameLen -= 1;  // strip the NUL terminator
    p->nameLen = nameLen;
    memcpy(p->nameUtf8, nameUtf8, nameLen);
    if (pPrinterName) {
        DWORD n = lstrlenW(pPrinterName);
        if (n >= 512) n = 511;
        memcpy(p->wideName, pPrinterName, n * sizeof(WCHAR));
        p->wideNameLen = n * (DWORD)sizeof(WCHAR);
    }

    p->hPipe = ConnectPipe(timeoutMs);   // wait for the Python agent
    Tracef(L"  ConnectPipe -> %p err=%u", p->hPipe, GetLastError());
    if (!p->hPipe) {
        if (!allowDisconnected) { HeapFree(GetProcessHeap(), 0, p); return NULL; }
        return p;  // probe: keep the struct, connect lazily on WritePort
    }
    if (!WriteHeader(p)) {
        Tracef(L"  WriteHeader FAILED err=%u", GetLastError());
        CloseHandle(p->hPipe); p->hPipe = NULL;
        if (!allowDisconnected) { HeapFree(GetProcessHeap(), 0, p); return NULL; }
    } else {
        Tracef(L"  header written (nameLen=%u)", p->nameLen);
    }
    return p;
}

// Ensure the pipe is connected and the header written; called by WritePort.
static BOOL EnsureConnected(PPL_PORT p) {
    if (p->hPipe) return TRUE;
    p->hPipe = ConnectPipe(3000);   // agent may have started since the probe
    if (!p->hPipe) { SetLastError(ERROR_NOT_CONNECTED); return FALSE; }
    if (!WriteHeader(p)) { CloseHandle(p->hPipe); p->hPipe = NULL; return FALSE; }
    return TRUE;
}

// ---------------- MONITOR2 dispatch implementations -------------------------
// All signatures follow the MONITOR2 table (leading HANDLE), NOT winspool.h.
// Export names are mapped to these via PrintLinkMonitor.def.

extern "C" BOOL WINAPI PL_EnumPortsW(HANDLE hMonitor, LPWSTR pName, DWORD Level,
                                     LPBYTE pPorts, DWORD cbBuf, LPDWORD pcbNeeded,
                                     LPDWORD pcbReturned) {
    if (Level != 1) { SetLastError(ERROR_INVALID_LEVEL); return FALSE; }
    *pcbNeeded = 0; *pcbReturned = 0;

    HKEY hk = NULL;
    if (RegOpenKeyExW(HKEY_LOCAL_MACHINE, PORTS_KEY, 0, KEY_READ, &hk) != ERROR_SUCCESS)
        return TRUE;  // no ports configured — legitimate empty answer

    DWORD count = 0, needed = 0;
    WCHAR name[512]; DWORD nameLen;
    for (DWORD i = 0; ; i++) {
        nameLen = 512;
        LONG r = RegEnumKeyExW(hk, i, name, &nameLen, NULL, NULL, NULL, NULL);
        if (r == ERROR_NO_MORE_ITEMS) break;
        if (r != ERROR_SUCCESS) break;
        count++;
        needed += sizeof(PORT_INFO_1W) + (nameLen + 1) * sizeof(WCHAR);
    }

    *pcbNeeded = needed;
    if (cbBuf < needed) { RegCloseKey(hk); SetLastError(ERROR_INSUFFICIENT_BUFFER); return FALSE; }

    PORT_INFO_1W* arr = (PORT_INFO_1W*)pPorts;
    LPWSTR str = (LPWSTR)(pPorts + count * sizeof(PORT_INFO_1W));
    DWORD n = 0;
    for (DWORD i = 0; ; i++) {
        nameLen = 512;
        LONG r = RegEnumKeyExW(hk, i, name, &nameLen, NULL, NULL, NULL, NULL);
        if (r != ERROR_SUCCESS) break;
        lstrcpyW(str, name);
        arr[n].pName = str;
        str += nameLen + 1;
        n++;
    }
    RegCloseKey(hk);
    *pcbReturned = n;
    return TRUE;
}

// localspl 26100's TMonitorHandle path calls the dispatch OpenPort as
// (hMonitor, pPortName, pHandle) — the out-param lands in the 3rd argument
// and the 4th (pHandle) is left unset. The documented 4-arg MONITOR2 call
// puts it in the 4th. Accept both: write through whichever is non-NULL.
extern "C" BOOL WINAPI PL_OpenPortW(HANDLE hMonitor, LPWSTR pPortName,
                                    LPWSTR pPrinterName, HANDLE* pHandle) {
    HANDLE* out = pHandle ? pHandle : (HANDLE*)pPrinterName;
    if (!out) return FALSE;
    // In the observed 3-arg 26100 call, arg3 is the out-handle slot, not a
    // string: use the port name as the job name then, so the frame header
    // carries a meaningful name.
    LPCWSTR jobName = pHandle ? (pPrinterName ? pPrinterName : L"job")
                              : (pPortName ? pPortName : L"job");
    Tracef(L"OpenPort %ls (arg3=%p arg4=%p) out=%p", pPortName, pPrinterName,
           pHandle, out);
    PPL_PORT p = OpenPortInternal(pPortName, jobName, 20000, FALSE);
    if (!p) return FALSE;
    *out = p;
    Tracef(L"  -> handle %p", p);
    return TRUE;
}

extern "C" BOOL WINAPI PL_StartDocPortW(HANDLE hPort, LPWSTR pPrinterName, DWORD JobId,
                                        DWORD Level, LPBYTE pDocInfo) {
    Tracef(L"StartDocPort hPort=%p pPrinter=%ls job=%u lvl=%u doc=%p", hPort,
           pPrinterName ? pPrinterName : L"(null)", JobId, Level, pDocInfo);
    __try { ((PPL_PORT)hPort)->jobId = JobId; }
    __except (EXCEPTION_EXECUTE_HANDLER) { return FALSE; }
    return TRUE;
}

// Guarded write: localspl fail-fasts (c0000409) if ANY exception escapes a
// monitor call, and the 26100 dispatch can hand us a bogus hPort/pcbWritten
// (observed: pcbWritten == 1). Wrap everything in SEH and never let an
// exception cross the monitor boundary.
//
// 26100 WritePort carries ONLY the StartDoc probe (the port-name prefix the
// despool verifies by reading back). The payload flows exclusively through
// ReadPort, so the probe must NOT be forwarded to the agent pipe: doing so
// polluted the stream head (received.bin started with the probe bytes and
// every job came out probe_len bytes too long). Record it for the one-shot
// echo and acknowledge the write.
static BOOL GuardedWritePort(PPL_PORT p, LPBYTE pBuf, DWORD cbBuf, LPDWORD pcbWritten) {
    DWORD written = 0;
    BOOL ok = FALSE;
    DWORD err = 0;
    Tracef(L"  WritePort ENTER hPort=%p cbBuf=%u pcbWritten=%p", p, cbBuf, pcbWritten);
    __try {
        if (p && pBuf) {
            written = cbBuf;   // probe ack: despool counts it as written to the port
            ok = TRUE;
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) { ok = FALSE; written = 0; }
    if (ok) { SetLastWrite(p, pBuf, written); }
    if (!ok) err = GetLastError();
    TraceBytes(L"  WritePort buf", pBuf, cbBuf);
    Tracef(L"  WritePort hPort=%p cbBuf=%u ok=%d err=%u written=%u pcbWritten=%p",
           p, cbBuf, ok, err, written, pcbWritten);
    if (pcbWritten) {
        __try { *pcbWritten = written; } __except (EXCEPTION_EXECUTE_HANDLER) {}
    }
    return ok;
}

extern "C" BOOL WINAPI PL_WritePort(HANDLE hPort, LPBYTE pBuf, DWORD cbBuf,
                                    LPDWORD pcbWritten) {
    PPL_PORT p = (PPL_PORT)hPort;
    __try {
        if (p) {
            // New job start: the 26100 despool reuses ONE port handle across
            // jobs and marks each job's start with the probe WritePort, so
            // reset the previous job's spool-file state here. Without this,
            // splReady stays set and every job after the first never locates
            // its SPL (observed: first job "none found", later jobs forward
            // the despool's zeroed staging instead of the document).
            if (p->hSpl) { CloseHandle(p->hSpl); p->hSpl = NULL; }
            p->splReady = FALSE;
            p->splOff = 0;
            p->splEnd = 0;
            p->stashLen = 0;
            p->pendingEcho = FALSE;
            p->lastWriteLen = 0;
        }
        if (p && !p->hPipe) {
            if (!EnsureConnected(p)) { if (pcbWritten) *pcbWritten = 0; return FALSE; }
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) { return FALSE; }
    return GuardedWritePort(p, pBuf, cbBuf, pcbWritten);
}

// Locate and open the job's spool file (newest *.SPL in the spool dir) and
// position at the EMF document payload (starts at the " EMF" dSignature,
// EMF header offset 40; RAW-spooled jobs have no signature -> serve from 0).
static void OpenSplPayload(PPL_PORT p) {
    wchar_t dir[MAX_PATH];
    if (!GetSystemDirectoryW(dir, MAX_PATH) || MAX_PATH - lstrlenW(dir) < 40) return;
    wcscat_s(dir, L"\\spool\\PRINTERS");
    wchar_t pat[MAX_PATH];
    swprintf_s(pat, L"%s\\*.SPL", dir);
    WIN32_FIND_DATAW fd;
    HANDLE hs = FindFirstFileW(pat, &fd);
    wchar_t best[MAX_PATH]; best[0] = 0;
    ULONGLONG newest = 0;
    if (hs != INVALID_HANDLE_VALUE) {
        do {
            ULONGLONG t = ((ULONGLONG)fd.ftLastWriteTime.dwHighDateTime << 32)
                          | fd.ftLastWriteTime.dwLowDateTime;
            if (t > newest) { newest = t; lstrcpynW(best, fd.cFileName, MAX_PATH); }
        } while (FindNextFileW(hs, &fd));
        FindClose(hs);
    }
    if (!best[0]) { Tracef(L"  SPL source: none found in %ls", dir); return; }
    swprintf_s(pat, L"%s\\%s", dir, best);
    HANDLE f = CreateFileW(pat, GENERIC_READ,
                           FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                           NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (f == INVALID_HANDLE_VALUE) {
        Tracef(L"  SPL source: open %ls failed err=%u", pat, GetLastError());
        return;
    }
    ULARGE_INTEGER sz;
    sz.LowPart = GetFileSize(f, &sz.HighPart);
    ULONGLONG payStart = 0;
    DWORD head = (DWORD)min((ULONGLONG)(1 << 20), sz.QuadPart);
    if (head) {
        BYTE* buf = (BYTE*)HeapAlloc(GetProcessHeap(), 0, head);
        DWORD got = 0;
        if (buf && ReadFile(f, buf, head, &got, NULL) && got) {
            for (DWORD i = 0; i + 4 <= got; i++) {
                if (buf[i] == ' ' && buf[i + 1] == 'E' && buf[i + 2] == 'M'
                    && buf[i + 3] == 'F') { payStart = (ULONGLONG)i - 40; break; }
            }
        }
        if (buf) HeapFree(GetProcessHeap(), 0, buf);
    }
    LARGE_INTEGER li; li.QuadPart = (LONGLONG)payStart;
    SetFilePointerEx(f, li, NULL, FILE_BEGIN);
    p->hSpl = f;
    p->splOff = payStart;
    p->splEnd = sz.QuadPart;
    Tracef(L"  SPL source: %ls size=%I64u payload@%I64u", pat, sz.QuadPart, payStart);
}

// localspl despool, reverse-engineered on 26100 (SplWritePrinter's lambda):
//  - StartDoc probe (PrintingDirectlyToPort): WritePort of tiny UTF-16LE port-
//    name prefixes ('P\0','P\0r',...) then ReadPort(262144). It verifies the
//    readback equals the bytes just written (count AND content): a 0-byte
//    answer retries forever, an accumulated answer fails the verify and the
//    despool re-runs the whole job with a longer prefix (up to 13 attempts).
//    The exact, one-shot echo of the last write passes it.
//  - Payload streaming (PrintRawJob -> WritePrinter -> SplWritePrinter):
//    WritePort is NEVER called. The despool hands the payload chunk to
//    ReadPort(hPort, pBuf=<payload>, cbBuf=262144, &read) and counts the read
//    as written: read!=0 = success, *pcbRead bytes are "written". The buffer
//    already contains the chunk, so forward it to the agent pipe and count it.
// Distinguish the two by the one-shot pendingEcho flag: the verify read always
// immediately follows a successful WritePort; the readback has no preceding
// write (and even if it did, only one echo can fire per WritePort).
extern "C" BOOL WINAPI PL_ReadPort(HANDLE hPort, LPBYTE pBuf, DWORD cbBuf,
                                   LPDWORD pcbRead) {
    Tracef(L"ReadPort hPort=%p pBuf=%p cbBuf=%u pcbRead=%p", hPort, pBuf, cbBuf, pcbRead);
    __try {
        if (!pcbRead) { return TRUE; }  // nothing to report; don't fail the job
        if (!pBuf) {
            // Despool EOF poll: no buffer (observed NULL with cbBuf like
            // 4294705150) — it is checking the port drained, NOT asking for
            // data. Answer 0/TRUE so the job completes instead of retrying
            // every 10s and erroring.
            Tracef(L"  ReadPort EOF poll (pBuf=NULL)");
            *pcbRead = 0;
            return TRUE;
        }
        if (cbBuf > 262144 && cbBuf < 0xFFFFFF00) {
            // 26100 dispatch mangling: cbBuf observed as 4294967295,
            // 4294705151, 4294705150 (-16387, 4294705151...). The true read
            // size is 262144; a mangled value is safe to clamp.
            Tracef(L"  ReadPort cbBuf mangled=%u", cbBuf);
        }
        TraceBytes(L"  ReadPort in ", pBuf, min(cbBuf, 64));
        PPL_PORT p = (PPL_PORT)hPort;
        DWORD n = 0;
        if (p) {
            if (p->pendingEcho && p->lastWriteLen) {
                // The 26100 dispatch mangles trailing args (observed: cbBuf
                // traced as 1 while the despool's real read buffer is
                // 262144). The despool verifies the probe by reading back
                // EXACTLY what it wrote (count AND content) in one call —
                // truncating the echo to the mangled cbBuf fails the verify
                // and the job dies with zero payload. lastWrite is capped at
                // 64 bytes, far below the real buffer, so serve it all.
                DWORD nEcho = p->lastWriteLen;
                // preserve the payload head the echo is about to overwrite;
                // the despool counts it as written, so the next forward
                // must send it first to keep the agent's stream intact
                DWORD keep = min(nEcho, (DWORD)sizeof(p->stash));
                memcpy(p->stash, pBuf, keep);
                p->stashLen = keep;
                memcpy(pBuf, p->lastWrite, nEcho);
                n = nEcho;
                p->pendingEcho = FALSE;
                Tracef(L"  ReadPort probe-echo n=%u", n);
            } else {
                // Payload delivery: the despool pre-fills its read buffer
                // with the chunk (buffer size == chunk size, observed
                // 262144/262148) and counts *pcbRead as written to the
                // port. The 26100 dispatch mangles cbBuf (traced as 68 for
                // a real 68, but also 4294967295/-16387 for the same
                // 262144 read), so the forward must NOT trust it: size by
                // the READABLE EXTENT of pBuf. The despool hands pBuf past
                // the echo area, so the readable prefix is exactly the
                // remaining chunk bytes. This also guarantees WriteFile
                // never touches unmapped memory: an ERROR_NOACCESS there
                // kills the pipe AND leaves the despool spinning forever in
                // ReadPort (observed 10s-retry loop, job never completes).
                n = min(cbBuf, 262144);
                if (n && IsBadReadPtr(pBuf, n)) {
                    DWORD lo = 0, hi = n;  // binary search the readable prefix
                    while (lo + 1 < hi) {
                        DWORD mid = lo + (hi - lo) / 2;
                        if (IsBadReadPtr(pBuf, mid)) hi = mid; else lo = mid;
                    }
                    n = lo;
                    Tracef(L"  ReadPort extent-trimmed to %u", n);
                }
                if (p && !p->splReady) { p->splReady = TRUE; OpenSplPayload(p); }
                if (p && p->hSpl) {
                    // Serve the real document from the spool file: the despool
                    // counts *pcbRead as bytes written to the port, so filling
                    // its buffer with SPL content both satisfies the count and
                    // carries the payload. Mirror each served byte to the
                    // agent pipe (the receiver's payload).
                    if (p->splOff >= p->splEnd) {
                        Tracef(L"  ReadPort SPL exhausted (served to %I64u)", p->splOff);
                        *pcbRead = 0;
                        return TRUE;
                    }
                    ULONGLONG avail = p->splEnd - p->splOff;
                    DWORD want = (DWORD)min((ULONGLONG)n, avail);
                    DWORD got = 0;
                    BOOL okR = ReadFile(p->hSpl, pBuf, want, &got, NULL);
                    if (okR && got) p->splOff += got;
                    DWORD pipeErr = 0;
                    if (okR && got && p->hPipe) {
                        DWORD written = 0;
                        if (!WriteFile(p->hPipe, pBuf, got, &written, NULL)
                            || written != got) {
                            pipeErr = GetLastError();
                            CloseHandle(p->hPipe); p->hPipe = NULL;
                        }
                    }
                    Tracef(L"  ReadPort SPL serve want=%u got=%u pipeErr=%u offset=%I64u",
                           want, got, pipeErr, p->splOff);
                    p->stashLen = 0;
                    *pcbRead = okR ? got : 0;
                    return TRUE;
                }
                if (p->hPipe) {
                    DWORD written = 0, total = 0;
                    if (p->stashLen) {
                        // stash = the chunk head the echo overwrote; it was
                        // delivered to the pipe first and counts toward the
                        // despool's chunk: *pcbRead must include it or the
                        // despool comes up short (observed: 262141 returned
                        // for a 262144 chunk -> retries -> job error).
                        WriteFile(p->hPipe, p->stash, p->stashLen, &written, NULL);
                        total = p->stashLen;
                        p->stashLen = 0;
                        written = 0;
                    }
                    TraceBytes(L"  forward head ", pBuf, min(n, 64));
                    BOOL okW = WriteFile(p->hPipe, pBuf, n, &written, NULL);
                    total += written;
                    Tracef(L"  ReadPort forward cbBuf=%u fwd=%u ok=%d written=%u total=%u err=%u",
                           cbBuf, n, okW, written, total, GetLastError());
                    if (!okW || written != n) {
                        // Never phantom-ack: drop the pipe so the next job
                        // reconnects (EnsureConnected) and the agent's read
                        // unblocks (broken pipe = end of job) instead of
                        // blocking the single-threaded reader forever.
                        CloseHandle(p->hPipe); p->hPipe = NULL;
                        SetLastError(ERROR_WRITE_FAULT);
                        *pcbRead = 0;
                        return FALSE;
                    }
                    n = total;
                } else {
                    p->stashLen = 0;
                    Tracef(L"  ReadPort forward no-pipe cbBuf=%u fwd=%u", cbBuf, n);
                    if (n) {
                        // Pipe disconnected (agent restart, canceled job).
                        // NEVER return FALSE here: the 26100 despool treats a
                        // FALSE on a valid-buffer ReadPort as "retry forever"
                        // (observed: 10s loop for 25+ minutes, port wedged,
                        // all later jobs error, never ClosePort). Instead
                        // report 0/TRUE so the job drains and ends.
                        n = 0;
                        Tracef(L"  ReadPort forward dead-pipe -> drain (TRUE/0)");
                        *pcbRead = 0;
                    }
                }
            }
        }
        *pcbRead = n;
        TraceBytes(L"  ReadPort resp", pBuf, min(n, 32));
        return TRUE;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return FALSE;
    }
}

extern "C" BOOL WINAPI PL_EndDocPort(HANDLE hPort) {
    Tracef(L"EndDocPort hPort=%p", hPort);
    __try {
        PPL_PORT p = (PPL_PORT)hPort;
        if (p && p->hPipe) FlushFileBuffers(p->hPipe);  // let the reader drain
    } __except (EXCEPTION_EXECUTE_HANDLER) { return FALSE; }
    return TRUE;  // agent detects EOF on close
}

extern "C" BOOL WINAPI PL_ClosePort(HANDLE hPort) {
    Tracef(L"ClosePort hPort=%p", hPort);
    __try {
        // The 26100 despool calls ClosePort between probe cycles but keeps
        // writing through the same handle (observed: probe 'P\0' -> close ->
        // probe 'P\0r' -> err=6 ERROR_INVALID_HANDLE forever). Keep the struct
        // and drop the pipe so the next WritePort reconnects (EnsureConnected).
        PPL_PORT p = (PPL_PORT)hPort;
        if (p && p->hPipe) { FlushFileBuffers(p->hPipe); CloseHandle(p->hPipe); p->hPipe = NULL; }
        if (p && p->hSpl) { CloseHandle(p->hSpl); p->hSpl = NULL; p->splReady = FALSE; }
    } __except (EXCEPTION_EXECUTE_HANDLER) { return FALSE; }
    return TRUE;
}

extern "C" BOOL WINAPI PL_AddPortW(HANDLE hMonitor, LPWSTR pName, HWND hWnd,
                                   LPWSTR pMonitorName) {
    SetLastError(ERROR_NOT_SUPPORTED);  // ports are created by PrintLinkSetup
    return FALSE;
}

extern "C" BOOL WINAPI PL_AddPortExW(HANDLE hMonitor, LPWSTR pName, DWORD Level,
                                     LPBYTE lpBuffer, LPWSTR lpMonitorName) {
    SetLastError(ERROR_NOT_SUPPORTED);
    return FALSE;
}

extern "C" BOOL WINAPI PL_ConfigurePortW(HANDLE hMonitor, HWND hWnd, LPWSTR pPortName) {
    SetLastError(ERROR_NOT_SUPPORTED);
    return FALSE;
}

extern "C" BOOL WINAPI PL_DeletePortW(HANDLE hMonitor, LPWSTR pName, HWND hWnd,
                                      LPWSTR pMonitorName) {
    SetLastError(ERROR_NOT_SUPPORTED);  // PrintLinkSetup removes ports
    return FALSE;
}

extern "C" BOOL WINAPI PL_GetPrinterDataFromPortW(HANDLE hPort, DWORD ControlID,
                                                  LPWSTR pValueName, LPBYTE lpInBuffer,
                                                  DWORD cbInBuffer, LPBYTE lpOutBuffer,
                                                  DWORD cbOutBuffer, LPDWORD pcbReturned) {
    Tracef(L"GetPrinterDataFromPort hPort=%p ctl=%u val=%ls", hPort, ControlID,
           pValueName ? pValueName : L"(null)");
    return FALSE;  // not needed; spooler tolerates failure
}

extern "C" BOOL WINAPI PL_SetPortTimeOuts(HANDLE hPort, PPORTTIMOUTS timeouts,
                                          DWORD reserved) {
    Tracef(L"SetPortTimeOuts hPort=%p mo=%ld fo=%ld w=%ld", hPort,
           timeouts ? timeouts->dwMultipleOpen : -1,
           timeouts ? timeouts->dwFirstOpen : -1,
           timeouts ? timeouts->dwWait : -1);
    return TRUE;
}

extern "C" BOOL WINAPI PL_XcvOpenPortW(HANDLE hMonitor, LPCWSTR pszObject,
                                       ACCESS_MASK GrantedAccess, PHANDLE phXcv) {
    *phXcv = NULL;
    return TRUE;
}

extern "C" BOOL WINAPI PL_XcvDataPortW(HANDLE hXcv, LPCWSTR pszDataName,
                                       PBYTE pInputData, DWORD cbInputData,
                                       PBYTE pOutputData, DWORD cbOutputData,
                                       PDWORD pcbOutputNeeded, PDWORD pdwStatus) {
    *pdwStatus = ERROR_NOT_SUPPORTED;
    return FALSE;
}

extern "C" BOOL WINAPI PL_XcvClosePort(HANDLE hXcv) { return TRUE; }

extern "C" VOID WINAPI PL_Shutdown(HANDLE hMonitor) {}

extern "C" BOOL WINAPI PL_OpenPortExW(HANDLE hMonitor, LPWSTR pPortName,
                                      LPWSTR pPrinterName, HANDLE* pHandle,
                                      LPWSTR pMonitorName, DWORD Level,
                                      LPBYTE pSupportInfo) {
    HANDLE* out = pHandle ? pHandle : (HANDLE*)pPrinterName;
    if (!out) return FALSE;
    PPL_PORT p = OpenPortInternal(pPortName, pPrinterName, 20000, FALSE);
    if (!p) return FALSE;
    *out = p;
    return TRUE;
}

extern "C" VOID WINAPI PL_ShutdownPrintMonitor(HANDLE hMonitor) {}

// ---------------- v1 (ANSI, pre-MONITOR2) interface -------------------------
// v3 drivers route through the legacy v1 monitor API: localspl resolves the
// ANSI entry-point names (OpenPort, WritePort, ...) via GetProcAddress and
// calls them with v1 signatures — NO leading HANDLE. The spooler passes the
// string returned from OpenPort back verbatim to StartDocPort/WritePort/
// ClosePort/..., so the "port name" acts as an opaque handle: we return an
// allocated copy of the port name and keep a table mapping it to the port
// struct (the same pattern tcpmon uses).
// .def maps: OpenPort -> PL1_OpenPort, etc. (W-suffixed names stay v2).

#define V1_MAX_PORTS 16
typedef struct {
    CHAR*   szKey;   // allocated ANSI port-name string handed to the spooler
    PPL_PORT p;
} V1_ENTRY;

static V1_ENTRY g_v1[V1_MAX_PORTS];
static CRITICAL_SECTION g_v1lock;

static PPL_PORT V1Find(LPCSTR key) {
    if (!key) return NULL;
    EnterCriticalSection(&g_v1lock);
    PPL_PORT p = NULL;
    for (int i = 0; i < V1_MAX_PORTS; i++)
        if (g_v1[i].szKey && lstrcmpA(g_v1[i].szKey, key) == 0) { p = g_v1[i].p; break; }
    LeaveCriticalSection(&g_v1lock);
    return p;
}

extern "C" BOOL WINAPI PL1_OpenPort(LPSTR pName, HWND hwnd, LPSTR pMonitorName,
                                    LPSTR* pPort) {
    // During AddPrinter the spooler probes the port with no agent running:
    // always succeed (allowDisconnected) and connect lazily on first write.
    WCHAR wname[256];
    MultiByteToWideChar(CP_ACP, 0, pName ? pName : "", -1, wname, 256);
    PPL_PORT p = OpenPortInternal(wname, L"PrintLink Remote Printer", 3000, TRUE);
    if (!p) return FALSE;

    CHAR* key = (CHAR*)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY,
                                 (lstrlenA(pName) + 1) * sizeof(CHAR));
    if (!key) { HeapFree(GetProcessHeap(), 0, p); return FALSE; }
    lstrcpyA(key, pName);

    EnterCriticalSection(&g_v1lock);
    BOOL added = FALSE;
    for (int i = 0; i < V1_MAX_PORTS; i++)
        if (!g_v1[i].szKey) { g_v1[i].szKey = key; g_v1[i].p = p; added = TRUE; break; }
    LeaveCriticalSection(&g_v1lock);
    if (!added) {
        HeapFree(GetProcessHeap(), 0, key); HeapFree(GetProcessHeap(), 0, p);
        SetLastError(ERROR_OUTOFMEMORY);
        return FALSE;
    }
    *pPort = key;
    return TRUE;
}

extern "C" BOOL WINAPI PL1_StartDocPort(LPSTR pPortName, LPSTR pPrinterName,
                                        DWORD JobId, DWORD Level, LPBYTE pDocInfo) {
    PPL_PORT p = V1Find(pPortName);
    if (!p) { SetLastError(ERROR_INVALID_HANDLE); return FALSE; }
    p->jobId = JobId;
    return TRUE;
}

extern "C" BOOL WINAPI PL1_WritePort(LPSTR pPortName, LPBYTE pBuf, DWORD cbBuf,
                                     LPDWORD pcbWritten) {
    PPL_PORT p = V1Find(pPortName);
    if (!p) { SetLastError(ERROR_INVALID_HANDLE); return FALSE; }
    __try {
        if (!EnsureConnected(p)) { if (pcbWritten) *pcbWritten = 0; return FALSE; }
    } __except (EXCEPTION_EXECUTE_HANDLER) { return FALSE; }
    return GuardedWritePort(p, pBuf, cbBuf, pcbWritten);
}

extern "C" BOOL WINAPI PL1_ReadPort(LPSTR pPortName, LPBYTE pBuf, DWORD cbBuf,
                                    LPDWORD pcbRead) {
    // Same no-data answer as PL_ReadPort (see above): a successful read with
    // zero bytes keeps the spooler on the normal WritePort path.
    __try {
        if (!pBuf || !pcbRead) { SetLastError(ERROR_INVALID_PARAMETER); return FALSE; }
        *pcbRead = 0;
        return TRUE;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return FALSE;
    }
}

extern "C" BOOL WINAPI PL1_EndDocPort(LPSTR pPortName) { return TRUE; }

extern "C" BOOL WINAPI PL1_ClosePort(LPSTR pPortName) {
    EnterCriticalSection(&g_v1lock);
    for (int i = 0; i < V1_MAX_PORTS; i++) {
        if (g_v1[i].szKey && lstrcmpA(g_v1[i].szKey, pPortName) == 0) {
            PPL_PORT p = g_v1[i].p;
            g_v1[i].szKey = NULL; g_v1[i].p = NULL;
            LeaveCriticalSection(&g_v1lock);
            HeapFree(GetProcessHeap(), 0, pPortName);
            if (p->hPipe) { FlushFileBuffers(p->hPipe); CloseHandle(p->hPipe); }
            HeapFree(GetProcessHeap(), 0, p);
            return TRUE;
        }
    }
    LeaveCriticalSection(&g_v1lock);
    return TRUE;  // unknown/closed handle — treat as success
}

extern "C" BOOL WINAPI PL1_EnumPorts(LPSTR pName, DWORD Level, LPBYTE pPorts,
                                     DWORD cbBuf, LPDWORD pcbNeeded,
                                     LPDWORD pcbReturned) {
    if (Level != 1) { SetLastError(ERROR_INVALID_LEVEL); return FALSE; }
    *pcbNeeded = 0; *pcbReturned = 0;

    HKEY hk = NULL;
    if (RegOpenKeyExW(HKEY_LOCAL_MACHINE, PORTS_KEY, 0, KEY_READ, &hk) != ERROR_SUCCESS)
        return TRUE;  // no ports configured — legitimate empty answer

    DWORD count = 0, needed = 0;
    WCHAR name[512]; DWORD nameLen;
    for (DWORD i = 0; ; i++) {
        nameLen = 512;
        LONG r = RegEnumKeyExW(hk, i, name, &nameLen, NULL, NULL, NULL, NULL);
        if (r != ERROR_SUCCESS) break;
        count++;
        needed += sizeof(PORT_INFO_1A) + nameLen + 1;
    }

    *pcbNeeded = needed;
    if (cbBuf < needed) { RegCloseKey(hk); SetLastError(ERROR_INSUFFICIENT_BUFFER); return FALSE; }

    PORT_INFO_1A* arr = (PORT_INFO_1A*)pPorts;
    char* str = (char*)(pPorts + count * sizeof(PORT_INFO_1A));
    DWORD n = 0;
    for (DWORD i = 0; ; i++) {
        nameLen = 512;
        LONG r = RegEnumKeyExW(hk, i, name, &nameLen, NULL, NULL, NULL, NULL);
        if (r != ERROR_SUCCESS) break;
        int len = WideCharToMultiByte(CP_ACP, 0, name, nameLen, str, 512, NULL, NULL);
        if (len <= 0) continue;  // skip entries that fail to convert (defensive)
        arr[n].pName = str;
        str += len + 1;
        n++;
    }
    RegCloseKey(hk);
    *pcbReturned = n;
    return TRUE;
}

extern "C" BOOL WINAPI PL1_AddPort(LPSTR pName, HWND hwnd, LPSTR pMonitorName) {
    SetLastError(ERROR_NOT_SUPPORTED);  // ports are created by PrintLinkSetup
    return FALSE;
}

extern "C" BOOL WINAPI PL1_ConfigurePort(LPSTR pName, HWND hwnd, LPSTR pPortName) {
    SetLastError(ERROR_NOT_SUPPORTED);
    return FALSE;
}

extern "C" BOOL WINAPI PL1_DeletePort(LPSTR pName, HWND hwnd, LPSTR pPortName) {
    SetLastError(ERROR_NOT_SUPPORTED);  // PrintLinkSetup removes ports
    return FALSE;
}

extern "C" BOOL WINAPI PL1_GetPrinterDataFromPort(LPSTR pPortName, LPSTR pPrinterName,
                                                  DWORD ControlID, LPSTR pValueName,
                                                  LPBYTE pBuffer, DWORD cbBuffer,
                                                  LPDWORD pcbNeeded) {
    return FALSE;  // not needed; spooler tolerates failure
}

extern "C" BOOL WINAPI PL1_SetPortTimeOuts(LPSTR pPortName, PPORTTIMOUTS timeouts,
                                           DWORD reserved) { return TRUE; }

extern "C" BOOL WINAPI PL1_XcvOpenPort(LPSTR pPortName, DWORD dwAccessRequired,
                                       PHANDLE pXcv) {
    *pXcv = NULL;
    return TRUE;
}

extern "C" BOOL WINAPI PL1_XcvDataPort(HANDLE hXcv, LPSTR pszDataName,
                                       PBYTE pInputData, DWORD cbInputData,
                                       PBYTE pOutputData, DWORD cbOutputData,
                                       PDWORD pcbOutputNeeded, PDWORD pdwStatus) {
    *pdwStatus = ERROR_NOT_SUPPORTED;
    return FALSE;
}

extern "C" BOOL WINAPI PL1_XcvClosePort(HANDLE hXcv) { return TRUE; }

extern "C" VOID WINAPI PL1_Shutdown() {}

// Monitor object returned by InitializePrintMonitor2 (build 26100 contract):
// obj[0] = sanity pointer, obj[8..] = 17-entry MONITOR2 dispatch table,
// obj[0x90] = pMonitorInit copy (localspl gate: [entry+0xE8] must be non-zero,
// i.e. the 18th qword of the copied object), padded to 0xB8 bytes total
// (localspl copies up to 0xB8).
typedef struct {
    LPVOID pVtable;       // 0x00 — any user-space pointer >= 0xB8 (as DWORD)
    LPVOID fn[17];        // 0x08..0x90 — MONITOR2 dispatch table
    LPVOID pMonitorInit;  // 0x90 — must be non-NULL
    BYTE   pad[0xB8 - 8 - 17 * 8 - 8];
} PL_MONITOR_OBJECT, *PPL_MONITOR_OBJECT;
static_assert(sizeof(PL_MONITOR_OBJECT) == 0xB8, "monitor object must be 0xB8");

static PPL_MONITOR_OBJECT g_monitor;

static void Trace(const wchar_t* msg) {
    HANDLE f = CreateFileW(L"C:\\Windows\\Temp\\plmon.log", FILE_APPEND_DATA,
                           FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_ALWAYS, 0, NULL);
    if (f != INVALID_HANDLE_VALUE) {
        wchar_t head[64];
        wsprintfW(head, L"[t=%u tID=%u] ", GetTickCount(),
                  (DWORD)(ULONG_PTR)GetCurrentThreadId());
        DWORD w;
        WriteFile(f, head, (DWORD)lstrlenW(head) * 2, &w, NULL);
        WriteFile(f, msg, (DWORD)lstrlenW(msg) * 2, &w, NULL);
        const wchar_t* nl = L"\r\n"; WriteFile(f, nl, 4, &w, NULL);
        CloseHandle(f);
    }
}

static void Tracef(const wchar_t* fmt, ...) {
    wchar_t buf[512];
    va_list ap; va_start(ap, fmt);
    vswprintf_s(buf, 512, fmt, ap);
    va_end(ap);
    Trace(buf);
}

static void TraceBytes(const wchar_t* label, const void* p, DWORD cb) {
    wchar_t buf[1024];
    wchar_t* q = buf;
    q += wsprintfW(q, L"%ls: %u bytes @%p: ", label, cb, p);
    if (p && cb) {
        const BYTE* b = (const BYTE*)p;
        DWORD n = cb > 24 ? 24 : cb;
        if (IsBadReadPtr(b, n)) {
            // 26100 despool tail reads hand pBuf past the mapped region
            // (buffer base + chunk + 3); an unguarded dump AVs here and the
            // SEH swallows it, silently failing the read (job -> retry -> error).
            lstrcpyW(q, L"(unreadable)"); q += lstrlenW(L"(unreadable)");
        } else {
            for (DWORD i = 0; i < n; i++) q += wsprintfW(q, L"%02x ", b[i]);
            if (cb > 24) q += wsprintfW(q, L"...");
        }
    } else {
        lstrcpyW(q, L"(null)"); q += lstrlenW(L"(null)");
    }
    Trace(buf);
}

// Returns the monitor object pointer (NOT BOOL!) — see header comment.
extern "C" LPVOID WINAPI PL_InitializePrintMonitor2(PMONITORINIT pMonitorInit,
                                                    LPHANDLE phMonitor) {
    Trace(L"InitializePrintMonitor2 called");
    if (!phMonitor) return NULL;
    if (!g_monitor) {
        g_monitor = (PPL_MONITOR_OBJECT)HeapAlloc(GetProcessHeap(),
                                                  HEAP_ZERO_MEMORY, 0xB8);
        if (!g_monitor) return NULL;
        g_monitor->pVtable = g_monitor;  // any user-space pointer >= 0xB8
        g_monitor->fn[0]  = (LPVOID)PL_EnumPortsW;
        g_monitor->fn[1]  = (LPVOID)PL_OpenPortW;
        g_monitor->fn[2]  = (LPVOID)PL_StartDocPortW;
        g_monitor->fn[3]  = (LPVOID)PL_WritePort;
        g_monitor->fn[4]  = (LPVOID)PL_ReadPort;
        g_monitor->fn[5]  = (LPVOID)PL_EndDocPort;
        g_monitor->fn[6]  = (LPVOID)PL_ClosePort;
        g_monitor->fn[7]  = (LPVOID)PL_AddPortW;
        g_monitor->fn[8]  = (LPVOID)PL_AddPortExW;
        g_monitor->fn[9]  = (LPVOID)PL_ConfigurePortW;
        g_monitor->fn[10] = (LPVOID)PL_DeletePortW;
        g_monitor->fn[11] = (LPVOID)PL_GetPrinterDataFromPortW;
        g_monitor->fn[12] = (LPVOID)PL_SetPortTimeOuts;
        g_monitor->fn[13] = (LPVOID)PL_XcvOpenPortW;
        g_monitor->fn[14] = (LPVOID)PL_XcvDataPortW;
        g_monitor->fn[15] = (LPVOID)PL_XcvClosePort;
        g_monitor->fn[16] = (LPVOID)PL_Shutdown;
        g_monitor->pMonitorInit = pMonitorInit;
        Trace(L"  monitor object allocated");
    }
    *phMonitor = g_monitor;
    Trace(L"  returning object");
    return g_monitor;
}

BOOL WINAPI DllMain(HINSTANCE hinst, DWORD reason, LPVOID reserved) {
    if (reason == DLL_PROCESS_ATTACH) {
        InitializeCriticalSection(&g_v1lock);
        DWORD n = GetEnvironmentVariableW(L"PLMONPIPE", g_pipeName,
                                          (DWORD)(sizeof(g_pipeName) / sizeof(wchar_t)));
        if (n == 0 || n >= (DWORD)(sizeof(g_pipeName) / sizeof(wchar_t))) {
            lstrcpynW(g_pipeName, PIPE_NAME, (int)(sizeof(g_pipeName) / sizeof(wchar_t)));
        }
        Trace(L"DllMain attach");
        Tracef(L"  pipe=%ls", g_pipeName);
    }
    else if (reason == DLL_PROCESS_DETACH) {
        DeleteCriticalSection(&g_v1lock);
    }
    return TRUE;
}
