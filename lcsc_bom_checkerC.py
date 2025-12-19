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
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

try:
    import requests
except Exception:
    requests = None  # for offline use

API_URL = "https://cart.jlcpcb.com/shoppingCart/smtGood/getComponentDetail?componentCode={code}"
UA = "Mozilla/5.0 (LCSC BOM checker)"

# ---- Utilities ----------------------------------------------------------------
PKG_RE = re.compile(r"\b(0[40612]{3}|1206|1210|2010|2512)\b")
CAP_RE = re.compile(r"(?P<val>\d+(\.\d+)?)\s*(?P<u>f|uf|μf|nf|pf)\b", re.I)
RES_RE = re.compile(
    r"(?P<val>\d+(?:\.\d+)?)\s*"
    r"(?P<u>(?:m|k|meg)?\s*ohm|[km]Ω|mΩ|Ω)"
    r"(?=$|[^0-9A-Za-z_])",
    re.I
)
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
        # print(j)
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
        print(f"Comparing package: BOM={parsed.package} vs LCSC={data['package']}")
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

# -----------------------------
# Normalization dictionaries
# -----------------------------

# Imperial SMD size -> metric code (mm*1000-ish used by KiCad) and vice versa
# (only add what you actually use; safe to extend)
IMPERIAL_TO_METRIC = {
    "0201": "0603",  # 0.6x0.3 mm
    "0402": "1005",  # 1.0x0.5 mm
    "0603": "1608",  # 1.6x0.8 mm
    "0805": "2012",
    "1206": "3216",
    "1210": "3225",
    "1812": "4532",
}
METRIC_TO_IMPERIAL = {v: k for k, v in IMPERIAL_TO_METRIC.items()}

# Package family aliases / normalization
PKG_ALIASES = {
    "WSON": {"WSON", "VSON", "DFN", "SON"},  # lots of ambiguity; treat as family signals
    "QFN": {"QFN", "VQFN", "MLF"},
    "LGA": {"LGA"},
    "BGA": {"BGA", "DSBGA", "WLCSP", "CSP"},
    "UDFN": {"UDFN", "DFN"},
    "X2SON": {"X2SON", "XSON", "SON"},
    "SOT": {"SOT", "SOT23", "SOT-23", "SOT-563", "SOT563"},
    "SOP": {"SOP", "SOIC", "TSSOP", "MSOP", "SSOP"},
    "SOD": {"SOD", "SOD123", "SOD-123", "SOD323", "SOD-323"},
}

# Helpful regexes
RE_IMPERIAL = re.compile(r"\b(0201|0402|0603|0805|1206|1210|1812)\b")
RE_METRIC = re.compile(r"\b(0603|1005|1608|2012|3216|3225|4532)\b")
RE_METRIC_KICAD = re.compile(r"\b(\d{4})Metric\b", re.IGNORECASE)

# Embedded, but guarded:
# - optional common prefix letters (C/R/L/D/F) right before the code
# - left side must be start or a non-alnum
# - right side must be end or a non-digit (prevents 0603 in 06030)
# - case-insensitive for prefixes
RE_IMPERIAL_EMBED = re.compile(
    r"(?i)(?:^|[^A-Z0-9])(?:[CRLDF])?(0201|0402|0603|0805|1206|1210|1812)(?=$|[^0-9])"
)

RE_METRIC_EMBED = re.compile(
    r"(?i)(?:^|[^A-Z0-9])(?:[CRLDF])?(0603|1005|1608|2012|3216|3225|4532)(?=$|[^0-9])"
)

RE_PITCH = re.compile(r"\bP\s*([0-9]+(?:\.[0-9]+)?)\b", re.IGNORECASE)
RE_LW = re.compile(r"\bL\s*([0-9]+(?:\.[0-9]+)?)\s*[-_ ]?W\s*([0-9]+(?:\.[0-9]+)?)\b", re.IGNORECASE)
RE_DIM_X = re.compile(r"\b([0-9]+(?:\.[0-9]+)?)\s*[x×]\s*([0-9]+(?:\.[0-9]+)?)\b", re.IGNORECASE)
RE_PINS = re.compile(r"\b(\d+)\s*P\b|\b(\d+)\s*pin\b|\b(\d+)\s*pins\b", re.IGNORECASE)
RE_QFN_PAREN = re.compile(r"\bQFN[- ]?(\d+)\s*\(\s*([0-9.]+)\s*[x×]\s*([0-9.]+)\s*\)", re.IGNORECASE)
RE_SMD2016 = re.compile(r"\bSMD\s*2016\b", re.IGNORECASE)


