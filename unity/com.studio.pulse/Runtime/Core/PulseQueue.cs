// Pure C# (no UnityEngine): the on-disk event queue.
//
// Append-only segment files (PostHog-style crash resilience): each file is one
// context line (the batch "ctx" object) followed by one event per line, written
// with a single write per event and flushed to the OS, so killing the process
// loses nothing already recorded; fsync happens when a segment closes and when
// the app goes to the background. A segment is deleted only after the collector
// answered 2xx for every event in it; a partly delivered segment remembers how
// many events went through in a small ".ack" file. The cap (events and bytes)
// drops the oldest segments first and counts what it dropped.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;

namespace Studio.Pulse
{
    internal sealed class PulseQueue
    {
        internal sealed class Segment
        {
            public long No;
            public string Path;
            public byte[] Ctx;     // context JSON (no newline)
            public int Count;      // event lines in the file
            public int Acked;      // leading event lines already delivered
            public long Bytes;     // file size
            public long MaxSeq;
            public bool Closed;
            public int InFlight;   // events of this segment in the request being sent

            public int Pending { get { return Count - Acked; } }
            public string AckPath { get { return System.IO.Path.ChangeExtension(Path, ".ack"); } }
        }

        internal struct Part
        {
            public Segment Seg;
            public int Count;
        }

        readonly string _dir;
        readonly List<Segment> _segs = new List<Segment>();
        readonly Action<string> _log;
        Segment _active;
        FileStream _stream;
        long _nextNo = 1;

        public int MaxEvents;
        public long MaxBytes;
        public int SegmentMaxEvents;
        public long SegmentMaxBytes;

        public int PendingEvents { get; private set; }
        public long TotalBytes { get; private set; }
        public long Dropped;      // events removed by the cap since this object was created

        public PulseQueue(string dir, Action<string> log)
        {
            _dir = dir;
            _log = log;
        }

        public int SegmentCount { get { return _segs.Count; } }

        /// <summary>Reads the queue directory; returns the highest seq found.</summary>
        public long Load()
        {
            Directory.CreateDirectory(_dir);
            long maxSeq = 0;
            var files = Directory.GetFiles(_dir, "*.seg");
            Array.Sort(files, StringComparer.Ordinal);
            foreach (string f in files)
            {
                try
                {
                    Segment s = ReadSegment(f);
                    if (s == null) continue;
                    if (s.MaxSeq > maxSeq) maxSeq = s.MaxSeq;
                    if (s.Pending <= 0)
                    {
                        DeleteFiles(s);
                        continue;
                    }
                    _segs.Add(s);
                    PendingEvents += s.Pending;
                    TotalBytes += s.Bytes;
                    if (s.No >= _nextNo) _nextNo = s.No + 1;
                }
                catch (Exception ex)
                {
                    Warn("queue: unreadable segment " + System.IO.Path.GetFileName(f) + ": " + ex.Message);
                }
            }
            foreach (string f in Directory.GetFiles(_dir, "*.ack"))
            {
                if (!File.Exists(System.IO.Path.ChangeExtension(f, ".seg"))) TryDelete(f);
            }
            return maxSeq;
        }

        Segment ReadSegment(string path)
        {
            byte[] data = File.ReadAllBytes(path);
            string name = System.IO.Path.GetFileNameWithoutExtension(path);
            if (!long.TryParse(name, NumberStyles.None, CultureInfo.InvariantCulture, out long no)) return null;
            int end = data.Length;
            while (end > 0 && data[end - 1] != (byte)'\n') end--; // a torn last line is dropped
            int nl = Array.IndexOf(data, (byte)'\n', 0, end);
            if (nl <= 0)
            {
                TryDelete(path);
                return null;
            }
            var seg = new Segment { No = no, Path = path, Ctx = new byte[nl], Closed = true };
            Buffer.BlockCopy(data, 0, seg.Ctx, 0, nl);
            var good = new List<(int, int)>();
            bool rewrite = end != data.Length;
            int pos = nl + 1;
            while (pos < end)
            {
                int e = Array.IndexOf(data, (byte)'\n', pos, end - pos);
                if (ValidEventLine(data, pos, e - pos))
                {
                    good.Add((pos, e - pos));
                    long seq = ParseSeq(data, pos);
                    if (seq > seg.MaxSeq) seg.MaxSeq = seq;
                }
                else rewrite = true;
                pos = e + 1;
            }
            seg.Count = good.Count;
            if (rewrite)
            {
                using (var fs = new FileStream(path, FileMode.Create, FileAccess.Write, FileShare.ReadWrite | FileShare.Delete))
                {
                    fs.Write(data, 0, nl + 1);
                    foreach (var (o, l) in good)
                    {
                        fs.Write(data, o, l);
                        fs.WriteByte((byte)'\n');
                    }
                    fs.Flush(true);
                }
                TryDelete(seg.AckPath); // line numbers changed; a re-send is deduped by the collector
                seg.Bytes = new FileInfo(path).Length;
            }
            else
            {
                seg.Bytes = data.Length;
                string ack = seg.AckPath;
                if (File.Exists(ack)
                    && int.TryParse(File.ReadAllText(ack).Trim(), NumberStyles.None, CultureInfo.InvariantCulture, out int acked))
                {
                    seg.Acked = Math.Min(acked, seg.Count);
                }
            }
            return seg;
        }

