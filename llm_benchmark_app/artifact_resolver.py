"""artifact_resolver.py — Unified artifact path resolution for LLM Benchmark history.

Resolves artifact paths for a history record using tiered priority:
  Tier 0.5: ResultStore / artifacts_manifest.json (direct exact-name lookups)
  Tier 1:   benchmark_artifacts DB table (result_db records only)
  Tier 2:   sweep_result embedded fields (json_path, report_md, report_png)
  Tier 3:   legacy DB row columns (json_path, markdown_path, png_path)
  Tier 4:   Derive report_dir from any path already found
  Tier 5:   Full glob-scan of report_dir (multi-pattern, charts/-subdir aware)

All tiers run and fill in any gaps; Tier 5 fills whatever remains.
The caller always gets a complete dict (missing = empty string, never None).

Debug log:
  Each resolve call appends a timestamped entry to:
    <report_dir>/artifact_resolver_debug.log
  (silently skipped if the dir is not writable; never raises)

Updated in TASK-LLM-BENCHMARK-HISTORY-ARTIFACT-REGEN-FIX-002-v1:
  Full implementation extracted from LLMBenchmarkApp, extended with:
  - artifacts_manifest.json parsing (loose field aliases)
  - Improved result JSON priority: result.json > result_*.json > sweep_result*.json > ...
  - charts/ subdir PNG resolution
  - summary_json / artifacts_manifest keys in return dict
  - write_artifact_resolver_debug_log() helper
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# ── Return-dict schema (all keys always present) ──────────────────────────────
#
#  report_dir         str  Absolute path to the run directory
#  result_json        str  Absolute path to result.json (or best variant)
#  report_txt         str  Absolute path to report.txt (or best variant)
#  report_md          str  Absolute path to report.md (or best variant)
#  chart_png          str  Absolute path to best chart PNG
#  artifacts_manifest str  Absolute path to artifacts_manifest.json
#  summary_json       str  Absolute path to summary.json
#  customer_pdf       str  Absolute path to customer PDF report
#  customer_docx      str  Absolute path to customer DOCX report
#  pdf_log            str  Absolute path to PDF generation log
#
_KEYS = (
    "report_dir", "result_json", "report_txt", "report_md",
    "chart_png", "artifacts_manifest", "summary_json",
    "customer_pdf", "customer_docx", "pdf_log",
)

# ── Manifest field aliases (lenient parsing) ──────────────────────────────────
_MANIFEST_PATH_FIELDS = ("relative_path", "path", "artifact_path", "file", "filename")
_MANIFEST_TYPE_FIELDS = ("file_type", "artifact_type", "type")

# ── Debug log filename ─────────────────────────────────────────────────────────
_DEBUG_LOG_FILENAME = "artifact_resolver_debug.log"

# ── JSON stems that are NOT result data ──────────────────────────────────────
_JSON_EXCLUDES = frozenset((
    "artifacts_manifest", "summary", "pdf_report_summary",
    "generation_summary", "e2e_latency_histogram", "config",
    "metrics", "parser_profile",
))


def _empty() -> dict:
    return {k: "" for k in _KEYS}


# ── Manifest helpers ──────────────────────────────────────────────────────────

def parse_artifacts_manifest(manifest_path: str) -> list[dict]:
    """Parse artifacts_manifest.json → list of artifact dicts.

    Supports both list format (RS format) and dict-with-nested-list format.
    Returns [] on any read / parse error; caller must fall back to dir scan.
    """
    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        log.debug("parse_artifacts_manifest(%s): %s", manifest_path, exc)
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("artifacts", "files", "items", "entries"):
            if key in data and isinstance(data[key], list):
                return data[key]
        # Single-entry dict — wrap it
        return [data]
    return []


def _manifest_entry_path(entry: dict, run_dir: str) -> str:
    """Extract and resolve a path from a manifest entry.  Returns '' if invalid."""
    raw = ""
    for field in _MANIFEST_PATH_FIELDS:
        v = entry.get(field)
        if v:
            raw = str(v)
            break
    if not raw:
        return ""
    p = Path(raw)
    if not p.is_absolute():
        p = Path(run_dir) / p
    return str(p) if p.is_file() else ""


def _manifest_entry_type(entry: dict) -> str:
    for field in _MANIFEST_TYPE_FIELDS:
        v = entry.get(field)
        if v:
            return str(v).lower()
    return ""


def _apply_manifest(manifest_path: str, run_dir: str, arts: dict) -> None:
    """Fill *arts* from entries in *manifest_path* (skips already-filled keys)."""
    entries = parse_artifacts_manifest(manifest_path)
    for entry in entries:
        ep = _manifest_entry_path(entry, run_dir)
        if not ep:
            continue
        name  = os.path.basename(ep).lower()
        ftype = _manifest_entry_type(entry)

        if not arts["result_json"] and (
            name == "result.json"
            or (ftype == "json" and "result" in name
                and not any(x in name for x in ("summary", "manifest", "histogram", "config")))
        ):
            arts["result_json"] = ep
        elif not arts["summary_json"] and name == "summary.json":
            arts["summary_json"] = ep
        elif not arts["report_txt"] and (
            name == "report.txt" or (ftype in ("text", "txt") and "report" in name)
        ):
            arts["report_txt"] = ep
        elif not arts["report_md"] and (
            name == "report.md" or (ftype == "markdown" and "report" in name)
        ):
            arts["report_md"] = ep
        elif not arts["chart_png"] and (name.endswith(".png") and ftype == "image"):
            arts["chart_png"] = ep
        elif name.endswith(".pdf"):
            if not arts["customer_pdf"]:
                arts["customer_pdf"] = ep
        elif name.endswith(".docx"):
            if not arts["customer_docx"]:
                arts["customer_docx"] = ep


# ── Directory scanner ─────────────────────────────────────────────────────────

def scan_dir_for_artifacts(report_dir: str) -> dict:
    """Glob-scan *report_dir* (and charts/ subdir) → best-guess artifact paths.

    result JSON priority (first match wins):
      1. exact result.json
      2. result_*.json         (excludes: summary, manifest, histogram, config)
      3. benchmark_result*.json
      4. sweep_result*.json
      5. *_result.json  /  *_result_*.json
      6. any *.json            (excluding known non-data stems)

    report txt / md:
      exact name first, then report_*.{ext}, then *_report*.{ext}

    chart PNG:
      1. charts/  — keyword match (chart/histogram/e2e/latency/…)
      2. charts/  — any PNG
      3. root dir — keyword match
      4. root dir — any PNG
    """
    arts = _empty()
    if not report_dir:
        return arts
    p = Path(report_dir)
    if not p.is_dir():
        return arts

    # ── Fixed-name files ──────────────────────────────────────────────────────
    for fname, key in (
        ("artifacts_manifest.json", "artifacts_manifest"),
        ("summary.json",            "summary_json"),
        ("report.txt",              "report_txt"),
        ("report.md",               "report_md"),
    ):
        fp = p / fname
        if fp.is_file():
            arts[key] = str(fp)

    # ── result JSON ───────────────────────────────────────────────────────────
    def _ok_json(f: Path) -> bool:
        return f.stem.lower() not in _JSON_EXCLUDES and not any(
            x in f.stem.lower() for x in _JSON_EXCLUDES
        )

    _rj: str = ""
    # 1 – exact
    _exact = p / "result.json"
    if _exact.is_file():
        _rj = str(_exact)
    # 2 – result_*.json
    if not _rj:
        cands = sorted(
            (f for f in p.glob("result_*.json") if _ok_json(f)),
            key=lambda f: f.stat().st_mtime, reverse=True,
        )
        if cands:
            _rj = str(cands[0])
    # 3 – benchmark_result*.json
    if not _rj:
        cands = sorted(
            (f for f in p.glob("benchmark_result*.json") if _ok_json(f)),
            key=lambda f: f.stat().st_mtime, reverse=True,
        )
        if cands:
            _rj = str(cands[0])
    # 4 – sweep_result*.json
    if not _rj:
        cands = sorted(
            (f for f in p.glob("sweep_result*.json") if _ok_json(f)),
            key=lambda f: f.stat().st_mtime, reverse=True,
        )
        if cands:
            _rj = str(cands[0])
    # 5 – *_result*.json
    if not _rj:
        cands = sorted(
            (f for f in p.glob("*_result*.json") if _ok_json(f)),
            key=lambda f: f.stat().st_mtime, reverse=True,
        )
        if cands:
            _rj = str(cands[0])
    # 6 – any non-excluded *.json
    if not _rj:
        cands = sorted(
            (f for f in p.glob("*.json") if _ok_json(f)),
            key=lambda f: f.stat().st_mtime, reverse=True,
        )
        if cands:
            _rj = str(cands[0])
    arts["result_json"] = _rj

    # ── report.txt (if not exact-matched above) ───────────────────────────────
    if not arts["report_txt"]:
        for pat in ("report_*.txt", "*_report*.txt"):
            cands = sorted(p.glob(pat), key=lambda f: f.stat().st_mtime, reverse=True)
            if cands:
                arts["report_txt"] = str(cands[0])
                break

    # ── report.md (if not exact-matched above) ────────────────────────────────
    if not arts["report_md"]:
        kws = ("report", "sweep", "analysis", "benchmark")
        for pat in ("report_*.md", "*_report*.md", "*.md"):
            cands = sorted(
                p.glob(pat),
                key=lambda f: (
                    not any(kw in f.stem.lower() for kw in kws),
                    -f.stat().st_mtime,
                ),
            )
            if cands:
                arts["report_md"] = str(cands[0])
                break

    # ── PNG chart — charts/ first, then root ──────────────────────────────────
    kws_png = ("chart", "histogram", "e2e", "latency", "analysis", "sweep", "benchmark")
    _charts_sub = p / "charts"
    _png = ""

    for search_dir in (_charts_sub, p):
        if not search_dir.is_dir():
            continue
        # keyword match first
        cands = sorted(
            (f for f in search_dir.glob("*.png")
             if any(kw in f.stem.lower() for kw in kws_png)),
            key=lambda f: f.stat().st_mtime, reverse=True,
        )
        if not cands:
            cands = sorted(search_dir.glob("*.png"),
                           key=lambda f: f.stat().st_mtime, reverse=True)
        if cands:
            _png = str(cands[0])
            break

    arts["chart_png"] = _png

    # ── Customer PDF / DOCX ───────────────────────────────────────────────────
    for ext, key in (("pdf", "customer_pdf"), ("docx", "customer_docx")):
        pref = sorted(
            (f for f in p.glob(f"*.{ext}")
             if any(kw in f.stem.lower() for kw in ("customer", "jisuman", "acceptance"))),
            key=lambda f: f.stat().st_mtime, reverse=True,
        )
        fallback = sorted(p.glob(f"*.{ext}"),
                          key=lambda f: f.stat().st_mtime, reverse=True)
        cands = pref or fallback
        if cands:
            arts[key] = str(cands[0])

    return arts


# ── Debug log ─────────────────────────────────────────────────────────────────

def write_artifact_resolver_debug_log(report_dir: str, info: dict) -> None:
    """Append a timestamped resolver trace to <report_dir>/artifact_resolver_debug.log.
    Silently ignores all errors — debug log must never break main flow.
    """
    try:
        log_path = os.path.join(report_dir, _DEBUG_LOG_FILENAME)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            f"\n=== artifact_resolver  {ts} ===",
            f"run_id/record_id : {info.get('run_id', '?')}",
            f"report_dir       : {info.get('report_dir', '?')}",
            f"manifest_path    : {info.get('manifest_path', '(none)')}",
            "result_json candidates:",
            *[f"  {c}" for c in info.get("result_json_candidates", [])],
            "report candidates:",
            *[f"  {c}" for c in info.get("report_candidates", [])],
            "chart candidates:",
            *[f"  {c}" for c in info.get("chart_candidates", [])],
            f"→ result_json : {info.get('result_json', '(none)')}",
            f"→ report_md   : {info.get('report_md', '(none)')}",
            f"→ report_txt  : {info.get('report_txt', '(none)')}",
            f"→ chart_png   : {info.get('chart_png', '(none)')}",
        ]
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


# ── Directory listing helper (for error messages) ─────────────────────────────

def list_dir_brief(report_dir: str, max_items: int = 20) -> list[str]:
    """Return a sorted list of filenames in *report_dir* (max *max_items* entries)."""
    if not report_dir or not os.path.isdir(report_dir):
        return []
    try:
        items = sorted(os.listdir(report_dir))
        lines = []
        for item in items[:max_items]:
            fp = os.path.join(report_dir, item)
            if os.path.isdir(fp):
                lines.append(f"{item}/")
            else:
                try:
                    size = os.path.getsize(fp)
                    lines.append(f"{item}  ({size:,} B)")
                except OSError:
                    lines.append(item)
        if len(items) > max_items:
            lines.append(f"... ({len(items) - max_items} more)")
        return lines
    except Exception:
        return []


# ── Main resolver ─────────────────────────────────────────────────────────────

def resolve_history_artifacts(
    ref: dict,
    row: dict | None = None,
    sweep_result: dict | None = None,
    *,
    result_db_path: str = "",
) -> dict:
    """Unified artifact resolver for any history record type.

    Args:
      ref:             History record ref dict (keys: source, run_dir, run_id, rid, …)
      row:             Optional DB row dict (result_db / legacy DB)
      sweep_result:    Optional sweep_result dict (embedded artifact fields)
      result_db_path:  Path to result DB file (for Tier 1 benchmark_artifacts query)

    Returns:
      dict with all keys in _KEYS always present (empty string = not found).
    """
    if row is None:
        row = {}
    if sweep_result is None:
        sweep_result = {}

    arts = _empty()

    # ── Seed report_dir ───────────────────────────────────────────────────────
    arts["report_dir"] = (
        ref.get("run_dir", "")
        or (row.get("report_dir") if isinstance(row, dict) else "")
        or sweep_result.get("report_dir", "")
        or ""
    )

    _debug: dict = {
        "run_id":               ref.get("run_id", ref.get("rid", "?")),
        "report_dir":           arts["report_dir"],
        "manifest_path":        "",
        "result_json_candidates": [],
        "report_candidates":    [],
        "chart_candidates":     [],
    }

    # ── Tier 0.5: ResultStore — exact name lookups + manifest ─────────────────
    _rs_dir = arts["report_dir"]
    if _rs_dir and os.path.isdir(_rs_dir):
        # Check for artifacts_manifest.json first
        _mf_path = os.path.join(_rs_dir, "artifacts_manifest.json")
        if os.path.isfile(_mf_path):
            arts["artifacts_manifest"] = _mf_path
            _debug["manifest_path"] = _mf_path
            _apply_manifest(_mf_path, _rs_dir, arts)

        # Direct exact-name lookups (fast; complement manifest resolution)
        for fname, key in (
            ("result.json",             "result_json"),
            ("summary.json",            "summary_json"),
            ("report.txt",              "report_txt"),
            ("report.md",               "report_md"),
            ("artifacts_manifest.json", "artifacts_manifest"),
        ):
            if not arts[key]:
                _fp = os.path.join(_rs_dir, fname)
                if os.path.isfile(_fp):
                    arts[key] = _fp

        # charts/ subdir PNG (exact subdir check before full glob)
        if not arts["chart_png"]:
            _charts_sub = os.path.join(_rs_dir, "charts")
            if os.path.isdir(_charts_sub):
                _pngs = sorted(
                    (str(p) for p in Path(_charts_sub).glob("*.png") if p.is_file()),
                    key=os.path.getmtime, reverse=True,
                )
                if _pngs:
                    arts["chart_png"] = _pngs[0]
                    _debug["chart_candidates"] = _pngs[:5]

    # ── Tier 1: benchmark_artifacts DB table ──────────────────────────────────
    if ref.get("source") == "result_db" and result_db_path:
        run_id = ref.get("run_id", "")
        try:
            import sqlite3 as _sqlite3
            conn = _sqlite3.connect(result_db_path)
            conn.row_factory = _sqlite3.Row
            rows_art = conn.execute(
                "SELECT artifact_type, path FROM benchmark_artifacts WHERE run_id=?",
                (run_id,),
            ).fetchall()
            conn.close()
            for ar in rows_art:
                atype = ar["artifact_type"]
                apath = ar["path"] or ""
                if not apath or not os.path.isfile(apath):
                    continue
                if atype == "customer_pdf_report" and not arts["customer_pdf"]:
                    arts["customer_pdf"] = apath
                elif atype == "customer_docx_report" and not arts["customer_docx"]:
                    arts["customer_docx"] = apath
                elif atype == "pdf_generation_log" and not arts["pdf_log"]:
                    arts["pdf_log"] = apath
        except Exception as exc:
            log.debug("Tier1 DB query failed for run_id=%s: %s", ref.get("run_id"), exc)

    # ── Tier 2: sweep_result embedded fields ──────────────────────────────────
    for src_key, art_key in (
        ("json_path",   "result_json"),
        ("result_json", "result_json"),
        ("report_md",   "report_md"),
        ("report_png",  "chart_png"),
    ):
        if not arts[art_key]:
            _v = sweep_result.get(src_key, "") or ""
            if _v and os.path.isfile(_v):
                arts[art_key] = _v

    # ── Tier 3: legacy DB row columns ─────────────────────────────────────────
    for col, art_key in (
        ("json_path",     "result_json"),
        ("markdown_path", "report_md"),
        ("png_path",      "chart_png"),
    ):
        if not arts[art_key]:
            _v = (row.get(col, "") if isinstance(row, dict) else "") or ""
            if _v and os.path.isfile(_v):
                arts[art_key] = _v

    # ── Tier 4: derive report_dir from any path already found ─────────────────
    if not arts["report_dir"]:
        for k in ("result_json", "report_md", "report_txt", "chart_png",
                  "customer_pdf", "customer_docx"):
            if arts[k]:
                _d = os.path.dirname(arts[k])
                # chart_png may live in charts/ — go up one level
                if os.path.basename(_d).lower() == "charts":
                    _d = os.path.dirname(_d)
                arts["report_dir"] = _d
                break

    # ── Tier 5: glob-scan report_dir for anything still missing ───────────────
    if arts["report_dir"] and os.path.isdir(arts["report_dir"]):
        scanned = scan_dir_for_artifacts(arts["report_dir"])
        for k in _KEYS:
            if not arts[k] and scanned.get(k):
                arts[k] = scanned[k]

    # ── Debug log ─────────────────────────────────────────────────────────────
    _debug.update(
        result_json=arts["result_json"],
        report_md=arts["report_md"],
        report_txt=arts["report_txt"],
        chart_png=arts["chart_png"],
    )
    _log_dir = arts["report_dir"]
    if _log_dir and os.path.isdir(_log_dir):
        write_artifact_resolver_debug_log(_log_dir, _debug)

    return arts
