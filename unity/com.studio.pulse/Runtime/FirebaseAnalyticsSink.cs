#if PULSE_FIREBASE
using System.Collections.Generic;
using Firebase.Analytics;

namespace Studio.Pulse
{
    /// <summary>
    /// Logs events through the Firebase Analytics SDK, so they reach the GA4
    /// BigQuery export as pulse_perf / pulse_load with the parameters listed in
    /// docs/EVENTS.md. Enabled when the PULSE_FIREBASE define is set (set
    /// automatically when the com.google.firebase.analytics UPM package exists).
    /// Firebase adds app version, platform and session id itself.
    /// </summary>
    public sealed class FirebaseAnalyticsSink : IPulseSink
    {
        public void Send(IReadOnlyList<PulseEvent> batch, PulseContext context)
        {
            for (int i = 0; i < batch.Count; i++)
            {
                PulseEvent e = batch[i];
                var parameters = new Parameter[e.Params.Count];
                for (int j = 0; j < e.Params.Count; j++)
                {
                    PulseParam p = e.Params[j];
                    switch (p.Kind)
                    {
                        case PulseParamKind.Int:
                            parameters[j] = new Parameter(p.Key, (long)p.Number);
                            break;
                        case PulseParamKind.Double:
                            parameters[j] = new Parameter(p.Key, p.Number);
                            break;
                        default:
                            parameters[j] = new Parameter(p.Key, p.Text);
                            break;
                    }
                }
                FirebaseAnalytics.LogEvent(e.Name, parameters);
            }
        }

        public void Flush()
        {
        }
    }
}
#endif
