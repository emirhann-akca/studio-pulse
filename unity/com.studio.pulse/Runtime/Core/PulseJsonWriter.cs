// Pure C# (no UnityEngine): compact JSON written straight into a reusable byte
// buffer, so recording an event allocates nothing once the buffer has grown.
//
// The byte format is shared with the Dart SDK (flutter/studio_pulse) and pinned
// by contract/golden/sdk_conformance.ndjson:
//   - strings: UTF-8; escapes \" \\ \b \f \n \r \t, other controls as \u00xx;
//     a cut never splits a surrogate pair, a lone surrogate becomes U+FFFD
//   - integers: plain digits
//   - doubles: the shortest digits that read back to the same double, laid out
//     like ECMAScript Number#toString (0.1, 50, 1e-7, 1e+21); -0 is 0
using System;

namespace Studio.Pulse
{
    public sealed class PulseJsonWriter
    {
        public byte[] Buffer;
        public int Length;

        readonly char[] _num = new char[40];
        readonly ExactCheck _exact = new ExactCheck();

        public PulseJsonWriter(int capacity = 1024)
        {
            Buffer = new byte[capacity];
        }

        public void Clear()
        {
            Length = 0;
        }

        void Ensure(int extra)
        {
            int need = Length + extra;
            if (need <= Buffer.Length) return;
            int size = Buffer.Length * 2;
            while (size < need) size *= 2;
            Array.Resize(ref Buffer, size);
        }

        public void Byte(byte b)
        {
            Ensure(1);
            Buffer[Length++] = b;
        }

        public void Bytes(byte[] src, int offset, int count)
        {
            Ensure(count);
            System.Buffer.BlockCopy(src, offset, Buffer, Length, count);
            Length += count;
        }

        /// <summary>ASCII text written as is (keys, names, punctuation).</summary>
        public void Ascii(string s)
        {
            Ensure(s.Length);
            for (int i = 0; i < s.Length; i++) Buffer[Length++] = (byte)s[i];
        }

        /// <summary>,"key": (the comma only when <paramref name="comma"/>).</summary>
        public void Key(string key, bool comma)
        {
            Ensure(key.Length + 4);
            if (comma) Buffer[Length++] = (byte)',';
            Buffer[Length++] = (byte)'"';
            for (int i = 0; i < key.Length; i++) Buffer[Length++] = (byte)key[i];
            Buffer[Length++] = (byte)'"';
            Buffer[Length++] = (byte)':';
        }

        public void Null()
        {
            Ascii("null");
        }

        public void Bool(bool v)
        {
            Ascii(v ? "true" : "false");
        }

        /// <summary>The number of UTF-16 units of <paramref name="s"/> kept under <paramref name="maxLen"/> (never half a surrogate pair).</summary>
        public static int CutLength(string s, int maxLen)
        {
            if (s.Length <= maxLen) return s.Length;
            int n = maxLen;
            if (n > 0 && char.IsHighSurrogate(s[n - 1])) n--;
            return n;
        }

