// `dotnet test` entry: one xunit theory case per [PulseTest] method (CI). A skipped
// [PulseTest] (e.g. the E2E test without PULSE_E2E_URL) passes with its reason logged.
using System.Collections.Generic;
using System.Linq;
using Xunit;
using Xunit.Abstractions;

namespace Studio.Pulse.Tests
{
    public sealed class PulseTests
    {
        readonly ITestOutputHelper _out;

        public PulseTests(ITestOutputHelper output)
        {
            _out = output;
        }

        public static IEnumerable<object[]> Cases()
        {
            return PulseTestRunner.Discover().Select(m => new object[] { PulseTestRunner.NameOf(m) });
        }

        [Theory]
        [MemberData(nameof(Cases))]
        public void Run(string name)
        {
            var m = PulseTestRunner.Discover().Single(x => PulseTestRunner.NameOf(x) == name);
            string r = PulseTestRunner.Run(m);
            if (r != null && r.StartsWith("SKIP: ")) { _out.WriteLine(r); return; }
            Assert.True(r == null, r);
        }
    }
}
