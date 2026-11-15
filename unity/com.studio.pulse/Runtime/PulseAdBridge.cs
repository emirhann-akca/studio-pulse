using System;
using System.Diagnostics;

namespace Studio.Pulse
{
    /// <summary>
    /// Entry point for ad SDK bridges: the optional assemblies Studio.Pulse.AppLovinMax and
    /// Studio.Pulse.LevelPlay (compiled only when that SDK is in the project) and any bridge a
    /// game writes for another mediation SDK. Safe from any thread and before Pulse.Init:
    /// impressions that arrive earlier are held (at most 32) and recorded by Init.
    /// </summary>
    public static class PulseAdBridge
    {
        /// <summary>Call once when the bridge subscribes: manual AdImpression calls for this platform are then ignored.</summary>
        public static void Register(string adPlatform)
        {
            Pulse.AdGate.Register(adPlatform);
        }

        /// <summary>One impression from the ad SDK's revenue callback (built with PulseAdRevenueMap).</summary>
        public static void Report(PulseAdRevenue revenue)
        {
            if (!revenue.IsValid || !Pulse.AdGate.FirstReport(revenue)) return;
            Action<PulseAdRevenue> listeners = Impression;
            if (listeners != null)
            {
                try { listeners(revenue); }
                catch (Exception ex) { UnityEngine.Debug.LogWarning("[Studio.Pulse] ad revenue listener: " + ex.Message); }
            }
            Pulse.RecordAutoAdRevenue(revenue);
        }

        /// <summary>
        /// Every impression a bridge reports, whether or not Pulse is initialised or auto-capture
        /// is on: the hook an MMP adapter (Studio.Pulse.Ads.AppsFlyer) forwards ad revenue from.
        /// Raised on the ad SDK's callback thread (MAX: a background thread by default).
        /// </summary>
        public static event Action<PulseAdRevenue> Impression;
    }

    public static partial class Pulse
    {
        internal static readonly PulseAdRevenueGate AdGate = new PulseAdRevenueGate();
        static readonly Stopwatch AdClock = Stopwatch.StartNew();
        static readonly object AdWarnLock = new object();
        static string _adWarned;

        /// <summary>Automatic / manual / duplicate ad impression counters (local diagnostics).</summary>
        public static PulseAdRevenueGate AdRevenue
        {
            get { return AdGate; }
        }

        static long AdClockMs()
        {
            return AdClock.ElapsedMilliseconds;
        }

        internal static void RecordAutoAdRevenue(PulseAdRevenue r)
        {
            if (!r.IsValid) return;
            PulseCore core = _core;
            if (core == null)
            {
                AdGate.Hold(r);
                return;
            }
            if (AdGate.AcceptAuto(r, AdClockMs()))
            {
                core.AdImpression(r.Platform, r.Format, r.Value, r.Currency, r.Source, r.AdUnit, r.Placement, r.Precision);
            }
        }

        static void RecordHeldAdRevenue()
        {
            foreach (PulseAdRevenue r in AdGate.TakePending()) RecordAutoAdRevenue(r);
        }

        static void WarnAdOnce(string why)
        {
            if (why == null) return;
            lock (AdWarnLock)
            {
                if (_adWarned == why) return;
                _adWarned = why;
            }
            UnityEngine.Debug.LogWarning("[Studio.Pulse] " + why);
        }
    }
}
