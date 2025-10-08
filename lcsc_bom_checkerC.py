#!/usr/bin/env python3
"""
BOM vs LCSC checker

Reads a CSV BOM, fetches LCSC/JLCPCB part data by LCSC code, and checks that the
BOM "Comment" (or "Comments"/"Value") matches the supplier description.

What it checks (when present in the BOM comment):
- Package size (e.g., 0402, 0603)
- Capacitance + dielectric + voltage for capacitors
- Resistance + tolerance + power for resistors
- Inductance for inductors
- Otherwise falls back to substring/token checks against the supplier "describe"

Usage:
  python lcsc_bom_checker.py BOM.csv \
      --out report.csv \
      --cache .lcsc_cache \
      --rate 4 \
      --timeout 10

Offline/debug:
  # Use pre-fetched JSONs per LCSC code (e.g., C76906.json) instead of HTTP

Notes:
- The JLC endpoint is undocumented and may change; be tolerant to missing fields.
- The script is conservative: if parsing fails, it reports "unknown" instead of forcing a match.
- CSV is assumed to have a column "LCSC" and one of {"Comment","Comments","Value"}.
"""

# python3 lcsc_bom_checker.py BOM-lyeonsSA3.csv --out report.csv --cache .lcsc_cache

from __future__ import annotations
import argparse, csv, json, os, re, sys, time, math, hashlib
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import dataclass
try:
    import requests
except Exception:
    requests = None  # for offline use

API_URL = "https://cart.jlcpcb.com/shoppingCart/smtGood/getComponentDetail?componentCode={code}"
UA = "Mozilla/5.0 (LCSC BOM checker)"

# ---- Utilities ----------------------------------------------------------------
PKG_RE = re.compile(r"\b(0[40612]{3}|1206|1210|2010|2512)\b")
CAP_RE = re.compile(r"(?P<val>\d+(\.\d+)?)\s*(?P<u>f|uf|μf|nf|pf)\b", re.I)
RES_RE = re.compile(r"(?P<val>\d+(\.\d+)?)(?P<u>\s*(m|k|meg)?\s*ohm|[km]Ω|mΩ|Ω)\b", re.I)
IND_RE = re.compile(r"(?P<val>\d+(\.\d+)?)\s*(?P<u>uh|μh|nh|h)\b", re.I)
VOLT_RE = re.compile(r"(?P<v>\d+(\.\d+)?)\s*V\b", re.I)
POW_RE = re.compile(r"(?P<p>\d+(\.\d+)?)(\s*(W|mW))\b", re.I)
TOL_RE = re.compile(r"±\s*(?P<t>\d+(\.\d+)?)\s*%\b")
DIELECTRIC_RE = re.compile(r"\b(C0G|NP0|X7R|X5R|Y5V|X6S|X7S|X8R)\b", re.I)

def norm_pkg(s: Optional[str]) -> Optional[str]:
    if not s: return None
    m = PKG_RE.search(s.replace(" ", ""))
    return m.group(1) if m else None

def norm_cap(val: float, unit: str) -> float:
    u = unit.lower().replace("μ","u")
    if u == "f":  scale = 1.0
    elif u in ("uf","uF"): scale = 1e-6
    elif u == "nf": scale = 1e-9
    elif u == "pf": scale = 1e-12
    else: scale = 1.0
    return val * scale

def norm_res(val: float, unit: str) -> float:
    u = unit.lower().replace(" ", "")
    u = u.replace("ohm","").replace("Ω","")
    if u == "m": scale = 1e-3
    elif u == "k": scale = 1e3
    elif u in ("meg","mohm"): scale = 1e6 if u=="meg" else 1e-3
    else: scale = 1.0
    return val * scale

def norm_ind(val: float, unit: str) -> float:
    u = unit.lower().replace("μ","u")
    if u == "h": scale = 1.0
    elif u in ("uh","uH"): scale = 1e-6
    elif u == "nh": scale = 1e-9
    else: scale = 1.0
    return val * scale

def nearly_equal(a: float, b: float, rel=0.05, abs_tol=0.0) -> bool:
    if a is None or b is None: return False
    return abs(a - b) <= max(rel * max(abs(a), abs(b)), abs_tol)