@dataclass
class Signals:
    sizes_imperial: Set[str]         # e.g. {"0402"}
    sizes_metric: Set[str]           # e.g. {"1005"}
    family_tokens: Set[str]          # e.g. {"QFN", "WSON", "BGA"}
    pin_counts: Set[int]             # e.g. {56}
    dims_mm: Set[Tuple[float, float]]# e.g. {(7.0, 7.0), (2.0, 1.6)}
    pitches_mm: Set[float]           # e.g. {0.4}

    def canonical_sizes(self) -> Set[str]:
        """
        Return canonical set of size codes, preferring imperial but keeping both.
        """
        out = set(self.sizes_imperial)
        # convert metric -> imperial where possible
        for m in self.sizes_metric:
            if m in METRIC_TO_IMPERIAL:
                out.add(METRIC_TO_IMPERIAL[m])
        # also convert imperial -> metric if needed (not returned by default)
        return out

    def canonical_metric_sizes(self) -> Set[str]:
        out = set(self.sizes_metric)
        for i in self.sizes_imperial:
            if i in IMPERIAL_TO_METRIC:
                out.add(IMPERIAL_TO_METRIC[i])
        return out


def _norm_family_token(tok: str) -> Optional[str]:
    t = tok.upper().replace("–", "-").strip()
    for canonical, aliases in PKG_ALIASES.items():
        if t == canonical:
            return canonical
        if t in aliases:
            return canonical
    # handle common explicit forms
    if t.startswith("QFN"):
        return "QFN"
    if t.endswith("BGA") or "BGA" in t:
        return "BGA"
    return None


def extract_signals_from_text(text: str) -> Signals:
    text_u = (text or "").upper()

    sizes_imp = set(RE_IMPERIAL.findall(text_u))
    sizes_met = set(RE_METRIC.findall(text_u))

    if not sizes_imp:
        sizes_imp = set(RE_IMPERIAL_EMBED.findall(text_u))
    if not sizes_met:
        sizes_met = set(RE_METRIC_EMBED.findall(text_u))
    # KiCad style "1005Metric" etc
    for m in RE_METRIC_KICAD.findall(text_u):
        sizes_met.add(m)

    families: Set[str] = set()
    for raw in re.findall(r"[A-Z0-9]+(?:-[A-Z0-9]+)*", text_u):
        fam = _norm_family_token(raw)
        if fam:
            families.add(fam)

    pins: Set[int] = set()
    for a, b, c in RE_PINS.findall(text_u):
        n = a or b or c
        if n:
            pins.add(int(n))

    # QFN-56(7x7)
    m = RE_QFN_PAREN.search(text_u)
    dims: Set[Tuple[float, float]] = set()
    if m:
        pins.add(int(m.group(1)))
        dims.add((float(m.group(2)), float(m.group(3))))
        families.add("QFN")

    # L7.0-W7.0 (KiCad custom footprint naming)
    for lm in RE_LW.finditer(text_u):
        dims.add((float(lm.group(1)), float(lm.group(2))))

    # 7x7 patterns in vendor describe/model
    for dm in RE_DIM_X.finditer(text_u):
        a, b = float(dm.group(1)), float(dm.group(2))
        # filter obviously-non-package dims (rare but helps reduce noise)
        if 0.3 <= a <= 50 and 0.3 <= b <= 50:
            dims.add((a, b))

    pitches: Set[float] = set()
    for pm in RE_PITCH.findall(text_u):
        pitches.add(float(pm))

    # Crystal shorthand: SMD2016 implies 2.0 x 1.6 mm
    if RE_SMD2016.search(text_u):
        dims.add((2.0, 1.6))

    return Signals(
        sizes_imperial=sizes_imp,
        sizes_metric=sizes_met,
        family_tokens=families,
        pin_counts=pins,
        dims_mm=dims,
        pitches_mm=pitches,
    )


def extract_signals(bom_footprint: str, fetched: Dict[str, Any]) -> Tuple[Signals, Signals]:
    # BOM signals: footprint string only (usually strongest)
    bom_sig = extract_signals_from_text(bom_footprint)

    # Fetched signals: combine package/describe/model/attributes values
    parts: List[str] = []
    for k in ("package", "describe", "brand", "model"):
        v = fetched.get(k)
        if v:
            parts.append(str(v))
    # attributes can contain more hints (sometimes includes "package" style text)
    attrs = fetched.get("attributes", {})
    if isinstance(attrs, dict):
        for ak, av in attrs.items():
            if av is None:
                continue
            parts.append(f"{ak}:{av}")
    fetched_sig = extract_signals_from_text(" | ".join(parts))
    
    # If fetched['package'] is an exact imperial code, ensure it's captured
    pkg = fetched.get("package")
    if isinstance(pkg, str):
        pkg_u = pkg.strip().upper()
        if pkg_u in IMPERIAL_TO_METRIC:
            fetched_sig.sizes_imperial.add(pkg_u)
        if pkg_u in METRIC_TO_IMPERIAL:
            fetched_sig.sizes_metric.add(pkg_u)

    return bom_sig, fetched_sig


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _dims_close(d1: Tuple[float, float], d2: Tuple[float, float], tol: float = 0.15) -> bool:
    # allow swapped L/W; tol in mm
    (a1, b1), (a2, b2) = d1, d2
    return (abs(a1 - a2) <= tol and abs(b1 - b2) <= tol) or (abs(a1 - b2) <= tol and abs(b1 - a2) <= tol)


