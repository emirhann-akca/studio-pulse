// Pure C# (no UnityEngine).
using System;

namespace Studio.Pulse
{
    /// <summary>SDK behaviour. The defaults come from event contract v2 (PulseContract.Limits).</summary>
    [Serializable]
    public sealed class PulseOptions
    {
        /// <summary>
        /// Nothing is written to disk or sent until SetConsent(true). Events recorded before that
        /// wait in memory (at most PendingConsentEvents) and are dropped if consent is refused.
        /// </summary>
        public bool ConsentRequired = false;

        /// <summary>Child-directed: no uid, no free-text parameters, ctx.child = true.</summary>
        public bool ChildDirected = false;

        /// <summary>Capture managed exceptions as pulse_error (Unity log hook, Flutter error hooks).</summary>
        public bool CaptureErrors = true;

        public int FlushEvents = PulseContract.Limits.FlushEvents;
        public int FlushIntervalMs = PulseContract.Limits.FlushIntervalMs;
        public int SessionTimeoutMs = PulseContract.Limits.SessionTimeoutMs;
        public int SessionMaxMs = PulseContract.Limits.SessionMaxMs;
        public int HeartbeatMs = PulseContract.Limits.HeartbeatMs;
        public int QueueMaxEvents = PulseContract.Limits.QueueMaxEvents;
        public int QueueMaxBytes = PulseContract.Limits.QueueMaxBytes;
        public int ErrorsPerLaunch = PulseContract.Limits.ErrorsPerLaunch;
        public int RetryMinMs = PulseContract.Limits.RetryMinMs;
        public int RetryMaxMs = PulseContract.Limits.RetryMaxMs;
        /// <summary>Random extra delay added to every retry, from Min to Max ms (Mixpanel: 1-5 s).</summary>
        public int RetryJitterMinMs = 1000;
        public int RetryJitterMaxMs = 5000;

        /// <summary>Events per request (the collector takes up to 500); a 413 halves it for this launch.</summary>
        public int MaxBatchEvents = 100;
        /// <summary>Uncompressed request size cap (the collector refuses more than 1 MiB).</summary>
        public int MaxBatchBytes = 900 * 1024;
        /// <summary>Bodies of at least this many bytes are gzipped.</summary>
        public int GzipMinBytes = 1024;
        /// <summary>A queue file closes after this many events (the unit the cap drops).</summary>
        public int SegmentMaxEvents = 100;
        public int SegmentMaxBytes = 256 * 1024;
        public int PendingConsentEvents = 500;
    }

    /// <summary>
    /// Parameters of a custom event. Reuse one instance (Clear() between events) and
    /// Track(name, params) allocates nothing. At most 25 parameters (GA4 rule).
    /// </summary>
    public sealed class PulseParams
    {
        public const int Capacity = PulseContract.Limits.MaxParams;

        internal enum Kind : byte { Long, Double, String, Bool }

        internal readonly string[] Keys = new string[Capacity];
        internal readonly Kind[] Kinds = new Kind[Capacity];
        internal readonly long[] Longs = new long[Capacity];
        internal readonly double[] Doubles = new double[Capacity];
        internal readonly string[] Strings = new string[Capacity];
        internal int Count;
        internal bool Overflow;

        public PulseParams Clear()
        {
            for (int i = 0; i < Count; i++) Strings[i] = null;
            Count = 0;
            Overflow = false;
            return this;
        }

        int Slot(string key)
        {
            for (int i = 0; i < Count; i++)
            {
                if (string.Equals(Keys[i], key, StringComparison.Ordinal)) return i;
            }
            if (Count >= Capacity) { Overflow = true; return -1; }
            Keys[Count] = key;
            return Count++;
        }

        public PulseParams Add(string key, long value)
        {
            int i = Slot(key);
            if (i >= 0) { Kinds[i] = Kind.Long; Longs[i] = value; Strings[i] = null; }
            return this;
        }

        public PulseParams Add(string key, int value)
        {
            return Add(key, (long)value);
        }

        public PulseParams Add(string key, double value)
        {
            int i = Slot(key);
            if (i >= 0) { Kinds[i] = Kind.Double; Doubles[i] = value; Strings[i] = null; }
            return this;
        }

        public PulseParams Add(string key, float value)
        {
            return Add(key, (double)value);
        }

        public PulseParams Add(string key, bool value)
        {
            int i = Slot(key);
            if (i >= 0) { Kinds[i] = Kind.Bool; Longs[i] = value ? 1 : 0; Strings[i] = null; }
            return this;
        }

        public PulseParams Add(string key, string value)
        {
            if (value == null) return this;
            int i = Slot(key);
            if (i >= 0) { Kinds[i] = Kind.String; Strings[i] = value; }
            return this;
        }
    }

    /// <summary>Counters for diagnostics and tests (local only).</summary>
    public struct PulseStats
    {
        /// <summary>Events waiting on disk (or in memory before consent).</summary>
        public int Queued;
        public long Recorded;
        public long Sent;
        /// <summary>Events deleted by the queue cap (oldest first).</summary>
        public long Dropped;
        /// <summary>Events refused: invalid locally, or in a batch the collector answered 400.</summary>
        public long Rejected;
        public long Requests;
        public int LastStatus;
        public string LastError;
        /// <summary>True after a 401/403: nothing is sent until the next launch.</summary>
        public bool Disabled;
        public bool OptedOut;
        public string ConsentState;
    }
}
