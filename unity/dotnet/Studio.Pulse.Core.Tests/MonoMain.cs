// Console runner for the [PulseTest] methods: used on machines without the .NET SDK
// (unity/dotnet/mono-test.sh compiles the core and the tests with Unity's bundled
// Mono). CI runs the same methods through `dotnet test` (XunitAdapter.cs).
using System;
using System.Diagnostics;

namespace Studio.Pulse.Tests
{
    public static class MonoMain
    {
        public static int Main(string[] args)
        {
            int passed = 0, failed = 0, skipped = 0;
            foreach (var m in PulseTestRunner.Discover())
            {
                string name = PulseTestRunner.NameOf(m);
                if (args.Length > 0 && name.IndexOf(args[0], StringComparison.OrdinalIgnoreCase) < 0) continue;
                var sw = Stopwatch.StartNew();
                string r = PulseTestRunner.Run(m);
                if (r == null) { passed++; Console.WriteLine("PASS  " + name + " (" + sw.ElapsedMilliseconds + " ms)"); }
                else if (r.StartsWith("SKIP: ", StringComparison.Ordinal)) { skipped++; Console.WriteLine("SKIP  " + name + ": " + r.Substring(6)); }
                else { failed++; Console.WriteLine("FAIL  " + name + "\n" + r); }
            }
            Console.WriteLine(passed + " passed, " + failed + " failed, " + skipped + " skipped");
            return failed == 0 ? 0 : 1;
        }
    }
}