@dataclass
class ParsedComment:
    package: Optional[str] = None
    voltage_v: Optional[float] = None
    tolerance_pct: Optional[float] = None
    power_w: Optional[float] = None
    dielectric: Optional[str] = None
    cap_f: Optional[float] = None
    res_ohm: Optional[float] = None
    ind_h: Optional[float] = None
    raw: str = ""

def parse_comment(s: str) -> ParsedComment:
    p = ParsedComment(raw=s or "")
    if not s:
        return p
    # package
    p.package = norm_pkg(s)
    # voltage
    m = VOLT_RE.search(s)
    if m: p.voltage_v = float(m.group("v"))
    # tol
    m = TOL_RE.search(s)
    if m: p.tolerance_pct = float(m.group("t"))
    # power
    m = POW_RE.search(s)
    if m:
        val = float(m.group("p"))
        unit = m.group(4).lower()
        p.power_w = val/1000.0 if unit == "mw" else val
    # dielectric
    m = DIELECTRIC_RE.search(s)
    if m: p.dielectric = m.group(1).upper().replace("NP0","C0G")
    # cap
    m = CAP_RE.search(s)
    if m:
        p.cap_f = norm_cap(float(m.group("val")), m.group("u"))
    # res
    m = RES_RE.search(s)
    if m:
        p.res_ohm = norm_res(float(m.group("val")), m.group("u"))
    # ind
    m = IND_RE.search(s)
    if m:
        p.ind_h = norm_ind(float(m.group("val")), m.group("u"))


    return p

# ---- LCSC API -----------------------------------------------------------------

