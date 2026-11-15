using System;
using System.Collections.Generic;
using System.Threading;
using UnityEngine;

namespace Studio.Pulse.Ads
{
    /// <summary>Runs callbacks on Unity's main thread and after real-time delays (ad load retries).</summary>
    [AddComponentMenu("")]
    public sealed class StudioMainThread : MonoBehaviour
    {
        struct Timed
        {
            public float At;
            public Action Run;
        }

        static StudioMainThread _instance;
        static int _mainThreadId = -1;
        static readonly object Gate = new object();
        static readonly List<Action> Queue = new List<Action>();
        static readonly List<Timed> Timers = new List<Timed>();
        readonly List<Action> _run = new List<Action>();

        /// <summary>Creates the hidden runner; call from the main thread (StudioApp.Start does).</summary>
        public static void Ensure()
        {
            if (_instance != null) return;
            _mainThreadId = Thread.CurrentThread.ManagedThreadId;
            var go = new GameObject("StudioMainThread") { hideFlags = HideFlags.HideAndDontSave };
            DontDestroyOnLoad(go);
            _instance = go.AddComponent<StudioMainThread>();
        }

        public static bool IsMainThread
        {
            get { return _mainThreadId == Thread.CurrentThread.ManagedThreadId; }
        }

        /// <summary>Runs now when already on the main thread, otherwise on the next frame.</summary>
        public static void Run(Action a)
        {
            if (a == null) return;
            if (IsMainThread) Safe(a);
            else Post(a);
        }

        /// <summary>Runs on the main thread on the next frame (any thread).</summary>
        public static void Post(Action a)
        {
            if (a == null) return;
            lock (Gate) Queue.Add(a);
        }

        /// <summary>Runs on the main thread after <paramref name="seconds"/> of real time (any thread).</summary>
        public static void Delay(double seconds, Action a)
        {
            if (a == null) return;
            Post(() =>
            {
                lock (Gate) Timers.Add(new Timed { At = Time.realtimeSinceStartup + (float)seconds, Run = a });
            });
        }

        void Update()
        {
            float now = Time.realtimeSinceStartup;
            lock (Gate)
            {
                _run.AddRange(Queue);
                Queue.Clear();
                for (int i = Timers.Count - 1; i >= 0; i--)
                {
                    if (Timers[i].At <= now)
                    {
                        _run.Add(Timers[i].Run);
                        Timers.RemoveAt(i);
                    }
                }
            }
            for (int i = 0; i < _run.Count; i++) Safe(_run[i]);
            _run.Clear();
        }

        static void Safe(Action a)
        {
            try { a(); }
            catch (Exception ex) { Debug.LogException(ex); }
        }
    }
}
