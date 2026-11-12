using System;
using System.Collections.Generic;
using System.Threading;
using UnityEngine.Networking;

namespace Studio.Pulse
{
    /// <summary>
    /// The SDK's HTTP transport in Unity (all platforms, WebGL included). UnityWebRequest must
    /// start on the main thread: a request asked for from another thread starts on the next frame.
    /// </summary>
    public sealed class UnityWebRequestTransport : IPulseTransport
    {
        readonly int _mainThread;
        readonly List<Action> _deferred = new List<Action>();
        public int TimeoutSeconds = 20;

        public UnityWebRequestTransport()
        {
            _mainThread = Thread.CurrentThread.ManagedThreadId;
        }

        public void Post(string url, string writeKey, byte[] body, int length, bool gzip, Action<PulseHttpResult> done)
        {
            byte[] data = body;
            if (length != body.Length)
            {
                data = new byte[length];
                Buffer.BlockCopy(body, 0, data, 0, length);
            }
            if (Thread.CurrentThread.ManagedThreadId != _mainThread)
            {
                lock (_deferred) _deferred.Add(() => Start(url, writeKey, data, gzip, done));
                return;
            }
            Start(url, writeKey, data, gzip, done);
        }

        /// <summary>Called every frame by the runtime object.</summary>
        internal void RunDeferred()
        {
            if (_deferred.Count == 0) return;
            Action[] work;
            lock (_deferred)
            {
                work = _deferred.ToArray();
                _deferred.Clear();
            }
            foreach (Action a in work) a();
        }

        void Start(string url, string writeKey, byte[] data, bool gzip, Action<PulseHttpResult> done)
        {
            UnityWebRequest req;
            try
            {
                req = new UnityWebRequest(url, UnityWebRequest.kHttpVerbPOST)
                {
                    uploadHandler = new UploadHandlerRaw(data),
                    downloadHandler = new DownloadHandlerBuffer(),
                    timeout = TimeoutSeconds,
                };
                req.SetRequestHeader("Content-Type", "application/json");
                req.SetRequestHeader("X-Pulse-Key", writeKey);
                if (gzip) req.SetRequestHeader("Content-Encoding", "gzip");
            }
            catch (Exception ex)
            {
                done(new PulseHttpResult { Status = 0, RetryAfterSeconds = -1, Error = ex.Message });
                return;
            }
            UnityWebRequestAsyncOperation op = req.SendWebRequest();
            op.completed += _ =>
            {
                var r = new PulseHttpResult { RetryAfterSeconds = -1 };
                try
                {
                    r.Status = (int)req.responseCode;
                    if (req.result == UnityWebRequest.Result.ConnectionError || r.Status == 0)
                    {
                        r.Status = 0;
                        r.Error = req.error;
                    }
                    else
                    {
                        string ra = req.GetResponseHeader("Retry-After");
                        if (!string.IsNullOrEmpty(ra) && int.TryParse(ra, out int sec)) r.RetryAfterSeconds = sec;
                        r.Body = req.downloadHandler != null ? req.downloadHandler.text : null;
                    }
                }
                catch (Exception ex)
                {
                    r.Status = 0;
                    r.Error = ex.Message;
                }
                finally
                {
                    req.Dispose();
                }
                done(r);
            };
        }
    }
}
