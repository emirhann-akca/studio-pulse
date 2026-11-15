using System;
#if UNITY_IOS && !UNITY_EDITOR && STUDIO_PULSE_ATT
using System.Runtime.InteropServices;
#endif
using UnityEngine;

namespace Studio.Pulse.Ads
{
    /// <summary>
    /// The iOS App Tracking Transparency prompt, asked before an ad SDK that does not ask itself
    /// (LevelPlay: "request ATT authorization before calling LevelPlay.Init()"). The native part
    /// (Plugins/iOS/Ads/StudioPulseAtt.mm) and this call are compiled only with the STUDIO_PULSE_ATT
    /// define; without it, and on other platforms, Request answers at once without a prompt.
    /// The prompt text (NSUserTrackingUsageDescription) comes from studio_app.json privacy.attUsageDescription.
    /// </summary>
    public static class StudioAtt
    {
        public const int NotDetermined = 0, Restricted = 1, Denied = 2, Authorized = 3;
        /// <summary>No ATT here (not iOS, or STUDIO_PULSE_ATT not defined).</summary>
        public const int Unavailable = -1;

        /// <summary>True when this build can show the prompt.</summary>
        public static bool Available
        {
            get
            {
#if UNITY_IOS && !UNITY_EDITOR && STUDIO_PULSE_ATT
                return true;
#else
                return false;
#endif
            }
        }

#if UNITY_IOS && !UNITY_EDITOR && STUDIO_PULSE_ATT
        delegate void Callback(int status);

        [DllImport("__Internal")] static extern int StudioPulseAttStatus();
        [DllImport("__Internal")] static extern void StudioPulseAttRequest(Callback callback);

        static Action<int> _pending;

        [AOT.MonoPInvokeCallback(typeof(Callback))]
        static void OnAnswer(int status)
        {
            Action<int> done = _pending;
            _pending = null;
            StudioMainThread.Run(() => { if (done != null) done(status); });
        }
#endif

        /// <summary>The current status without prompting (Unavailable where there is no ATT).</summary>
        public static int Status
        {
            get
            {
#if UNITY_IOS && !UNITY_EDITOR && STUDIO_PULSE_ATT
                return StudioPulseAttStatus();
#else
                return Unavailable;
#endif
            }
        }

        /// <summary>
        /// Prompts once if the user has not answered yet; done(status) on the main thread. Users who
        /// already answered, and builds without ATT, get their status at once.
        /// </summary>
        public static void Request(Action<int> done)
        {
#if UNITY_IOS && !UNITY_EDITOR && STUDIO_PULSE_ATT
            int now = StudioPulseAttStatus();
            if (now != NotDetermined || _pending != null)
            {
                if (done != null) done(now);
                return;
            }
            _pending = done;
            StudioPulseAttRequest(OnAnswer);
#else
            if (done != null) done(Unavailable);
#endif
        }

        /// <summary>Logs, on an iOS build without STUDIO_PULSE_ATT, that no tracking prompt can be shown.</summary>
        public static void WarnIfMissing(string why)
        {
#if UNITY_IOS && !UNITY_EDITOR && !STUDIO_PULSE_ATT
            Debug.LogWarning("[Studio.Ads] " + why + ": define STUDIO_PULSE_ATT (iOS Scripting Define Symbols) to ask App Tracking Transparency");
#endif
        }
    }
}