def fetch_lcsc(code: str, timeout: float, cache_dir: Optional[Path]=None,
               offline_json_dir: Optional[Path]=None) -> Dict[str, Any]:
    """
    Returns a dict with keys: success (bool), data (dict) or msg (str)
    """
    code = code.strip()
    # Offline path first
    if offline_json_dir:
        cand = offline_json_dir / f"{code}.json"
        if cand.exists():
            with cand.open("r", encoding="utf-8") as f:
                data = json.load(f)
            ok = bool(data.get("data"))
            return {"success": ok, "data": data if ok else None, "msg": None if ok else "no data in JSON"}
    # # Cache
    # j = None
    # if cache_dir:
    #     cache_dir.mkdir(parents=True, exist_ok=True)
    #     cache_path = cache_dir / f"{code}.json"
    #     if cache_path.exists():
    #         try:
    #             j = json.loads(cache_path.read_text(encoding="utf-8"))
    #             if j.get("data"):  # valid
    #                 return {"success": True, "data": j}
    #         except Exception:
    #             pass
    # HTTP
    if requests is None:
        return {"success": False, "msg": "requests not available (offline environment)"}
    url = API_URL.format(code=code)
    headers = {"User-Agent": UA}
    try:
        print(f"fetching LCSC {code} ...")
        r = requests.get(url, headers=headers, timeout=timeout)
        if r.status_code != 200:
            return {"success": False, "msg": f"HTTP {r.status_code}"}
        j = r.json()
        if not j.get("data"):
            return {"success": False, "msg": "no 'data' in response"}
        # cache
        if cache_dir:
            (cache_dir / f"{code}.json").write_text(json.dumps(j, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"success": True, "data": j}
    except Exception as e:
        return {"success": False, "msg": f"request error: {e}"}

# ---- Extractors from LCSC JSON ------------------------------------------------

def lcsc_describe(blob: Dict[str, Any]) -> Dict[str, Any]:
    """Extract normalised fields from LCSC JSON."""
    d = blob.get("data", {})
    out = {
        "describe": d.get("describe") or "",
        "package": d.get("componentSpecificationEn") or None,
        "brand": d.get("componentBrandEn") or None,
        "model": d.get("componentModelEn") or None,
        "attributes": {},
    }
    attrs = d.get("attributes") or []
    for a in attrs:
        k = (a.get("attribute_name_en") or "").strip().lower()
        v = (a.get("attribute_value_name") or "").strip()
        if k:
            out["attributes"][k] = v
    # Attempt to normalise common fields
    # Voltage
    volt = out["attributes"].get("voltage rating")
    if volt:
        m = VOLT_RE.search(volt)
        out["voltage_v"] = float(m.group("v")) if m else None
    else:
        m = VOLT_RE.search(out["describe"])
        out["voltage_v"] = float(m.group("v")) if m else None
    # Capacitance
    cap = out["attributes"].get("capacitance")
    if cap:
        m = CAP_RE.search(cap)
        out["cap_f"] = norm_cap(float(m.group("val")), m.group("u")) if m else None
    else:
        m = CAP_RE.search(out["describe"])
        out["cap_f"] = norm_cap(float(m.group("val")), m.group("u")) if m else None
    # Dielectric (temperature coefficient)
    diel = out["attributes"].get("temperature coefficient")
    if diel:
        out["dielectric"] = diel.upper().replace("NP0","C0G")
    else:
        m = DIELECTRIC_RE.search(out["describe"])
        out["dielectric"] = m.group(1).upper().replace("NP0","C0G") if m else None
    # Package
    if out["package"]:
        out["package"] = norm_pkg(out["package"])
    if not out["package"]:
        out["package"] = norm_pkg(out["describe"])

    # Model/brand cleanup
    if out["model"]:
        out["model"] = out["model"].strip()
    if not out["model"]:
        out["model"] = "none"
    if out["brand"]:
        out["brand"] = out["brand"].strip()
    if not out["brand"]:
        out["brand"] = "none"
    return out

# ---- Comparison ---------------------------------------------------------------

def compare(parsed: ParsedComment, data: Dict[str, Any]) -> Dict[str, Any]:
    issues = []
    matches = []

    # Package
    if parsed.package and data.get("package"):
        if parsed.package == data["package"]:
            matches.append("package")
        else:
            issues.append(f"package: BOM={parsed.package} vs LCSC={data['package']}")

    # Capacitor checks
    if parsed.cap_f is not None and data.get("cap_f") is not None:
        if nearly_equal(parsed.cap_f, data["cap_f"], rel=0.05):
            matches.append("capacitance")
        else:
            # make it uF, nF, pF for readability
            parse_cap_readable = f"{parsed.cap_f*1e6:.0f}uF" if parsed.cap_f >= 1e-6 else (f"{parsed.cap_f*1e9:.0f}nF" if parsed.cap_f >= 1e-9 else f"{parsed.cap_f*1e12:.0f}pF")
            lcsc_cap_readable = f"{data['cap_f']*1e6:.0f}uF" if data['cap_f'] >= 1e-6 else (f"{data['cap_f']*1e9:.0f}nF" if data['cap_f'] >= 1e-9 else f"{data['cap_f']*1e12:.0f}pF")
            issues.append(f"capacitance: BOM={parse_cap_readable} vs LCSC={lcsc_cap_readable}")

    if parsed.dielectric and data.get("dielectric"):
        if parsed.dielectric == data["dielectric"]:
            matches.append("dielectric")
        else:
            issues.append(f"dielectric: BOM={parsed.dielectric} vs LCSC={data['dielectric']}")

    if parsed.voltage_v is not None and data.get("voltage_v") is not None:
        # Do not allow BOM voltage lower than LCSC rating claim
        if parsed.voltage_v <= data["voltage_v"] + 1e-6:
            matches.append("voltage")
        else:
            issues.append(f"voltage: BOM={parsed.voltage_v}V > LCSC={data['voltage_v']}V")

    # Resistor checks (best-effort from 'describe' tokens)
    if parsed.res_ohm is not None:
        desc = (data.get("describe") or "").lower()
        # try to find the same magnitude token in description
        ohm_txt = None
        if parsed.res_ohm >= 1e6:
            ohm_txt = f"{parsed.res_ohm/1e6:.0f}m"  # "1M" often appears as 1M
        elif parsed.res_ohm >= 1e3:
            ohm_txt = f"{parsed.res_ohm/1e3:.0f}k"
        elif parsed.res_ohm >= 1:
            ohm_txt = f"{parsed.res_ohm:.0f}"
        elif parsed.res_ohm >= 1e-3:
            ohm_txt = f"{parsed.res_ohm*1e3:.0f}m"
        if ohm_txt and ohm_txt in desc:
            matches.append("resistance~token")
        else:
            issues.append("resistance: could not confirm from LCSC description")

    # Inductor checks
    if parsed.ind_h is not None:
        desc = (data.get("describe") or "").lower()
        # basic token search μH/nH
        tok = None
        if parsed.ind_h >= 1e-6:
            tok = f"{parsed.ind_h*1e6:g}uh"
        elif parsed.ind_h >= 1e-9:
            tok = f"{parsed.ind_h*1e9:g}nh"
        if tok and tok in desc.replace("μ","u"):
            matches.append("inductance~token")
        else:
            issues.append("inductance: could not confirm from LCSC description")


    # if the part is not a cap/res/ind, check against the model/brand/describe tokens
    if (parsed.cap_f is None and parsed.res_ohm is None and parsed.ind_h is None):
        if parsed.raw.lower() in data.get("model").lower() or parsed.raw.lower() in data.get("brand").lower():
            matches.append("model/brand")
        elif parsed.raw.lower() and data.get("describe").lower() and parsed.raw.lower() in data["describe"].lower():
            matches.append("describe~substring")
        else:
            issues.append("no clear feature match for generic part")

    # Fallback: token containment if nothing matched and nothing failed hard
    fallback_note = None
    if not matches and not issues:
        bom_tokens = set(re.findall(r"[A-Za-z0-9\.\+\-]+", parsed.raw.lower()))
        desc_tokens = set(re.findall(r"[A-Za-z0-9\.\+\-]+", (data.get("describe") or "").lower()))
        inter = bom_tokens & desc_tokens
        if len(inter) >= max(2, len(bom_tokens)//3):
            matches.append("token~overlap")
            fallback_note = f"tokens matched: {sorted(list(inter))[:6]}"
        else:
            issues.append("no clear feature match and low token overlap")
    status = "OK" if issues == [] else ("WARN" if matches else "FAIL")
    return {
        "status": status,
        "matches": ";".join(matches) if matches else "",
        "issues": "; ".join(issues) if issues else "",
        "fallback": fallback_note or "",
    }

# ---- Main ---------------------------------------------------------------------

def find_col(header: list[str], candidates: list[str]) -> Optional[int]:
    low = [h.strip().lower() for h in header]
    for cand in candidates:
        if cand in low:
            return low.index(cand)
    return None

def main():
    ap = argparse.ArgumentParser(description="Check BOM 'Comment' vs LCSC part data.")
    ap.add_argument("bom_csv", help="Path to BOM CSV file")
    ap.add_argument("--out", default="bom_check_report.csv", help="Output CSV report")
    ap.add_argument("--cache", default=".lcsc_cache", help="Directory for API JSON cache")
    ap.add_argument("--force-fetch", action="store_true", help="Ignore cache and re-fetch all parts")
    ap.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout seconds")
    ap.add_argument("--rate", type=float, default=40.0, help="Max requests per second (basic sleep pacing)")
    args = ap.parse_args()

    bom_path = Path(args.bom_csv)
    if not bom_path.exists():
        print(f"ERROR: BOM file not found: {bom_path}", file=sys.stderr)
        sys.exit(2)

    cache_dir = Path(args.cache) if args.cache else None
    offline_dir = Path(args.cache) if not args.force_fetch and args.cache else None
    if offline_dir and not offline_dir.is_dir():
        print(f"ERROR: offline-json-dir not a directory: {offline_dir}", file=sys.stderr)
        sys.exit(2)

    rows = list(csv.reader(bom_path.open(newline="", encoding="utf-8-sig")))
    if not rows:
        print("ERROR: empty CSV", file=sys.stderr)
        sys.exit(2)
    header = rows[0]
    body = rows[1:]

    idx_lcsc = find_col(header, ["lcsc", "lcsc#", "lcsc code", "lcsc_code"])
    idx_comment = find_col(header, ["comment", "comments", "value"])
    idx_ref = find_col(header, ["designator", "refdes", "ref", "designators"])
    idx_qty = find_col(header, ["qty", "quantity"])

    if idx_lcsc is None:
        print("ERROR: Cannot find 'LCSC' column", file=sys.stderr)
        sys.exit(2)
    if idx_comment is None:
        print("ERROR: Cannot find a 'Comment'/'Comments'/'Value' column", file=sys.stderr)
        sys.exit(2)

    report_hdr = [
        "Status","RefDes","Qty","LCSC","BOM_Comment",
        "LCSC_Package","LCSC_Describe","Matched","Issues","FallbackNote"
    ]
    out_rows = [report_hdr]

    last_t = 0.0
    if offline_dir == None and args.rate > 4:
        print("Note: offline-json-dir is set; ignoring --rate > 4", file=sys.stderr)
        args.rate = 4.0
    min_dt = 1.0 / max(args.rate, 0.001)

    total_price = 0.0
    for r in body:
        # guard against ragged rows
        r = r + [""] * (len(header) - len(r))
        lcsc_code = (r[idx_lcsc] or "").strip()
        comment = (r[idx_comment] or "").strip()
        refdes = (r[idx_ref] or "").strip() if idx_ref is not None else ""
        qty = (r[idx_qty] or "").strip() if idx_qty is not None else ""

        if not lcsc_code:
            out_rows.append(["N/A", refdes, qty, "", comment, "", "", "", "no LCSC", ""])
            continue

        # crude pacing
        now = time.time()
        if now - last_t < min_dt:
            time.sleep(min_dt - (now - last_t))
        last_t = time.time()

        fetched = fetch_lcsc(lcsc_code, timeout=args.timeout, cache_dir=cache_dir, offline_json_dir=offline_dir)
        if not fetched.get("success"):
            out_rows.append(["FAIL", refdes, qty, lcsc_code, comment, "", "", "", f"fetch error: {fetched.get('msg')}", ""])
            print(f"Warning: LCSC fetch failed for {lcsc_code}: {fetched.get('msg')}", file=sys.stderr)
            continue

        info = lcsc_describe(fetched["data"])
        parsed = parse_comment(comment)
        cmpres = compare(parsed, info)

        # get parts quality, price and stock info
        # print(f"qty in design: {qty}")
        # if "initialPrice" in fetched["data"]:
        price = float(fetched["data"]["data"].get("initialPrice"))
        stock = int(fetched["data"]["data"].get("stockCount"))
        qty = int(qty) if qty.isdigit() else 1

        # print(f"{parsed.raw} ({lcsc_code}):\t", end="")
        # align output: part name (<10char or padded) + LCSC code (10char) + status
        pname = parsed.raw if len(parsed.raw) <= 13 else (parsed.raw[:11] + "..")
        print(f"{pname:13} {lcsc_code:12} ", end="")
        if cmpres["status"] == "OK":
            ma = cmpres["matches"] if len(cmpres["matches"]) <= 11 else (cmpres["matches"][:11])
            print(f"\033[92mMATCH\033[0m {ma:11}", end="")
        if cmpres["status"] == "FAIL":
            # print FAIL in red
            print(f"\033[91mMISMATCH\033[0m {cmpres["issues"]}")
            print(f"  LCSC : {info}")

        stock_str = ""
        if stock == 0:
            stock_str = "\033[91mOUT OF STOCK\033[0m"
        elif stock < qty:
            stock_str="\033[93mLOW STOCK\033[0m"
        else:
            stock_str=f"\033[92m{stock}\033[0m in stock"

        print(f" | {stock_str:26} ", end="")
        
        print(f" | {price:6}$ each", end="")
        
        print("")



        out_rows.append([
            cmpres["status"],
            refdes,
            qty,
            lcsc_code,
            comment,
            info.get("package") or "",
            (info.get("describe") or "")[:160],
            cmpres["matches"],
            cmpres["issues"],
            cmpres["fallback"],
        ])

        total_price += price * qty



        

    out_path = Path(args.out)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerows(out_rows)

    # Console summary
    total = len(out_rows) - 1
    ok = sum(1 for r in out_rows[1:] if r[0] == "OK")
    warn = sum(1 for r in out_rows[1:] if r[0] == "WARN")
    fail = sum(1 for r in out_rows[1:] if r[0] == "FAIL")
    na = sum(1 for r in out_rows[1:] if r[0] == "N/A")
    print(f"Checked {total} rows → OK={ok}, WARN={warn}, FAIL={fail}, N/A={na}")
    print(f"Wrote: {out_path}")
    print(f"Estimated total price (at qty): ${total_price:.2f} (without shipping/tax)")

if __name__ == "__main__":
    main()
