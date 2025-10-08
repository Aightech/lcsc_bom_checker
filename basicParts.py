#!/usr/bin/env python3
"""
Fetch capacitance for a list of LCSC capacitor part IDs, sort by increasing value,
and print the sorted list.

Endpoint:
  https://cart.jlcpcb.com/shoppingCart/smtGood/getComponentDetail?componentCode=<LCSC>

Notes:
- Extracts capacitance from structured attributes when available; otherwise falls back
  to parsing the 'describe' string.
- Outputs human-readable units and the value in farads.
- Basic on-disk cache to avoid repeated HTTP calls.

Python ≥3.8
"""

import re
import json
import time
import argparse
from pathlib import Path
from typing import Optional, Tuple, List, Dict

import requests

API = "https://cart.jlcpcb.com/shoppingCart/smtGood/getComponentDetail?componentCode={code}"
UA  = "Mozilla/5.0 (+cap_list)"
CAP_RE = re.compile(r"(?P<val>\d+(?:\.\d+)?)\s*(?P<u>f|uf|μf|nf|pf)", re.I)

def to_farads(val: float, unit: str) -> float:
    u = unit.lower().replace("μ", "u")
    if u == "f":   return val
    if u == "uf":  return val * 1e-6
    if u == "nf":  return val * 1e-9
    if u == "pf":  return val * 1e-12
    # fallback
    return val

def parse_cap_from_text(text: str) -> Optional[float]:
    if not text:
        return None
    m = CAP_RE.search(text)
    if not m:
        return None
    return to_farads(float(m.group("val")), m.group("u"))

def fetch_lcsc(code: str, timeout: float = 10.0, cache_dir: Optional[Path] = None) -> Dict:
    """
    Return the raw JSON dict from the API (with top-level 'data'), or {} on failure.
    Uses a simple JSON cache if cache_dir is provided.
    """
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cpath = cache_dir / f"{code}.json"
        if cpath.exists():
            try:
                return json.loads(cpath.read_text(encoding="utf-8"))
            except Exception:
                pass

    try:
        r = requests.get(API.format(code=code), headers={"User-Agent": UA}, timeout=timeout)
        if r.status_code != 200:
            return {}
        j = r.json()
        if cache_dir:
            (cache_dir / f"{code}.json").write_text(json.dumps(j, ensure_ascii=False, indent=2), encoding="utf-8")
        return j
    except Exception:
        return {}

def extract_capacitance_f(api_json: Dict) -> Optional[float]:
    """
    Try structured attributes first, then free text.
    """
    d = (api_json or {}).get("data") or {}
    # 1) structured attributes
    attrs = d.get("attributes") or []
    for a in attrs:
        name = (a.get("attribute_name_en") or "").strip().lower()
        if name == "capacitance":
            v = (a.get("attribute_value_name") or "").strip()
            cap_f = parse_cap_from_text(v)
            if cap_f is not None:
                return cap_f
    # 2) description
    desc = d.get("describe") or ""
    cap_f = parse_cap_from_text(desc)
    if cap_f is not None:
        return cap_f
    # 3) model/spec fields sometimes contain it
    for k in ("componentModelEn", "componentSpecificationEn"):
        cap_f = parse_cap_from_text(d.get(k) or "")
        if cap_f is not None:
            return cap_f
    return None

def fmt_si_f(F: float) -> str:
    if F >= 1:
        return f"{F:g} F"
    if F >= 1e-3:
        return f"{F*1e3:g} mF"
    if F >= 1e-6:
        return f"{F*1e6:g} µF"
    if F >= 1e-9:
        return f"{F*1e9:g} nF"
    return f"{F*1e12:g} pF"

DEFAULT_IDS = [
"C1523","C1525","C1530","C1532","C1538","C1546","C1547","C1548","C1549","C1554",
"C1555","C1562","C1567","C1588","C1594","C1603","C1604","C1613","C1620","C1622",
"C1623","C1631","C1634","C1644","C1647","C1648","C1653","C1658","C1663","C1664",
"C1671","C1710","C1729","C1739","C1743","C1744","C1779","C1790","C1798","C1804",
"C1846","C1848","C5378","C9196","C12530","C12891","C13585","C13967","C14663","C14857",
"C14858","C15008","C15195","C15525","C15849","C15850","C16772","C16780","C19666","C19702",
"C21117","C21120","C21122","C23630","C23733","C24497","C28233","C28260","C28323","C29823",
"C32949","C38523","C45783","C46653","C49678","C50254","C52923","C53134","C53987","C57112",
"C59461","C96123","C96446","C107145","C307331","C377773","C440198","C1322360"
]

def main():
    ap = argparse.ArgumentParser(description="List capacitance for LCSC capacitor IDs (sorted ascending).")
    ap.add_argument("--ids", nargs="*", default=DEFAULT_IDS, help="LCSC IDs (default: built-in list)")
    ap.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout seconds")
    ap.add_argument("--cache", type=Path, default=Path(".lcsc_cache_cap"), help="Cache directory")
    ap.add_argument("--rate", type=float, default=6.0, help="Max requests per second (simple pacing)")
    args = ap.parse_args()

    min_dt = 1.0 / max(args.rate, 0.1)
    last = 0.0

    results: List[Tuple[str, Optional[float]]] = []
    for code in args.ids:
        # pacing
        now = time.time()
        dt = now - last
        if dt < min_dt:
            time.sleep(min_dt - dt)
        last = time.time()

        j = fetch_lcsc(code, timeout=args.timeout, cache_dir=args.cache)
        cap_f = extract_capacitance_f(j)
        results.append((code, cap_f))

    # separate known vs unknown, sort known by F ascending
    known = [(c, f) for c, f in results if f is not None]
    unknown = [c for c, f in results if f is None]
    known.sort(key=lambda x: x[1])

    # print
    for code, F in known:
        print(f"{code:>9s}  {fmt_si_f(F):>10s}  ({F:.12g} F)")
    if unknown:
        print("\n# No capacitance parsed for:")
        for code in unknown:
            print(code)

if __name__ == "__main__":
    main()
