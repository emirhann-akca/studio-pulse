"""Writes sdk_numbers.json: doubles (as IEEE-754 bit patterns) and the text both SDKs
must write for them: the shortest digits that read back to the same double (Python's
repr), laid out like ECMAScript Number#toString (0.1, 50, 1e-7, 1e+21; -0 is 0).

    python contract/golden/sdk_numbers.py
"""

from __future__ import annotations

import json
import math
import random
import struct
from decimal import Decimal
from pathlib import Path


def js(d: float) -> str:
    if d == 0:
        return "0"
    if d.is_integer() and abs(d) < 2**53:
        return str(int(d))
    _, digits, e = Decimal(repr(d)).as_tuple()
    ds = "".join(map(str, digits))
    while len(ds) > 1 and ds.endswith("0"):
        ds, e = ds[:-1], e + 1
    ds = ds.lstrip("0") or "0"
    k = len(ds)
    n = k + e
    if k <= n <= 21:
        out = ds + "0" * (n - k)
    elif 0 < n <= 21:
        out = ds[:n] + "." + ds[n:]
    elif -6 < n <= 0:
        out = "0." + "0" * (-n) + ds
    else:
        x = n - 1
        out = ds[0] + ("." + ds[1:] if k > 1 else "") + "e" + ("+" if x > 0 else "-") + str(abs(x))
    return ("-" if d < 0 else "") + out


def bits(d: float) -> str:
    return format(struct.unpack("<Q", struct.pack("<d", d))[0], "016x")


def main() -> None:
    rnd = random.Random(20261112)
    vals = [0.0, -0.0, 1.0, -1.0, 0.1, 0.2, 0.1 + 0.2, 1 / 3, 2 / 3, 0.42, 0.77, 0.8, 50.0, 17.3,
            18.0, 0.0123, 1.5, 1.25, 3912.25, 1e-7, 1.2e-7, 1e-6, 1.5e-6, 0.000001234, 1e20, 1e21,
            1.5e21, 2.0**53, 2.0**53 + 2, -(2.0**53), 1e300, 1.7976931348623157e308, 5e-324,
            2.2250738585072014e-308, 2.225073858507201e-308, 4.9406564584124654e-322,
            123456789.125, 0.30000000000000004, 9007199254740993.0, 1e15, 1e16, 1e17,
            123456789012345680.0, 0.000123, 1e-5, 4.35, 2.675, 100.5, -0.5, 12345.6789]  # fmt: skip
    vals += [struct.unpack("<d", struct.pack("<Q", rnd.getrandbits(64)))[0] for _ in range(1500)]
    vals += [rnd.random() for _ in range(800)]
    vals += [round(rnd.uniform(-1000, 1000), rnd.randint(0, 6)) for _ in range(400)]
    vals += [rnd.uniform(0, 1) * 10 ** rnd.randint(-12, 25) for _ in range(300)]
    vals += [struct.unpack("<d", struct.pack("<Q", rnd.getrandbits(52)))[0] for _ in range(50)]
    out, seen = [], set()
    for v in vals:
        if math.isnan(v) or math.isinf(v) or bits(v) in seen:
            continue
        seen.add(bits(v))
        out.append([bits(v), js(v)])
    path = Path(__file__).with_name("sdk_numbers.json")
    path.write_text("[\n" + ",\n".join(json.dumps(x) for x in out) + "\n]\n", encoding="utf-8")
    print(f"{len(out)} doubles -> {path}")


if __name__ == "__main__":
    main()
