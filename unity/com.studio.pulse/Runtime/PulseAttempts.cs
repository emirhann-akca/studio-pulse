using System;
using UnityEngine;

namespace Studio.Pulse
{
    public static partial class Pulse
    {
        /// <summary>
        /// level_attempt: one event per finished attempt (won, lost, or ended again after a continue)
        /// with the help it used; on <see cref="PulseAttemptTrace.Rate"/> of installs also the tap trace.
        /// A no-op before Init; never throws. docs/SDK_UNITY.md "Level attempts".
        /// </summary>
        public static bool LevelAttempt(PulseAttempt attempt)
        {
            return _core != null && attempt != null && _core.LevelAttempt(attempt, PulseAttemptTrace.Sampled);
        }
    }

    /// <summary>
    /// Whether this install sends the tap trace of its attempts: chosen once with <see cref="Rate"/>
    /// and kept in PlayerPrefs (<see cref="PrefsKey"/>), so a player's skill can be read across
    /// attempts. Set Rate / PrefsKey before the first attempt.
    /// </summary>
    public static class PulseAttemptTrace
    {
        /// <summary>The key Acorn Mart 1.0.1 shipped with; installs that update keep their choice.</summary>
        public const string DefaultPrefsKey = "pulse_iz_ornek";

        public static float Rate = PulseAttempt.DefaultTraceRate;
        public static string PrefsKey = DefaultPrefsKey;

        /// <summary>Tests: a value overrides the install's choice.</summary>
        public static bool? Force;

        static int _choice = -1;

        /// <summary>The install's choice (made and stored on first use; false if PlayerPrefs fails).</summary>
        public static bool Sampled
        {
            get
            {
                if (Force.HasValue) return Force.Value;
                if (_choice < 0)
                {
                    try
                    {
                        int stored = PlayerPrefs.GetInt(PrefsKey, -1);
                        PulseInstallSample.Resolve(stored, Rate, () => UnityEngine.Random.value, out int store);
                        if (store != stored) PlayerPrefs.SetInt(PrefsKey, store);
                        _choice = store;
                    }
                    catch (Exception)
                    {
                        _choice = 0;
                    }
                }
                return _choice == 1;
            }
        }

        /// <summary>Tests: forget the cached choice (the stored one is read again).</summary>
        public static void Forget()
        {
            _choice = -1;
        }
    }
}