        public void String(string s, int maxLen = int.MaxValue)
        {
            int n = CutLength(s, maxLen);
            Ensure(n * 6 + 2);
            byte[] b = Buffer;
            int p = Length;
            b[p++] = (byte)'"';
            for (int i = 0; i < n; i++)
            {
                char c = s[i];
                if (c < 0x80)
                {
                    switch (c)
                    {
                        case '"': b[p++] = (byte)'\\'; b[p++] = (byte)'"'; break;
                        case '\\': b[p++] = (byte)'\\'; b[p++] = (byte)'\\'; break;
                        case '\b': b[p++] = (byte)'\\'; b[p++] = (byte)'b'; break;
                        case '\f': b[p++] = (byte)'\\'; b[p++] = (byte)'f'; break;
                        case '\n': b[p++] = (byte)'\\'; b[p++] = (byte)'n'; break;
                        case '\r': b[p++] = (byte)'\\'; b[p++] = (byte)'r'; break;
                        case '\t': b[p++] = (byte)'\\'; b[p++] = (byte)'t'; break;
                        default:
                            if (c < 0x20)
                            {
                                b[p++] = (byte)'\\'; b[p++] = (byte)'u'; b[p++] = (byte)'0'; b[p++] = (byte)'0';
                                b[p++] = (byte)Hex[c >> 4]; b[p++] = (byte)Hex[c & 0xF];
                            }
                            else b[p++] = (byte)c;
                            break;
                    }
                }
                else if (c < 0x800)
                {
                    b[p++] = (byte)(0xC0 | (c >> 6));
                    b[p++] = (byte)(0x80 | (c & 0x3F));
                }
                else if (char.IsHighSurrogate(c) && i + 1 < n && char.IsLowSurrogate(s[i + 1]))
                {
                    int cp = char.ConvertToUtf32(c, s[i + 1]);
                    i++;
                    b[p++] = (byte)(0xF0 | (cp >> 18));
                    b[p++] = (byte)(0x80 | ((cp >> 12) & 0x3F));
                    b[p++] = (byte)(0x80 | ((cp >> 6) & 0x3F));
                    b[p++] = (byte)(0x80 | (cp & 0x3F));
                }
                else if (char.IsSurrogate(c))
                {
                    b[p++] = 0xEF; b[p++] = 0xBF; b[p++] = 0xBD; // U+FFFD for a lone surrogate
                }
                else
                {
                    b[p++] = (byte)(0xE0 | (c >> 12));
                    b[p++] = (byte)(0x80 | ((c >> 6) & 0x3F));
                    b[p++] = (byte)(0x80 | (c & 0x3F));
                }
            }
            b[p++] = (byte)'"';
            Length = p;
        }

        const string Hex = "0123456789abcdef";

        public void Long(long v)
        {
            Ensure(20);
            if (v == 0) { Buffer[Length++] = (byte)'0'; return; }
            if (v == long.MinValue) { Ascii("-9223372036854775808"); return; }
            if (v < 0) { Buffer[Length++] = (byte)'-'; v = -v; }
            int start = Length;
            while (v > 0)
            {
                Buffer[Length++] = (byte)('0' + (int)(v % 10));
                v /= 10;
            }
            Array.Reverse(Buffer, start, Length - start);
        }

