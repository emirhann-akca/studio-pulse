// E2E driver: plays a realistic session through the Unity SDK core against a running
// `pulse collect` and prints a JSON summary. Usage:
//   Studio.Pulse.Driver <collector url> <write key> <data dir> <platform> <store> [app id]
using System;
using Studio.Pulse.Tests;

namespace Studio.Pulse.Driver
{
    public static class Program
    {
        public static int Main(string[] args)
        {
            if (args.Length < 5)
            {
                Console.Error.WriteLine("usage: <url> <write key> <data dir> <platform> <store> [app id]");
                return 2;
            }
            var r = E2EScenario.Run(args[0], args[1], args[2], args[3], args[4], args.Length > 5 ? args[5] : "com.studio.e2e.unity");
            Console.WriteLine("{\"sdk\":\"unity/" + PulseCore.Version + "\",\"install_id\":\"" + r.InstallId + "\",\"recorded\":" + r.Recorded
                + ",\"sent\":" + r.Sent + ",\"queued\":" + r.Queued + ",\"rejected\":" + r.Rejected + ",\"last_status\":" + r.LastStatus + "}");
            return r.Queued == 0 && r.Sent == r.Recorded ? 0 : 1;
        }
    }
}
