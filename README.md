# BOM vs LCSC Checker

This script validates a CSV bill of materials (BOM) against supplier data from LCSC/JLCPCB.  
It compares the part information written in the BOM (`Comment`/`Value`) with the official description returned by the LCSC API (or pre-fetched JSONs when offline).

## Features

- Checks that the BOM entry matches supplier data for:
  - Package size (0402, 0603, etc.)
  - Capacitor: capacitance, dielectric, voltage rating
  - Resistor: resistance, tolerance, power rating
  - Inductor: inductance
- Falls back to substring or token matching for generic parts.
- Works offline with cached or pre-fetched JSON files.
- Produces a CSV report with results (`OK`, `WARN`, `FAIL`, `N/A`).

## Usage

```bash
python bom_check_lcsc.py BOM.csv \
    --out report.csv \
    --cache .lcsc_cache \
    --rate 4 \
    --timeout 10
```

### Arguments

* `bom_csv` (positional): Path to the BOM CSV file.
* `--out`: Output report CSV file (default: `bom_check_report.csv`).
* `--cache`: Directory for JSON cache (default: `.lcsc_cache`).
* `--timeout`: HTTP timeout in seconds (default: 10).
* `--rate`: Maximum HTTP requests per second (default: 4).
  Used for pacing API requests. Does **not** delay cached/offline fetches.

### Offline/Debug mode

If JSONs are already available (e.g. `C76906.json`), they can be used directly:

```bash
python bom_check_lcsc.py BOM.csv --offline-json-dir /path/to/jsons
```

## BOM Requirements

CSV must contain:

* A column `LCSC` (or `LCSC#`, `LCSC Code`, `lcsc_code`)
* One of `Comment`, `Comments`, or `Value`
  Optionally:
* `Designator`/`RefDes`/`Ref` for reference designators
* `Qty`/`Quantity` for quantities

Example BOM row:

| Designator | Qty | LCSC   | Comment       |
| ---------- | --- | ------ | ------------- |
| R1         | 10  | C12345 | 10kΩ ±1% 0603 |

## Output Report

The output CSV has columns:

* `Status`: `OK`, `WARN`, `FAIL`, or `N/A`
* `RefDes`: Reference designator
* `Qty`: Quantity
* `LCSC`: Supplier part code
* `BOM_Comment`: Original BOM value
* `LCSC_Package`: Normalised package from supplier
* `LCSC_Describe`: Supplier description
* `Matched`: Features that matched
* `Issues`: Mismatches or missing features
* `FallbackNote`: Additional info (e.g. token overlap)

Console output also summarises total parts checked.

## Performance Notes

* With caching enabled (`--cache`), parts already fetched from LCSC are reused from disk.
* The `--rate` limit only applies to new HTTP requests. Cached/offline lookups return immediately.
* For large BOMs with repeated LCSC codes, performance can be improved by deduplicating lookups in the script.

## Example Run

```bash
python bom_check_lcsc.py BOM.csv --out report.csv --cache .lcsc_cache
```

Console output:

```
OK match for '10kΩ ±1% 0603' → ['package','resistance~token']
FAIL mismatch for '100nF 50V X7R 0603' → ['capacitance: BOM=100nF vs LCSC=10nF']
Checked 75 rows → OK=60, WARN=5, FAIL=8, N/A=2
Wrote: report.csv
```

## Limitations

* LCSC/JLCPCB API is undocumented and subject to change.
* Only common passive components are explicitly parsed.
* For unmatched parts, the checker falls back to token overlap heuristics.
* Reports "unknown" instead of forcing a match when parsing fails.