        /// <summary>A finite double in the shared shortest form; callers skip NaN and infinities.</summary>
        public void Double(double v)
        {
            if (double.IsNaN(v) || double.IsInfinity(v)) { Null(); return; }
            if (v == 0) { Byte((byte)'0'); return; }
            if (Math.Abs(v) < 9007199254740992.0 && v == Math.Floor(v))
            {
                Long((long)v);
                return;
            }
            // Shortest round-trip digits, computed exactly (no runtime formatting or parsing:
            // Mono's are not always correctly rounded): the correctly rounded p-digit decimal
            // for p = 15, 16, 17 (from 1 for subnormals); the first one inside v's rounding
            // interval wins. For normal doubles a shorter form is the 15-digit one with
            // trailing zeros, which are stripped below.
            int digitsLen = 0, exp10 = 0;
            Span<char> dig = stackalloc char[17];
            double a = Math.Abs(v);
            int startP = a >= 2.2250738585072014E-308 ? 15 : 1;
            ulong mant = 0;
            int f = 0;
            for (int p = startP; p <= 17; p++)
            {
                _exact.Digits(a, p, out mant, out f);
                if (p == 17 || _exact.RoundTrips(a, mant, f)) break;
            }
            ulong t = mant;
            while (t > 0) { _num[digitsLen++] = (char)('0' + (int)(t % 10)); t /= 10; }
            for (int i = 0; i < digitsLen; i++) dig[i] = _num[digitsLen - 1 - i];
            exp10 = f + digitsLen - 1; // value = d.ddd x 10^exp10
            while (digitsLen > 1 && dig[digitsLen - 1] == '0') digitsLen--;
            // ECMAScript layout: value = 0.d1d2..dk x 10^n with n = exp10 + 1.
            int k = digitsLen, n = exp10 + 1;
            Ensure(32);
            if (v < 0) Buffer[Length++] = (byte)'-';
            if (k <= n && n <= 21)
            {
                for (int i = 0; i < k; i++) Buffer[Length++] = (byte)dig[i];
                for (int i = k; i < n; i++) Buffer[Length++] = (byte)'0';
            }
            else if (0 < n && n <= 21)
            {
                for (int i = 0; i < n; i++) Buffer[Length++] = (byte)dig[i];
                Buffer[Length++] = (byte)'.';
                for (int i = n; i < k; i++) Buffer[Length++] = (byte)dig[i];
            }
            else if (-6 < n && n <= 0)
            {
                Buffer[Length++] = (byte)'0';
                Buffer[Length++] = (byte)'.';
                for (int i = n; i < 0; i++) Buffer[Length++] = (byte)'0';
                for (int i = 0; i < k; i++) Buffer[Length++] = (byte)dig[i];
            }
            else
            {
                Buffer[Length++] = (byte)dig[0];
                if (k > 1)
                {
                    Buffer[Length++] = (byte)'.';
                    for (int i = 1; i < k; i++) Buffer[Length++] = (byte)dig[i];
                }
                Buffer[Length++] = (byte)'e';
                int e = n - 1;
                Buffer[Length++] = (byte)(e < 0 ? '-' : '+');
                Long(e < 0 ? -e : e);
            }
        }

        /// <summary>A UUID from 16 bytes, lowercase, as a JSON string.</summary>
        public void Uuid(byte[] b, int offset)
        {
            Ensure(38);
            Buffer[Length++] = (byte)'"';
            UuidText(b, offset, Buffer, Length);
            Length += 36;
            Buffer[Length++] = (byte)'"';
        }

        public static void UuidText(byte[] b, int offset, byte[] dst, int at)
        {
            int p = at;
            for (int i = 0; i < 16; i++)
            {
                if (i == 4 || i == 6 || i == 8 || i == 10) dst[p++] = (byte)'-';
                byte x = b[offset + i];
                dst[p++] = (byte)Hex[x >> 4];
                dst[p++] = (byte)Hex[x & 0xF];
            }
        }

        public static string UuidString(byte[] b, int offset)
        {
            var tmp = new byte[36];
            UuidText(b, offset, tmp, 0);
            return System.Text.Encoding.ASCII.GetString(tmp);
        }
    }

    /// <summary>
    /// Does the decimal D x 10^F read back as the double v (round-half-even)? Exact
    /// comparison with the boundaries of v's rounding interval, in fixed-size big
    /// integers allocated once (no allocation per call).
    /// </summary>
    internal sealed class ExactCheck
    {
        const int Words = 80; // 2560 bits: 10^325 x 2^1075 fits
        readonly uint[] _a = new uint[Words];
        readonly uint[] _b = new uint[Words];
        int _na, _nb;

        public bool RoundTrips(double v, ulong d, int f)
        {
            long bits = BitConverter.DoubleToInt64Bits(v);
            int be = (int)((bits >> 52) & 0x7FF);
            ulong m = (ulong)bits & 0xFFFFFFFFFFFFFUL;
            int e2;
            if (be == 0) e2 = -1074;
            else { m |= 1UL << 52; e2 = be - 1075; }
            bool even = (m & 1) == 0;
            // upper boundary (2m + 1) x 2^(e2 - 1); lower (2m - 1) x 2^(e2 - 1), or
            // (4m - 1) x 2^(e2 - 2) when m is the smallest mantissa of its binade
            int hi = Compare(d, f, 2 * m + 1, e2 - 1);
            if (hi > 0 || (hi == 0 && !even)) return false;
            bool narrowLow = m == (1UL << 52) && be > 1;
            int lo = narrowLow ? Compare(d, f, 4 * m - 1, e2 - 2) : Compare(d, f, 2 * m - 1, e2 - 1);
            return lo > 0 || (lo == 0 && even);
        }

