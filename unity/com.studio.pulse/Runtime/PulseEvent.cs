// Pure C#: no UnityEngine use, so it can be unit-tested outside the Editor.
using System.Collections.Generic;

namespace Studio.Pulse
{
    public enum PulseParamKind
    {
        Int,
        Double,
        Text,
    }

    public struct PulseParam
    {
        public string Key;
        public PulseParamKind Kind;
        public double Number;
        public string Text;
    }

    /// <summary>
    /// One telemetry event. Events are created at most once per perf window or
    /// load, never per frame.
    /// </summary>
    public sealed class PulseEvent
    {
        public readonly string Name;
        public readonly long UnixMs;
        public readonly List<PulseParam> Params = new List<PulseParam>(16);

        public PulseEvent(string name, long unixMs)
        {
            Name = name;
            UnixMs = unixMs;
            AddInt(PulseSchema.ParamSchemaVersion, PulseSchema.SchemaVersion);
        }

        public PulseEvent AddInt(string key, long value)
        {
            Params.Add(new PulseParam { Key = key, Kind = PulseParamKind.Int, Number = value });
            return this;
        }

        public PulseEvent AddDouble(string key, double value)
        {
            Params.Add(new PulseParam { Key = key, Kind = PulseParamKind.Double, Number = value });
            return this;
        }

        public PulseEvent AddText(string key, string value)
        {
            Params.Add(new PulseParam { Key = key, Kind = PulseParamKind.Text, Text = value ?? string.Empty });
            return this;
        }
    }

    /// <summary>Values shared by every event of a session (the JSON-lines envelope).</summary>
    public sealed class PulseContext
    {
        public string AppId = "";
        public string AppVersion = "";
        public string Platform = "UNKNOWN";
        public string Session = "";
    }
}