def judge_match(bom_fp: str, fetched: Dict[str, Any]) -> Tuple[str, str]:
    """
    Return (verdict, explanation).
    verdict ∈ {"MATCH", "MISMATCH", "UNKNOWN"}
    """
    bom_sig, fet_sig = extract_signals(bom_fp, fetched)

    # 1) Strong check for passive sizes (0201/0402/0603...) if present
    bom_sizes = bom_sig.canonical_sizes()
    fet_sizes = fet_sig.canonical_sizes()

    if bom_sizes:
        if fet_sizes:
            if bom_sizes & fet_sizes:
                return "MATCH", f"Passive size match: BOM {sorted(bom_sizes)} vs fetched {sorted(fet_sizes)}"
            else:
                return "MISMATCH", f"Passive size mismatch: BOM {sorted(bom_sizes)} vs fetched {sorted(fet_sizes)}"
        else:
            # fetched has no explicit size, but may have metric size
            fet_metric = fet_sig.canonical_metric_sizes()
            bom_metric = bom_sig.canonical_metric_sizes()
            if bom_metric and fet_metric and (bom_metric & fet_metric):
                return "MATCH", f"Passive metric-size match: BOM {sorted(bom_metric)} vs fetched {sorted(fet_metric)}"
            return "UNKNOWN", f"BOM indicates passive size {sorted(bom_sizes)} but fetched has no size signal (package/describe/model missing size)"

    # 2) For IC/connector packages: use family+pins+dims/pitch when available
    bom_fam = bom_sig.family_tokens
    fet_fam = fet_sig.family_tokens

    # family intersection helps, but can be ambiguous (e.g., SON/DFN/WSON)
    fam_hit = bool(bom_fam & fet_fam)

    # pin count check if both have one (or both include the same pin count)
    pin_hit = False
    if bom_sig.pin_counts and fet_sig.pin_counts:
        pin_hit = bool(bom_sig.pin_counts & fet_sig.pin_counts)

    # dims check if both have dims
    dim_hit = False
    if bom_sig.dims_mm and fet_sig.dims_mm:
        for d1 in bom_sig.dims_mm:
            for d2 in fet_sig.dims_mm:
                if _dims_close(d1, d2):
                    dim_hit = True
                    break
            if dim_hit:
                break

    # pitch check if both have pitch
    pitch_hit = False
    if bom_sig.pitches_mm and fet_sig.pitches_mm:
        for p1 in bom_sig.pitches_mm:
            for p2 in fet_sig.pitches_mm:
                if abs(p1 - p2) <= 0.02:
                    pitch_hit = True
                    break
            if pitch_hit:
                break

    # decision logic:
    # - If we have at least two independent confirming signals => MATCH
    # - If we have a strong contradicting signal (pins mismatch OR dims mismatch) => MISMATCH
    # - else UNKNOWN
    confirms = sum([fam_hit, pin_hit, dim_hit, pitch_hit])
    contradicts = 0

    if bom_sig.pin_counts and fet_sig.pin_counts and not pin_hit:
        contradicts += 1
    if bom_sig.dims_mm and fet_sig.dims_mm and not dim_hit:
        # dims mismatch is meaningful if BOM had a single clear dim and fetched had a single clear dim
        if len(bom_sig.dims_mm) == 1 and len(fet_sig.dims_mm) == 1:
            contradicts += 1

    # If BOM looks like it encodes a very specific package name and fetched describe is generic,
    # avoid false mismatches.
    if contradicts >= 1 and confirms == 0:
        return "MISMATCH", (
            f"No match signals and at least one contradiction. "
            f"BOM fam={sorted(bom_fam)} pins={sorted(bom_sig.pin_counts)} dims={sorted(bom_sig.dims_mm)} pitch={sorted(bom_sig.pitches_mm)} "
            f"vs fetched fam={sorted(fet_fam)} pins={sorted(fet_sig.pin_counts)} dims={sorted(fet_sig.dims_mm)} pitch={sorted(fet_sig.pitches_mm)}"
        )

    if confirms >= 2:
        return "MATCH", (
            f"Confirmed by {confirms} signals (family/pins/dims/pitch). "
            f"BOM fam={sorted(bom_fam)} pins={sorted(bom_sig.pin_counts)} dims={sorted(bom_sig.dims_mm)} pitch={sorted(bom_sig.pitches_mm)} "
            f"vs fetched fam={sorted(fet_fam)} pins={sorted(fet_sig.pin_counts)} dims={sorted(fet_sig.dims_mm)} pitch={sorted(fet_sig.pitches_mm)}"
        )

    # weak single-signal match: treat as UNKNOWN unless it's a family+pins exact
    if fam_hit and pin_hit:
        return "MATCH", f"Family+pin match: fam={sorted(bom_fam & fet_fam)} pins={sorted(bom_sig.pin_counts & fet_sig.pin_counts)}"

    s = ("Fam:" + f"{sorted(bom_fam)}" + "/" + f"{sorted(fet_fam)}" if (bom_fam or fet_fam) else "") + \
        (" Pins:" + f"{sorted(bom_sig.pin_counts)}" + "/" + f"{sorted(fet_sig.pin_counts)}" if (bom_sig.pin_counts or fet_sig.pin_counts) else "") + \
        (" Dims:" + f"{sorted(bom_sig.dims_mm)}" + "/" + f"{sorted(fet_sig.dims_mm)}" if (bom_sig.dims_mm or fet_sig.dims_mm) else "") + \
        (" Pitch:" + f"{sorted(bom_sig.pitches_mm)}" + "/" + f"{sorted(fet_sig.pitches_mm)}" if (bom_sig.pitches_mm or fet_sig.pitches_mm) else "") 
    if confirms == 1:
        return "UNKNOWN", (
            f"WEAK match: " + s
        )

    return "UNKNOWN", (
        f"MISSING info: " + s
    )