        static readonly byte[] IdPrefix = { (byte)'{', (byte)'"', (byte)'i', (byte)'d', (byte)'"', (byte)':', (byte)'"' };
        static readonly byte[] SeqKey = { (byte)'"', (byte)',', (byte)'"', (byte)'s', (byte)'e', (byte)'q', (byte)'"', (byte)':' };
        const int SeqAt = 7 + 36 + 8;

        static bool ValidEventLine(byte[] d, int o, int len)
        {
            if (len < SeqAt + 2 || d[o + len - 1] != (byte)'}') return false;
            for (int i = 0; i < IdPrefix.Length; i++) if (d[o + i] != IdPrefix[i]) return false;
            for (int i = 0; i < SeqKey.Length; i++) if (d[o + 43 + i] != SeqKey[i]) return false;
            return d[o + SeqAt] >= (byte)'0' && d[o + SeqAt] <= (byte)'9';
        }

        static long ParseSeq(byte[] d, int o)
        {
            long v = 0;
            for (int i = o + SeqAt; i < d.Length && d[i] >= (byte)'0' && d[i] <= (byte)'9'; i++) v = v * 10 + (d[i] - '0');
            return v;
        }

        /// <summary>Appends one event line (the writer holds the JSON and its trailing newline).</summary>
        public void Append(byte[] ctx, PulseJsonWriter line, long seq)
        {
            if (_active == null || !ReferenceEquals(_active.Ctx, ctx) || _active.Count >= SegmentMaxEvents || _active.Bytes >= SegmentMaxBytes)
            {
                CloseActive(true);
                Open(ctx);
            }
            _stream.Write(line.Buffer, 0, line.Length);
            _stream.Flush();
            _active.Count++;
            _active.Bytes += line.Length;
            if (seq > _active.MaxSeq) _active.MaxSeq = seq;
            PendingEvents++;
            TotalBytes += line.Length;
            EnforceCap();
        }

        void Open(byte[] ctx)
        {
            Directory.CreateDirectory(_dir);
            long no = _nextNo++;
            string path = System.IO.Path.Combine(_dir, no.ToString("D12", CultureInfo.InvariantCulture) + ".seg");
            _stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.ReadWrite | FileShare.Delete, 4096);
            _stream.Write(ctx, 0, ctx.Length);
            _stream.WriteByte((byte)'\n');
            _stream.Flush();
            _active = new Segment { No = no, Path = path, Ctx = ctx, Bytes = ctx.Length + 1 };
            _segs.Add(_active);
            TotalBytes += _active.Bytes;
        }

        /// <summary>fsync the open segment (app going to the background).</summary>
        public void Sync()
        {
            if (_stream == null) return;
            try { _stream.Flush(true); }
            catch (Exception ex) { Warn("queue: fsync failed: " + ex.Message); }
        }

        public void CloseActive(bool fsync)
        {
            if (_stream != null)
            {
                try
                {
                    _stream.Flush(fsync);
                    _stream.Dispose();
                }
                catch (Exception ex)
                {
                    Warn("queue: close failed: " + ex.Message);
                }
                _stream = null;
            }
            if (_active != null)
            {
                _active.Closed = true;
                if (_active.Pending == 0 && _active.InFlight == 0) Remove(_active);
                _active = null;
            }
        }

        public bool HasActivePending { get { return _active != null && _active.Pending > 0; } }