        readonly uint[] _n = new uint[Words];
        readonly uint[] _q = new uint[Words];
        int _nn, _nq;

        /// <summary>The p-digit decimal D x 10^F nearest to v &gt; 0 (ties to even), exactly.</summary>
        public void Digits(double v, int p, out ulong d, out int f)
        {
            long bits = BitConverter.DoubleToInt64Bits(v);
            int be = (int)((bits >> 52) & 0x7FF);
            ulong m = (ulong)bits & 0xFFFFFFFFFFFFFUL;
            int e2;
            if (be == 0) e2 = -1074;
            else { m |= 1UL << 52; e2 = be - 1075; }
            int k = (int)Math.Floor(Math.Log10(v)); // estimate of the decimal exponent
            ulong lo = Pow10(p - 1), hi = Pow10(p);
            for (int guard = 0; ; guard++)
            {
                f = k - p + 1;
                d = Divide(m, e2, f);
                if (guard >= 4) return;
                if (d >= hi) k++;
                else if (d < lo) k--;
                else return;
            }
        }

        static ulong Pow10(int n)
        {
            ulong r = 1;
            while (n-- > 0) r *= 10;
            return r;
        }

        /// <summary>round_half_even(m x 2^e2 / 10^f); the quotient is below 2^60.</summary>
        ulong Divide(ulong m, int e2, int f)
        {
            Set(_n, ref _nn, m);
            Set(_q, ref _nq, 1);
            int nb = Math.Max(e2, 0), qb = Math.Max(-e2, 0);
            if (f < 0) { MulPow5(_n, ref _nn, -f); nb += -f; }
            else { MulPow5(_q, ref _nq, f); qb += f; }
            if (nb >= qb) Shl(_n, ref _nn, nb - qb);
            else Shl(_q, ref _nq, qb - nb);
            // shift-subtract division with T = Q x 2^60 shifted right one bit per step
            Shl(_q, ref _nq, 60);
            ulong quotient = 0;
            for (int bit = 60; bit >= 0; bit--)
            {
                if (Cmp(_q, _nq, _n, _nn) <= 0)
                {
                    Sub(_n, ref _nn, _q, _nq);
                    quotient |= 1UL << bit;
                }
                if (bit > 0) Shr1(_q, ref _nq);
            }
            // _n is the remainder R, _q is back to Q: round half to even on 2R vs Q
            Shl(_n, ref _nn, 1);
            int c = Cmp(_n, _nn, _q, _nq);
            if (c > 0 || (c == 0 && (quotient & 1) == 1)) quotient++;
            return quotient;
        }

        static int Cmp(uint[] x, int nx, uint[] y, int ny)
        {
            if (nx != ny) return nx > ny ? 1 : -1;
            for (int i = nx - 1; i >= 0; i--)
            {
                if (x[i] != y[i]) return x[i] > y[i] ? 1 : -1;
            }
            return 0;
        }

        static void Sub(uint[] x, ref int nx, uint[] y, int ny)
        {
            long borrow = 0;
            for (int i = 0; i < nx; i++)
            {
                long t = (long)x[i] - (i < ny ? y[i] : 0) - borrow;
                borrow = t < 0 ? 1 : 0;
                x[i] = (uint)(t + (borrow << 32));
            }
            while (nx > 0 && x[nx - 1] == 0) nx--;
        }

        static void Shr1(uint[] x, ref int n)
        {
            for (int i = 0; i < n; i++)
            {
                x[i] = (x[i] >> 1) | (i + 1 < n ? x[i + 1] << 31 : 0);
            }
            while (n > 0 && x[n - 1] == 0) n--;
        }

