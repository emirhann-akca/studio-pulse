// Compiled only when Unity LevelPlay 8.9+ is in the project: the UPM package
// com.unity.services.levelplay (versionDefines in Studio.Pulse.LevelPlay.asmdef) or the same
// assembly imported into Assets (Editor/PulseAdSdkDefines.cs sets STUDIO_PULSE_LEVELPLAY).
// Signatures checked against com.unity.services.levelplay 8.9.0 and 9.6.0
// (LevelPlay.OnImpressionDataReady, LevelPlayImpressionData). 9.5.0 marked the global event
// obsolete in favour of each ad object's OnAdImpressionDataReady; 9.6.0 still raises it from both
// native bridges (AndroidLevelPlaySdk.setUnityImpressionDataListener, IosLevelPlaySdk
// OnImpressionSuccess), so the bridge keeps using it and the asmdef stops at 10.0.0. Older
// ironSource plug-ins (IronSourceEvents.onImpressionDataReadyEvent) are not bridged. The ads
// package's LevelPlay provider also reports each ad's OnAdImpressionDataReady; the auction id
// makes the two reports of one impression count once (PulseAdRevenueGate.FirstReport).
using Unity.Services.LevelPlay;
using UnityEngine;
using UnityEngine.Scripting;

[assembly: AlwaysLinkAssembly]

namespace Studio.Pulse.AdBridges
{
    /// <summary>
    /// Forwards LevelPlay's impression data (revenue in USD) to ad_impression. No game code.
    /// Off with PulseOptions.AutoCaptureAdRevenue = false or the STUDIO_PULSE_NO_AD_BRIDGES define.
    /// </summary>
    [Preserve]
    static class PulseLevelPlayBridge
    {
        static bool _subscribed;

        [Preserve]
        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterAssembliesLoaded)]
        static void Subscribe()
        {
            if (_subscribed) return;
            _subscribed = true;
            PulseAdBridge.Register(PulseAdRevenueMap.PlatformLevelPlay);
#pragma warning disable CS0618 // the only global impression callback; see the header
            LevelPlay.OnImpressionDataReady += Forward;
#pragma warning restore CS0618
        }

        static void Forward(LevelPlayImpressionData data)
        {
            if (data == null) return;
            PulseAdRevenue r;
            if (PulseAdRevenueMap.FromLevelPlay(data.AdFormat, data.Revenue, data.Precision, data.AdNetwork,
                    data.MediationAdUnitName, data.Placement, data.AuctionId, out r))
            {
                PulseAdBridge.Report(r);
            }
        }
    }
}
