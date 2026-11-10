using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using UnityEngine;
using UnityEngine.Networking;

namespace Studio.Pulse
{
    /// <summary>
    /// Writes events as JSON lines (docs/EVENTS.md) to a local file and/or POSTs
    /// them to an HTTP endpoint (Content-Type: application/x-ndjson). Failed HTTP
    /// batches are kept (up to maxPendingBytes) and retried on the next flush.
    /// The Python connector `unity_jsonl` reads these files directly.
    /// </summary>
    public sealed class JsonLinesSink : IPulseSink
    {
        readonly string _filePath;
        readonly string _endpoint;
        readonly string _authHeader;
        readonly int _maxPendingBytes;
        readonly StringBuilder _sb = new StringBuilder(4096);
        readonly StringBuilder _pending = new StringBuilder();
        UnityWebRequest _inFlight;

        /// <param name="filePath">Append-only .jsonl file, e.g. Path.Combine(Application.persistentDataPath, "pulse.jsonl"). Null to disable.</param>
        /// <param name="httpEndpoint">Collector URL. Null to disable.</param>
        /// <param name="authHeader">Optional Authorization header value for the collector.</param>
        public JsonLinesSink(string filePath = null, string httpEndpoint = null, string authHeader = null, int maxPendingBytes = 256 * 1024)
        {
            _filePath = filePath;
            _endpoint = httpEndpoint;
            _authHeader = authHeader;
            _maxPendingBytes = maxPendingBytes;
        }

        public static string DefaultFilePath()
        {
            return Path.Combine(Application.persistentDataPath, "pulse", "events.jsonl");
        }

        public void Send(IReadOnlyList<PulseEvent> batch, PulseContext context)
        {
            if (batch.Count == 0) return;
            _sb.Length = 0;
            for (int i = 0; i < batch.Count; i++) PulseJson.AppendLine(_sb, batch[i], context);
            string lines = _sb.ToString();

            if (!string.IsNullOrEmpty(_filePath))
            {
                try
                {
                    string dir = Path.GetDirectoryName(_filePath);
                    if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
                    File.AppendAllText(_filePath, lines, Encoding.UTF8);
                }
                catch (Exception ex)
                {
                    Debug.LogWarning("[Studio.Pulse] could not write " + _filePath + ": " + ex.Message);
                }
            }

            if (!string.IsNullOrEmpty(_endpoint))
            {
                if (_pending.Length + lines.Length <= _maxPendingBytes) _pending.Append(lines);
                TryPost();
            }
        }

        void TryPost()
        {
            if (_inFlight != null || _pending.Length == 0) return;
            byte[] body = Encoding.UTF8.GetBytes(_pending.ToString());
            int sentChars = _pending.Length;
            var req = new UnityWebRequest(_endpoint, UnityWebRequest.kHttpVerbPOST)
            {
                uploadHandler = new UploadHandlerRaw(body),
                downloadHandler = new DownloadHandlerBuffer(),
                timeout = 20,
            };
            req.SetRequestHeader("Content-Type", "application/x-ndjson");
            if (!string.IsNullOrEmpty(_authHeader)) req.SetRequestHeader("Authorization", _authHeader);
            _inFlight = req;
            UnityWebRequestAsyncOperation op = req.SendWebRequest();
            op.completed += _ =>
            {
                if (req.result == UnityWebRequest.Result.Success)
                {
                    _pending.Remove(0, Math.Min(sentChars, _pending.Length));
                }
                else
                {
                    Debug.LogWarning("[Studio.Pulse] upload failed (" + req.responseCode + "): " + req.error);
                }
                req.Dispose();
                _inFlight = null;
            };
        }

        public void Flush()
        {
            TryPost();
        }
    }
}