# ---- Main ---------------------------------------------------------------------

def find_col(header: list[str], candidates: list[str]) -> Optional[int]:
    low = [h.strip().lower() for h in header]
    for cand in candidates:
        if cand in low:
            return low.index(cand)
    return None

def main():
    print("LCSC BOM Checker")
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
    idx_footprint = find_col(header, ["footprint", "package"])
    idx_ref = find_col(header, ["designator", "refdes", "ref", "designators"])
    idx_qty = find_col(header, ["qty", "quantity"])

    if idx_lcsc is None:
        print("ERROR: Cannot find 'LCSC' column", file=sys.stderr)
        sys.exit(2)
    if idx_comment is None:
        print("ERROR: Cannot find a 'Comment'/'Comments'/'Value' column", file=sys.stderr)
        sys.exit(2)
    if idx_ref is None:
        print("WARNING: Cannot find 'RefDes' column; proceeding without it", file=sys.stderr)
    if idx_qty is None:
        print("WARNING: Cannot find 'Qty' column; proceeding without it", file=sys.stderr)
    if idx_footprint is None:
        print("WARNING: Cannot find 'Footprint' column; proceeding without it", file=sys.stderr)

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
        footprint = (r[idx_footprint] or "").strip() if idx_footprint is not None else ""
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

        # print("")
        # # print(info)
        # print(f"BOM: {footprint}")
        # print(f"Fetched LCSC {lcsc_code}: {info}")
        # print("")
        # print(f"Package match verdict: {verdict} ({why})")
        
        parsed = parse_comment(comment)
        cmpres = compare(parsed, info)
        verdict, why = judge_match(footprint, info)
        cmpres["why"] = why
        if cmpres["status"] == "OK" and verdict == "MATCH":
            cmpres["status"] = "OK"
        elif cmpres["status"] == "FAIL" or verdict == "MISMATCH":
            cmpres["status"] = "FAIL"
            if verdict == "MISMATCH":
                cmpres["issues"] = "Package "
        if verdict == "UNKNOWN":
            cmpres["warn"] = "WARN"
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
            print(f"\033[91mMISMATCH\033[0m {cmpres["issues"]}", end="")
            # print(f"  LCSC : {info}")

        stock_str = ""
        if stock == 0:
            stock_str = "\033[91mOUT OF STOCK\033[0m"
        elif stock < qty:
            stock_str="\033[93mLOW STOCK\033[0m"
        else:
            stock_str=f"\033[92m{stock}\033[0m in stock"

        print(f" | {stock_str:26} ", end="")
        
        print(f" | {price:6}$ each", end="")

        print(f" | ", end="")
        if cmpres["status"] == "FAIL":
            print(cmpres["why"], end="")
        if "warn" in cmpres:
            print(why, end="")
        
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
