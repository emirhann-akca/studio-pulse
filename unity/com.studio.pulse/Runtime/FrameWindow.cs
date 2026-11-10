// Fixed-size ring buffer of frame times with percentile snapshots.
// Pure C#: no UnityEngine use. Add() never allocates; Snapshot() sorts a
// preallocated scratch array once per window.
using System;

namespace Studio.Pulse
{
    public sealed class FrameWindow
    {
        readonly float[] _ring;
        readonly float[] _scratch;
        int _head;
        int _stored;

        /// <summary>Frames seen in this window (can exceed the ring capacity).</summary>
        public int Frames { get; private set; }
        /// <summary>Frames slower than PulseSchema.SlowFrameMs in this window.</summary>
        public int SlowFrames { get; private set; }
        public float MaxMs { get; private set; }
        public int Capacity { get { return _ring.Length; } }

        public FrameWindow(int capacity)
        {
            if (capacity < 16) capacity = 16;
            _ring = new float[capacity];
            _scratch = new float[capacity];
        }

        public void Add(float frameMs)
        {
            _ring[_head] = frameMs;
            _head = (_head + 1) % _ring.Length;
            if (_stored < _ring.Length) _stored++;
            Frames++;
            if (frameMs > PulseSchema.SlowFrameMs) SlowFrames++;
            if (frameMs > MaxMs) MaxMs = frameMs;
        }

        /// <summary>p50/p95 of the most recent frames (at most Capacity of them).</summary>
        public void Snapshot(out float p50, out float p95)
        {
            if (_stored == 0) { p50 = 0f; p95 = 0f; return; }
            Array.Copy(_ring, _scratch, _stored);
            Array.Sort(_scratch, 0, _stored);
            p50 = Percentile(_scratch, _stored, 0.50);
            p95 = Percentile(_scratch, _stored, 0.95);
        }

        public void Reset()
        {
            _head = 0;
            _stored = 0;
            Frames = 0;
            SlowFrames = 0;
            MaxMs = 0f;
        }

        /// <summary>Linear interpolation between closest ranks (same as numpy / DuckDB quantile_cont).</summary>
        public static float Percentile(float[] sorted, int count, double q)
        {
            if (count <= 0) return 0f;
            if (count == 1) return sorted[0];
            double pos = (count - 1) * q;
            int lo = (int)Math.Floor(pos);
            int hi = lo + 1 < count ? lo + 1 : lo;
            double frac = pos - lo;
            return (float)(sorted[lo] + (sorted[hi] - sorted[lo]) * frac);
        }
    }
}