        /// <summary>sign(D x 10^F - B x 2^G)</summary>
        int Compare(ulong d, int f, ulong bm, int g)
        {
            Set(_a, ref _na, d);
            Set(_b, ref _nb, bm);
            int a2 = 0, b2 = g;
            if (f >= 0)
            {
                MulPow5(_a, ref _na, f);
                a2 = f;
            }
            else
            {
                MulPow5(_b, ref _nb, -f);
                b2 = g - f;
            }
            if (a2 > b2) Shl(_a, ref _na, a2 - b2);
            else if (b2 > a2) Shl(_b, ref _nb, b2 - a2);
            if (_na != _nb) return _na > _nb ? 1 : -1;
            for (int i = _na - 1; i >= 0; i--)
            {
                if (_a[i] != _b[i]) return _a[i] > _b[i] ? 1 : -1;
            }
            return 0;
        }

        static void Set(uint[] x, ref int n, ulong v)
        {
            x[0] = (uint)v;
            x[1] = (uint)(v >> 32);
            n = x[1] != 0 ? 2 : (x[0] != 0 ? 1 : 0);
        }

        static void MulSmall(uint[] x, ref int n, uint k)
        {
            ulong carry = 0;
            for (int i = 0; i < n; i++)
            {
                ulong t = (ulong)x[i] * k + carry;
                x[i] = (uint)t;
                carry = t >> 32;
            }
            if (carry != 0) x[n++] = (uint)carry;
        }

        static void MulPow5(uint[] x, ref int n, int k)
        {
            while (k >= 13) { MulSmall(x, ref n, 1220703125u); k -= 13; } // 5^13
            uint r = 1;
            while (k-- > 0) r *= 5;
            if (r != 1) MulSmall(x, ref n, r);
        }

        static void Shl(uint[] x, ref int n, int s)
        {
            if (n == 0) return;
            int words = s >> 5, bitsShift = s & 31;
            if (bitsShift != 0)
            {
                uint carry = 0;
                for (int i = 0; i < n; i++)
                {
                    uint w = x[i];
                    x[i] = (w << bitsShift) | carry;
                    carry = w >> (32 - bitsShift);
                }
                if (carry != 0) x[n++] = carry;
            }
            if (words > 0)
            {
                for (int i = n - 1; i >= 0; i--) x[i + words] = x[i];
                for (int i = 0; i < words; i++) x[i] = 0;
                n += words;
            }
        }
    }

    /// <summary>UUIDv7 (RFC 9562): 48-bit Unix ms, version 7, 74 random bits; UUIDv4 for install ids.</summary>
    public static class PulseUuid
    {
        /// <summary>Fills 16 bytes at <paramref name="offset"/>; draws exactly 10 random bytes.</summary>
        public static void V7(long unixMs, IPulseRandom rnd, byte[] dst, int offset)
        {
            dst[offset + 0] = (byte)(unixMs >> 40);
            dst[offset + 1] = (byte)(unixMs >> 32);
            dst[offset + 2] = (byte)(unixMs >> 24);
            dst[offset + 3] = (byte)(unixMs >> 16);
            dst[offset + 4] = (byte)(unixMs >> 8);
            dst[offset + 5] = (byte)unixMs;
            rnd.NextBytes(dst, offset + 6, 10);
            dst[offset + 6] = (byte)(0x70 | (dst[offset + 6] & 0x0F));
            dst[offset + 8] = (byte)(0x80 | (dst[offset + 8] & 0x3F));
        }

        /// <summary>Random UUIDv4; draws exactly 16 random bytes.</summary>
        public static void V4(IPulseRandom rnd, byte[] dst, int offset)
        {
            rnd.NextBytes(dst, offset, 16);
            dst[offset + 6] = (byte)(0x40 | (dst[offset + 6] & 0x0F));
            dst[offset + 8] = (byte)(0x80 | (dst[offset + 8] & 0x3F));
        }
    }
}
