// Pure C# (no UnityEngine): level_attempt, one event per finished attempt with the help the
// player used, and, on a sample of installs, the tap trace (PulseTrace). First shipped by Acorn
// Mart 1.0.1 (19 Nov 2026); the event name, the parameter names, their order and types are the
// ones it sends. docs/SDK_UNITY.md "Level attempts".
using System;
using System.Collections.Generic;

namespace Studio.Pulse
{
    /// <summary>
    /// One finished attempt of a level. Difficulty is usually measured without boosters and
    /// continues; comparing it with real players needs how much help each attempt used. An attempt
    /// ends when the player wins or loses; if they continue and end again, that is another event
    /// with a higher <see cref="Continues"/>. An attempt without help: Continues 0 and Boosters 0.
    /// The game fills it; <c>Pulse.LevelAttempt(attempt)</c> (Unity) or
    /// <see cref="PulseCore.LevelAttempt"/> sends it.
    /// </summary>
    public sealed class PulseAttempt
    {
        public const string EventName = "level_attempt";

        /// <summary>Share of installs that also send the tap trace (Acorn Mart 1.0.1: a quarter).</summary>
        public const float DefaultTraceRate = 0.25f;

        public long Level;
        /// <summary>The board / layout number that rebuilds the level exactly (null: not sent).</summary>
        public long? Table;
        /// <summary>Difficulty band of the level, e.g. normal, hard, boss (null: not sent).</summary>
        public string Band;
        /// <summary>Which attempt of this level on this install (the game's counter).</summary>
        public int Try;
        public bool Won;
        /// <summary>A short static code (out_of_time, no_moves ...); null: "won" or "unknown".</summary>
        public string Reason;
        /// <summary>Continues taken in this attempt so far.</summary>
        public int Continues;
        /// <summary>Boosters used in this attempt (not counting continues).</summary>
        public int Boosters;
        /// <summary>Moves made; null: the taps that are not <see cref="PulseTrace.NotAMove"/>.</summary>
        public int? Moves;
        /// <summary>Seconds the attempt took (sent rounded to 0.1).</summary>
        public double Seconds;
        /// <summary>Share of the level done, 0-1 (sent rounded to 0.001).</summary>
        public double Progress;
        /// <summary>Move ids in order; a booster or continue tap is <see cref="PulseTrace.NotAMove"/>.</summary>
        public int[] Taps;
        /// <summary>Each tap's time in seconds since the level started.</summary>
        public float[] TapTimes;

        /// <summary>The moves sent: <see cref="Moves"/>, or the taps that are moves.</summary>
        public int MoveCount
        {
            get
            {
                if (Moves.HasValue) return Moves.Value;
                int n = 0;
                if (Taps != null) for (int i = 0; i < Taps.Length; i++) if (Taps[i] != PulseTrace.NotAMove) n++;
                return n;
            }
        }

        /// <summary>
        /// The event's parameters in the order they are sent: the summary (level, table, band, try,
        /// won, reason, continues, boosters, moves, seconds, progress), then with a trace its status
        /// and the non-empty tap and time parts. At most 24 (the limit is 25).
        /// </summary>
        public List<KeyValuePair<string, object>> Fields(bool withTrace)
        {
            var l = new List<KeyValuePair<string, object>>(24);
            l.Add(Field("level", Level));
            if (Table.HasValue) l.Add(Field("table", Table.Value));
            if (Band != null) l.Add(Field("band", Band));
            l.Add(Field("try", Try));
            l.Add(Field("won", Won));
            l.Add(Field("reason", Reason ?? (Won ? "won" : "unknown")));
            l.Add(Field("continues", Continues));
            l.Add(Field("boosters", Boosters));
            l.Add(Field("moves", MoveCount));
            l.Add(Field("seconds", Math.Round(Seconds, 1)));
            l.Add(Field("progress", Math.Round(Progress, 3)));
            if (!withTrace) return l;
            string status = PulseTrace.Encode(Taps, TapTimes, out string[] t, out string[] d);
            l.Add(Field(PulseTrace.StatusKey, status));
            if (status == PulseTrace.Broken) return l;
            for (int p = 0; p < PulseTrace.Parts; p++) if (!string.IsNullOrEmpty(t[p])) l.Add(Field(PulseTrace.TapsKey(p), t[p]));
            for (int p = 0; p < PulseTrace.Parts; p++) if (!string.IsNullOrEmpty(d[p])) l.Add(Field(PulseTrace.TimesKey(p), d[p]));
            return l;
        }

        /// <summary>Clears <paramref name="p"/> and adds <see cref="Fields"/> to it, typed as they are sent.</summary>
        public PulseParams Fill(PulseParams p, bool withTrace)
        {
            p.Clear();
            foreach (var f in Fields(withTrace))
            {
                object v = f.Value;
                if (v is long l) p.Add(f.Key, l);
                else if (v is int i) p.Add(f.Key, i);
                else if (v is bool b) p.Add(f.Key, b);
                else if (v is double x) p.Add(f.Key, x);
                else p.Add(f.Key, v as string);
            }
            return p;
        }

        static KeyValuePair<string, object> Field(string key, object value)
        {
            return new KeyValuePair<string, object>(key, value);
        }
    }

    /// <summary>
    /// A yes / no chosen once per install with a given probability and then kept (so a player's
    /// skill can be read across their attempts). Storage is the caller's (PlayerPrefs in Unity).
    /// </summary>
    public static class PulseInstallSample
    {
        /// <summary>
        /// <paramref name="stored"/>: the kept choice (1 in, 0 or any other non-negative value out,
        /// negative: not chosen yet). Not chosen: in when <paramref name="random"/>() &lt; rate;
        /// <paramref name="store"/> is then the value to keep (otherwise <paramref name="stored"/>).
        /// </summary>
        public static bool Resolve(int stored, float rate, Func<double> random, out int store)
        {
            store = stored;
            if (stored < 0) store = random() < rate ? 1 : 0;
            return store == 1;
        }
    }
}