        /// <summary>
        /// Writes a v2 batch body into <paramref name="body"/> from the oldest pending events
        /// (closed segments with the same context, at most maxEvents / maxBytes) and
        /// marks them in flight. Returns the parts, or null when nothing is pending.
        /// </summary>
        public List<Part> BuildBatch(PulseJsonWriter body, long sentAt, int maxEvents, int maxBytes)
        {
            if (HasActivePending) CloseActive(true);
            List<Part> parts = null;
            byte[] ctx = null;
            int events = 0;
            foreach (Segment s in _segs)
            {
                if (events >= maxEvents) break;
                if (!s.Closed || s.Pending <= 0 || s.InFlight > 0) continue;
                if (ctx != null && !SameBytes(ctx, s.Ctx)) break;
                byte[] data;
                try { data = File.ReadAllBytes(s.Path); }
                catch (Exception ex)
                {
                    Warn("queue: cannot read " + System.IO.Path.GetFileName(s.Path) + ": " + ex.Message);
                    continue;
                }
                int pos = Array.IndexOf(data, (byte)'\n') + 1;
                for (int skip = 0; skip < s.Acked && pos > 0; skip++) pos = Array.IndexOf(data, (byte)'\n', pos) + 1;
                if (pos <= 0) continue;
                int taken = 0;
                while (events < maxEvents && pos < data.Length)
                {
                    int e = Array.IndexOf(data, (byte)'\n', pos);
                    if (e < 0) break;
                    int len = e - pos;
                    if (ctx == null)
                    {
                        ctx = s.Ctx;
                        body.Clear();
                        body.Ascii("{\"v\":2,\"schema\":\"");
                        body.Ascii(PulseContract.Version);
                        body.Ascii("\",\"sent_at\":");
                        body.Long(sentAt);
                        body.Ascii(",\"ctx\":");
                        body.Bytes(ctx, 0, ctx.Length);
                        body.Ascii(",\"events\":[");
                    }
                    if (events > 0 && body.Length + len + 3 > maxBytes) { events = maxEvents; break; }
                    if (events > 0) body.Byte((byte)',');
                    body.Bytes(data, pos, len);
                    events++;
                    taken++;
                    pos = e + 1;
                }
                if (taken > 0)
                {
                    if (parts == null) parts = new List<Part>();
                    parts.Add(new Part { Seg = s, Count = taken });
                    s.InFlight = taken;
                }
                if (taken < s.Pending) break; // keep events in order
            }
            if (parts == null) return null;
            body.Ascii("]}");
            return parts;
        }

        static bool SameBytes(byte[] a, byte[] b)
        {
            if (ReferenceEquals(a, b)) return true;
            if (a.Length != b.Length) return false;
            for (int i = 0; i < a.Length; i++) if (a[i] != b[i]) return false;
            return true;
        }

        /// <summary>The request ended: delivered (or refused for good) events leave the queue.</summary>
        public void Complete(List<Part> parts, bool delivered)
        {
            foreach (Part p in parts)
            {
                Segment s = p.Seg;
                s.InFlight = 0;
                if (!delivered || !_segs.Contains(s)) continue;
                s.Acked += p.Count;
                PendingEvents -= p.Count;
                if (s.Pending <= 0 && s.Closed) Remove(s);
                else WriteAck(s);
            }
        }

        public long MaxSeqOf(List<Part> parts)
        {
            long m = 0;
            foreach (Part p in parts) if (p.Seg.MaxSeq > m) m = p.Seg.MaxSeq;
            return m;
        }

        void WriteAck(Segment s)
        {
            try
            {
                string tmp = s.AckPath + ".tmp";
                File.WriteAllText(tmp, s.Acked.ToString(CultureInfo.InvariantCulture));
                if (File.Exists(s.AckPath)) File.Delete(s.AckPath);
                File.Move(tmp, s.AckPath);
            }
            catch (Exception ex)
            {
                Warn("queue: ack write failed (a re-send is deduped): " + ex.Message);
            }
        }

        void Remove(Segment s)
        {
            _segs.Remove(s);
            TotalBytes -= s.Bytes;
            if (s.Pending > 0) PendingEvents -= s.Pending;
            DeleteFiles(s);
        }

        void DeleteFiles(Segment s)
        {
            TryDelete(s.Path);
            TryDelete(s.AckPath);
        }

        void EnforceCap()
        {
            while ((PendingEvents > MaxEvents || TotalBytes > MaxBytes) && _segs.Count > 0)
            {
                Segment victim = null;
                foreach (Segment s in _segs)
                {
                    if (s.InFlight == 0) { victim = s; break; }
                }
                if (victim == null) return;
                if (victim == _active)
                {
                    if (_segs.Count == 1 && victim.Pending <= 1) return; // keep the event just written
                    CloseActive(false);
                    if (!_segs.Contains(victim)) continue;
                }
                Dropped += victim.Pending;
                Remove(victim);
            }
        }

        /// <summary>Deletes every queued event (opt-out, consent refused, deletion request).</summary>
        public void Wipe()
        {
            CloseActive(false);
            foreach (Segment s in _segs.ToArray()) Remove(s);
            _segs.Clear();
            PendingEvents = 0;
            TotalBytes = 0;
            try
            {
                if (Directory.Exists(_dir))
                {
                    foreach (string f in Directory.GetFiles(_dir)) TryDelete(f);
                }
            }
            catch (Exception ex)
            {
                Warn("queue: wipe failed: " + ex.Message);
            }
        }

        void TryDelete(string path)
        {
            try { if (File.Exists(path)) File.Delete(path); }
            catch (Exception ex) { Warn("queue: delete failed: " + ex.Message); }
        }

        void Warn(string msg)
        {
            if (_log != null) _log(msg);
        }
    }
}
