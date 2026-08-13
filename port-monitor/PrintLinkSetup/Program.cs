// PrintLinkSetup — registers the port monitor, port, and "PrintLink Remote" printer.
// Usage (elevated):
//   PrintLinkSetup.exe install   -> register monitor DLL, add port + printer
//   PrintLinkSetup.exe uninstall -> remove printer, port, and monitor
using System;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.ServiceProcess;
using Microsoft.Win32;

class PrintLinkSetup
{
    const string MonitorName  = "PrintLinkMonitor";
    const string MonitorDll   = "PrintLinkMonitor.dll";
    const string PortName     = "PrintLink:";
    const string PrinterName  = "PrintLink Remote Printer";
    // Driver fallback chain. v4 drivers (e.g. 'Microsoft Print To PDF') reject
    // third-party port monitors, so only v3 drivers can be used here. The
    // v3 XPS driver is an optional feature on some builds; 'Generic / Text
    // Only' ships in the inbox ntprint.inf driver store on every Windows and
    // is installed on demand by EnsureDriver().
    static readonly string[] DriverCandidates =
    {
        "Microsoft XPS Document Writer",
        "Generic / Text Only",
    };

    [DllImport("winspool.drv", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool AddMonitor(string pName, uint Level, ref MONITOR_INFO_2 pMonitors);

    [DllImport("winspool.drv", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool DeleteMonitor(string pName, string pEnvironment, string pMonitorName);

    [DllImport("winspool.drv", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool AddPrinter(string pName, uint Level, ref PRINTER_INFO_2 pPrinter);

    [DllImport("winspool.drv", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool DeletePrinter(IntPtr hPrinter);

    [DllImport("winspool.drv", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool OpenPrinter(string pPrinterName, out IntPtr phPrinter, IntPtr pDefault);

    [DllImport("winspool.drv", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool ClosePrinter(IntPtr hPrinter);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int MessageBox(IntPtr hWnd, string text, string caption, uint type);

    [DllImport("winspool.drv", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool AddPrinterDriverEx(string pName, uint Level,
                                          ref DRIVER_INFO_6 pDriverInfo, uint dwFlag);

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct DRIVER_INFO_6
    {
        public string pName, pEnvironment, pDriverPath, pDataFile, pConfigFile,
                      pHelpFile, pMonitorName, pDefaultDataType, pszzDependentFiles,
                      pInfPath;
        public uint pDriverAttributes;
        public string pszzCoreDriverDependencies;
        public long ftMinInboxDriverVerDate;
        public ulong dwlMinInboxDriverVerVersion;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct MONITOR_INFO_2 { public string pName, pEnvironment, pDLLName; }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct PRINTER_INFO_2
    {
        public string pServerName, pPrinterName, pShareName, pPortName,
                      pDriverName, pComment, pLocation, pDevMode, pSepFile,
                      pPrintProcessor, pDatatype, pParameters;
        public IntPtr pSecurityDescriptor;
        public uint Attributes, Priority, DefaultPriority, StartTime,
                    UntilTime, Status, cJobs, AveragePPM;
    }

    static int Main(string[] args)
    {
        bool install = args.Length == 0 || args[0] == "install";
        try
        {
            if (install) { Install(); Console.WriteLine("PrintLink printer installed."); }
            else         { Uninstall(); Console.WriteLine("PrintLink printer removed."); }
            return 0;
        }
        catch (Exception e)
        {
            Console.Error.WriteLine("ERROR: " + e.Message);
            // Installers run us hidden; surface failures visually too.
            MessageBox(IntPtr.Zero, e.Message, "PrintLink Setup failed", 0x10 /* MB_ICONERROR */);
            return 1;
        }
    }

    // Spoolsv spawns a PrintIsolationHost (sandbox) that loads the monitor DLL
    // to despool jobs; stopping the spooler does NOT kill it. It must be
    // terminated before the System32 DLL file can be replaced/removed.
    static void StopIsolationHosts()
    {
        try
        {
            foreach (var p in System.Diagnostics.Process.GetProcessesByName("PrintIsolationHost"))
                p.Kill();
        }
        catch (Exception) { /* best effort; the copy retries below anyway */ }
    }

    static void WriteSystem32Dll(string dll, bool copy)
    {
        var target = Path.Combine(Environment.SystemDirectory, MonitorDll);
        // The sandbox host may still be releasing the DLL; retry briefly.
        for (int attempt = 0; ; attempt++)
        {
            try
            {
                if (copy) File.Copy(dll, target, true);
                else      File.Delete(target);
                return;
            }
            catch (IOException) when (attempt < 10)
            {
                System.Threading.Thread.Sleep(500);
            }
        }
    }

    static void Install()
    {
        // The spooler loads the monitor DLL from System32 to verify it during
        // AddMonitor, so copy it there from this exe's own directory first.
        // Spoolsv and PrintIsolationHost both lock the DLL while loaded, so
        // the copy must happen with the spooler stopped and sandbox hosts dead.
        var dll = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, MonitorDll);
        if (!File.Exists(dll))
            throw new FileNotFoundException("PrintLinkMonitor.dll not found next to PrintLinkSetup.exe.", dll);
        using (var spooler = new ServiceController("Spooler"))
        {
            spooler.Stop();
            spooler.WaitForStatus(ServiceControllerStatus.Stopped, TimeSpan.FromSeconds(30));
            try
            {
                // Drop a printer left behind by a previous failed uninstall;
                // with the spooler (and PrintIsolationHost) stopped the
                // registry delete cannot be blocked by a sandboxed printer.
                // Otherwise AddPrinter fails with 1802 on reinstall.
                Registry.LocalMachine.DeleteSubKeyTree(
                    @"SYSTEM\CurrentControlSet\Control\Print\Printers\" + PrinterName, false);
                StopIsolationHosts();
                WriteSystem32Dll(dll, copy: true);
            }
            finally
            {
                spooler.Start();
                spooler.WaitForStatus(ServiceControllerStatus.Running, TimeSpan.FromSeconds(30));
            }
        }

        var mi2 = new MONITOR_INFO_2 { pName = MonitorName, pEnvironment = "Windows x64",
                                       pDLLName = MonitorDll };
        if (!AddMonitor(null, 2, ref mi2))
        {
            // Re-install/repair: the monitor is already registered from a
            // previous attempt — that's fine, the fresh DLL was just copied.
            int err = Marshal.GetLastWin32Error();
            if (err != 183 /* ERROR_ALREADY_EXISTS */
                && err != 3006 /* ERROR_ADD_MONITOR: already installed */)
                ThrowLast("AddMonitor (run elevated; DLL must be in C:\\Windows\\System32)", err);
        }

        // The spooler only sees ports that are registered under the monitor's
        // Ports key; AddPrinter on a missing port fails with ERROR_UNKNOWN_PORT.
        using (var ports = Registry.LocalMachine.CreateSubKey(
            @"SYSTEM\CurrentControlSet\Control\Print\Monitors\" + MonitorName + @"\Ports"))
            ports.CreateSubKey(PortName);

        // Spoolsv caches the port list at startup, so a freshly created port
        // key is invisible to AddPrinter until the spooler is restarted.
        using (var spooler = new ServiceController("Spooler"))
        {
            spooler.Stop();
            spooler.WaitForStatus(ServiceControllerStatus.Stopped, TimeSpan.FromSeconds(30));
            spooler.Start();
            spooler.WaitForStatus(ServiceControllerStatus.Running, TimeSpan.FromSeconds(30));
        }

        var pi2 = new PRINTER_INFO_2
        {
            pPrinterName = PrinterName, pPortName = PortName, pDriverName = FindDriver(),
            pPrintProcessor = "winprint", pDatatype = "RAW"
        };
        // Right after a restart spoolsv may still be initializing its print
        // providers; retry transient 1802/1795 failures briefly.
        for (int attempt = 0; ; attempt++)
        {
            if (AddPrinter(null, 2, ref pi2))
                return;
            int err = Marshal.GetLastWin32Error();
            if (attempt >= 4 || (err != 1802 && err != 1795))
                ThrowLast("AddPrinter", err);
            System.Threading.Thread.Sleep(1000);
        }
    }

    static string FindDriver()
    {
        // v3 drivers only (v4 rejects third-party port monitors): return the
        // first installed candidate, else install 'Generic / Text Only' from
        // the inbox driver store, else fall back to any installed v3 driver
        // (printing goes through our port monitor, so any renderer works).
        string root = @"SYSTEM\CurrentControlSet\Control\Print\Environments\Windows x64\Drivers";
        var installedV3 = new System.Collections.Generic.List<string>();
        using (var drv = Registry.LocalMachine.OpenSubKey(root + "\\Version-3"))
            if (drv != null)
                foreach (var name in drv.GetSubKeyNames())
                    installedV3.Add(name);
        foreach (var wanted in DriverCandidates)
        {
            var hit = installedV3.Find(
                n => n.Equals(wanted, StringComparison.OrdinalIgnoreCase));
            if (hit != null) return hit;
        }
        if (installedV3.Count > 0) return installedV3[0];
        EnsureGenericDriver();
        return "Generic / Text Only";
    }

    static void EnsureGenericDriver()
    {
        // Install the inbox 'Generic / Text Only' driver from ntprint.inf.
        string store = Path.Combine(Environment.SystemDirectory,
            "DriverStore", "FileRepository");
        string inf = Directory.GetDirectories(store, "ntprint.inf_amd64_*")
            .OrderByDescending(d => d).Select(d => Path.Combine(d, "ntprint.inf"))
            .FirstOrDefault();
        if (inf == null)
            throw new InvalidOperationException(
                "Inbox driver store ntprint.inf not found.");
        var dri = new DRIVER_INFO_6
        {
            pName = "Generic / Text Only",
            pEnvironment = "Windows x64",
            pDefaultDataType = "RAW",
            pInfPath = inf,
        };
        if (!AddPrinterDriverEx(null, 6, ref dri, 0))
            ThrowLast("AddPrinterDriverEx ('Generic / Text Only')",
                      Marshal.GetLastWin32Error());
    }

    static void Uninstall()
    {
        // DeletePrinter needs a live spooler, but fails with ERROR_ACCESS_DENIED
        // (5) when a PrintIsolationHost sandbox still owns the printer. The
        // fallback below removes the printer key directly with the spooler
        // stopped, which cannot be blocked by the sandbox.
        bool printerDeleted = false;
        if (OpenPrinter(PrinterName, out var h, IntPtr.Zero))
        {
            printerDeleted = DeletePrinter(h);
            ClosePrinter(h);
        }

        Registry.LocalMachine.DeleteSubKeyTree(
            @"SYSTEM\CurrentControlSet\Control\Print\Monitors\" + MonitorName + @"\Ports", false);

        // DeleteMonitor and the System32 DLL file both need the spooler
        // stopped (spoolsv / PrintIsolationHost keep the monitor DLL loaded).
        using (var spooler = new ServiceController("Spooler"))
        {
            spooler.Stop();
            spooler.WaitForStatus(ServiceControllerStatus.Stopped, TimeSpan.FromSeconds(30));
            try
            {
                if (!printerDeleted)
                    Registry.LocalMachine.DeleteSubKeyTree(
                        @"SYSTEM\CurrentControlSet\Control\Print\Printers\" + PrinterName, false);
                if (!DeleteMonitor(null, "Windows x64", MonitorName))
                    // Best-effort fallback: the registration is just a registry
                    // key; with the spooler stopped it can be removed directly.
                    Registry.LocalMachine.DeleteSubKeyTree(
                        @"SYSTEM\CurrentControlSet\Control\Print\Monitors\" + MonitorName, false);
                StopIsolationHosts();
                WriteSystem32Dll(null, copy: false);   // delete the DLL
            }
            finally
            {
                spooler.Start();
                spooler.WaitForStatus(ServiceControllerStatus.Running, TimeSpan.FromSeconds(30));
            }
        }
    }

    static void ThrowLast(string what, int err)
        => throw new System.ComponentModel.Win32Exception(err,
               what + " failed (Win32 error " + err + ")");
}