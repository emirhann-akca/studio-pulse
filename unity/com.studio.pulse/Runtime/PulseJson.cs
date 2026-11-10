// JSON-lines encoding of PulseEvent (format documented in docs/EVENTS.md).
// Pure C#: no UnityEngine use. Numbers use the invariant culture.
using System;
using System.Globalization;
using System.Text;

namespace Studio.Pulse
{
    public static class PulseJson
    {
        static readonly DateTime Epoch = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);

        public static void AppendLine(StringBuilder sb, PulseEvent e, PulseContext ctx)
        {
            sb.Append("{\"v\":").Append(PulseSchema.SchemaVersion.ToString(CultureInfo.InvariantCulture));
            sb.Append(",\"event\":"); AppendString(sb, e.Name);
            sb.Append(",\"ts\":\"").Append(Epoch.AddMilliseconds(e.UnixMs).ToString("yyyy-MM-dd'T'HH:mm:ss.fff'Z'", CultureInfo.InvariantCulture)).Append('"');
            sb.Append(",\"app\":"); AppendString(sb, ctx.AppId);
            sb.Append(",\"app_version\":"); AppendString(sb, ctx.AppVersion);
            sb.Append(",\"platform\":"); AppendString(sb, ctx.Platform);
            sb.Append(",\"session\":"); AppendString(sb, ctx.Session);
            sb.Append(",\"params\":{");
            for (int i = 0; i < e.Params.Count; i++)
            {
                PulseParam p = e.Params[i];
                if (i > 0) sb.Append(',');
                AppendString(sb, p.Key);
                sb.Append(':');
                switch (p.Kind)
                {
                    case PulseParamKind.Int:
                        sb.Append(((long)p.Number).ToString(CultureInfo.InvariantCulture));
                        break;
                    case PulseParamKind.Double:
                        AppendNumber(sb, p.Number);
                        break;
                    default:
                        AppendString(sb, p.Text);
                        break;
                }
            }
            sb.Append("}}\n");
        }

        static void AppendNumber(StringBuilder sb, double v)
        {
            if (double.IsNaN(v) || double.IsInfinity(v)) { sb.Append("null"); return; }
            sb.Append(Math.Round(v, 3).ToString("0.###", CultureInfo.InvariantCulture));
        }

        public static void AppendString(StringBuilder sb, string s)
        {
            sb.Append('"');
            if (s != null)
            {
                foreach (char c in s)
                {
                    switch (c)
                    {
                        case '"': sb.Append("\\\""); break;
                        case '\\': sb.Append("\\\\"); break;
                        case '\n': sb.Append("\\n"); break;
                        case '\r': sb.Append("\\r"); break;
                        case '\t': sb.Append("\\t"); break;
                        default:
                            if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                            else sb.Append(c);
                            break;
                    }
                }
            }
            sb.Append('"');
        }

        public static long ToUnixMs(DateTime utc)
        {
            return (long)(utc - Epoch).TotalMilliseconds;
        }
    }
}
