// Device tier classification. Pure C#: no UnityEngine use (DeviceTier.Current
// in PerfSampler.cs feeds it SystemInfo values).
namespace Studio.Pulse
{
    public static class DeviceTierRules
    {
        /// <summary>
        /// low:  less than 3.5 GB RAM, or 4 or fewer CPU cores, or a GPU reporting less than 512 MB
        /// high: at least 7 GB RAM and at least 6 cores (and >= 2 GB GPU memory when reported)
        /// mid:  everything else
        /// </summary>
        public static string Classify(int systemMemoryMb, int processorCount, int graphicsMemoryMb)
        {
            bool lowRam = systemMemoryMb > 0 && systemMemoryMb < 3500;
            bool fewCores = processorCount > 0 && processorCount <= 4;
            bool weakGpu = graphicsMemoryMb > 0 && graphicsMemoryMb < 512;
            if (lowRam || fewCores || weakGpu) return "low";
            bool gpuOk = graphicsMemoryMb <= 0 || graphicsMemoryMb >= 2048;
            if (systemMemoryMb >= 7000 && processorCount >= 6 && gpuOk) return "high";
            return "mid";
        }
    }
}
