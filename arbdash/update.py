"""Daily update: fetch -> store raw CSVs -> calculate -> render dashboard.

Usage
  python -m arbdash.update                # normal daily run
  python -m arbdash.update --recompute    # recalculate all history with current config.yaml
  python -m arbdash.update --render-only  # rebuild docs/index.html from stored data
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import traceback
from pathlib import Path

import pandas as pd
import yaml

from . import calc, render, sources

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"

KEYS = {
    "lme": ["date", "metal"],
    "shfe": ["date", "metal", "contract"],
    "intl": ["date", "series"],
    "fx": ["date"],
    "rates": ["date"],
}


def load_config(path: Path = ROOT / "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    for code, m in cfg["metals"].items():
        if not 0 <= float(m.get("duty") or 0) < 0.5:
            raise ValueError(f"{code}: duty must be a decimal like 0.02, got {m.get('duty')}")
    if not 0 < float(cfg.get("vat", 0.13)) < 0.5:
        raise ValueError("vat must be a decimal like 0.13")
    for code, o in (cfg.get("others") or {}).items():
        if o.get("kind") not in calc.UNIT_K:
            raise ValueError(f"others.{code}: kind must be one of {sorted(calc.UNIT_K)}")
        for f in ("duty", "vat"):
            if not 0 <= float(o.get(f) or 0) < 0.5:
                raise ValueError(f"others.{code}: {f} must be a decimal like 0.13, got {o.get(f)}")
    return cfg


def read_csv(name: str) -> pd.DataFrame:
    p = DATA / f"{name}.csv"
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p, dtype={"contract": str})
    if "date" in df:
        df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def upsert(name: str, new: pd.DataFrame) -> pd.DataFrame:
    old = read_csv(name)
    if new is None or new.empty:
        return old
    df = pd.concat([old, new], ignore_index=True) if not old.empty else new.copy()
    df = df.drop_duplicates(KEYS[name], keep="last").sort_values(KEYS[name]).reset_index(drop=True)
    DATA.mkdir(exist_ok=True)
    df.to_csv(DATA / f"{name}.csv", index=False)
    return df


def _last_date(df: pd.DataFrame, metal: str | None = None):
    if df.empty:
        return None
    if metal is not None and "metal" in df:
        df = df[df["metal"] == metal]
    return max(df["date"]) if not df.empty else None


def fetch_all(cfg: dict, today: dt.date) -> dict:
    """Fetch new raw data and upsert it. Returns a per-source status log."""
    log: dict = {"lme": {}, "shfe": {}, "fx": {}}
    backfill = int(cfg["settings"].get("backfill_days", 180))
    default_start = today - dt.timedelta(days=backfill)

    # ---- LME ------------------------------------------------------------
    sources.log("LME: start")
    lme_old = read_csv("lme")
    frames = []
    for code, m in cfg["metals"].items():
        last = _last_date(lme_old, code)
        start = (last - dt.timedelta(days=10)) if last else default_start
        try:
            df = sources.fetch_lme_westmetall(m["westmetall_field"], start)
            log["lme"][code] = {"ok": True, "source": "westmetall", "rows": len(df), "last": str(df["date"].max())}
        except Exception as e:  # noqa: BLE001
            msg = f"westmetall: {e}"
            try:
                df = sources.fetch_lme_sina(m["sina_lme_symbol"], start)
                # keep any official cash we already have for these dates
                if not lme_old.empty:
                    have = lme_old[(lme_old["metal"] == code) & (lme_old["source"] == "westmetall")]
                    df = df[~df["date"].isin(set(have["date"]))]
                log["lme"][code] = {"ok": True, "source": "sina-3m (fallback, no cash price)", "rows": len(df), "note": msg}
            except Exception as e2:  # noqa: BLE001
                df = pd.DataFrame()
                log["lme"][code] = {"ok": False, "error": f"{msg}; sina: {e2}"}
        if not df.empty:
            df["metal"] = code
            frames.append(df)
        sources.log(f"LME {code}: {log['lme'][code]}")
    upsert("lme", pd.concat(frames, ignore_index=True) if frames else None)

    # ---- SHFE / INE (all Chinese futures go into shfe.csv) ----------------
    shfe_old = read_csv("shfe")
    code_map = {m["shfe_code"]: k for k, m in cfg["metals"].items()}
    by_exchange: dict[str, set] = {"shfe": set(code_map)}
    for k, o in (cfg.get("others") or {}).items():
        code_map[o["code"]] = k
        by_exchange.setdefault(o.get("exchange", "shfe"), set()).add(o["code"])
    prev_log = {}
    try:
        prev_log = json.loads((DATA / "run_log.json").read_text())
    except (OSError, ValueError):
        pass
    for exch, codes in by_exchange.items():
        if not codes:
            continue
        keys = [code_map[c] for c in codes]
        old = shfe_old[shfe_old["metal"].isin(keys)] if not shfe_old.empty else shfe_old
        last = _last_date(old)
        start = (last - dt.timedelta(days=4)) if last else default_start
        days = [start + dt.timedelta(days=i) for i in range((today - start).days + 1)]
        # days that failed on the previous run get another try
        retry = [dt.date.fromisoformat(d) for d in (prev_log.get(exch) or {}).get("failed_days", [])]
        # weekdays inside the stored history that have no rows (earlier failures; holidays are cheap 404s)
        gaps = set()
        if last:
            have = set(old["date"])
            d = max(min(have), default_start)
            while d < start:
                if d.weekday() < 5 and d not in have:
                    gaps.add(d)
                d += dt.timedelta(days=1)
        days = sorted(set(days) | gaps | {d for d in retry if d >= default_start})
        sources.log(f"{exch.upper()}: {sorted(codes)} from {start}")
        try:
            df = sources.fetch_shfe_range(days, codes, exch)
            log[exch] = {"ok": True, "source": exch, "rows": len(df), "last": str(df["date"].max())}
            if df.attrs.get("failed_days"):
                log[exch]["failed_days"] = df.attrs["failed_days"]
        except Exception as e:  # noqa: BLE001
            msg = f"{exch}: {e}"
            try:
                df = sources.fetch_shfe_sina(codes, start)
                log[exch] = {"ok": True, "source": "sina (fallback)", "rows": len(df), "last": str(df["date"].max()), "note": msg}
            except Exception as e2:  # noqa: BLE001
                df = pd.DataFrame()
                log[exch] = {"ok": False, "error": f"{msg}; sina: {e2}"}
        sources.log(f"{exch.upper()}: {log[exch]}")
        if not df.empty:
            df["metal"] = df["metal"].map(code_map)
            upsert("shfe", df)

    # ---- International prices for gold / silver / crude -------------------
    intl_old = read_csv("intl")
    frames = []
    log["intl"] = {}
    for k, o in (cfg.get("others") or {}).items():
        for series, spec, label in [(k, o.get("intl"), o.get("intl_label", k)), (f"{k}_2", o.get("intl2"), o.get("intl2_label"))]:
            if not spec:
                continue
            last = _last_date(intl_old[intl_old["series"] == series]) if not intl_old.empty else None
            start = (last - dt.timedelta(days=10)) if last else default_start
            try:
                df, used, errs = sources.fetch_intl(spec, start)
                df["series"] = series
                frames.append(df)
                log["intl"][series] = {"ok": True, "label": label, "source": used, "last": str(df["date"].max()),
                                       **({"note": "; ".join(errs)} if errs else {})}
            except Exception as e:  # noqa: BLE001
                log["intl"][series] = {"ok": False, "label": label, "error": str(e)}
            sources.log(f"{label}: {log['intl'][series]}")
    upsert("intl", pd.concat(frames, ignore_index=True) if frames else None)

    # ---- FX -------------------------------------------------------------
    fx_old = read_csv("fx")
    last = _last_date(fx_old)
    start = (last - dt.timedelta(days=10)) if last else default_start - dt.timedelta(days=10)
    try:
        df = sources.fetch_fx_frankfurter(start, today)
        log["fx"] = {"ok": True, "source": "ECB via frankfurter.dev", "last": str(df["date"].max())}
    except Exception as e:  # noqa: BLE001
        msg = f"frankfurter: {e}"
        try:
            df = sources.fetch_fx_latest_er()
            log["fx"] = {"ok": True, "source": "open.er-api.com (fallback)", "note": msg}
        except Exception as e2:  # noqa: BLE001
            df = pd.DataFrame()
            log["fx"] = {"ok": False, "error": f"{msg}; er-api: {e2}"}
    sources.log(f"FX: {log['fx']}")
    upsert("fx", df)

    # ---- SOFR (for LME full carry) -----------------------------------------
    r_old = read_csv("rates")
    last = _last_date(r_old)
    start = (last - dt.timedelta(days=10)) if last else default_start - dt.timedelta(days=10)
    try:
        df, used = sources.fetch_sofr(start, today)
        log["rates"] = {"ok": True, "source": f"SOFR, {used}", "last": str(df["date"].max())}
    except Exception as e:  # noqa: BLE001
        df = pd.DataFrame()
        log["rates"] = {"ok": False, "error": f"SOFR: {e}"}
    sources.log(f"SOFR: {log['rates']}")
    upsert("rates", df)
    return log


def run(recompute: bool = False, render_only: bool = False, today: dt.date | None = None) -> int:
    today = today or dt.date.today()
    cfg = load_config()
    log_path = DATA / "run_log.json"

    if render_only:
        log = json.loads(log_path.read_text()) if log_path.exists() else {}
    else:
        log = fetch_all(cfg, today)

    existing = read_csv("metrics")
    metrics = calc.compute(read_csv("lme"), read_csv("shfe"), read_csv("fx"), cfg,
                           existing=existing, recompute_all=recompute, today=today, rates=read_csv("rates"))
    metrics = calc.tidy(metrics) if not metrics.empty else metrics
    DATA.mkdir(exist_ok=True)
    if not metrics.empty:
        metrics.to_csv(DATA / "metrics.csv", index=False)

    others = calc.compute_others(read_csv("shfe"), read_csv("intl"), read_csv("fx"), cfg,
                                 existing=read_csv("others"), recompute_all=recompute, today=today)
    others = calc.tidy_others(others) if not others.empty else others
    if not others.empty:
        others.to_csv(DATA / "others.csv", index=False)

    if not render_only:
        log["run_utc"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        log_path.write_text(json.dumps(log, indent=2, default=str))

    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(render.render(metrics, cfg, log, others=others), encoding="utf-8")
    (DOCS / ".nojekyll").touch()

    failed = [k for k in ("lme", "shfe", "ine", "fx") if isinstance(log.get(k), dict) and log[k].get("ok") is False]
    failed += [f"lme:{k}" for k, v in log.get("lme", {}).items() if isinstance(v, dict) and v.get("ok") is False]
    failed += [f"intl:{k}" for k, v in log.get("intl", {}).items() if isinstance(v, dict) and v.get("ok") is False]
    print(json.dumps(log, indent=2, default=str))
    if failed:
        print(f"WARNING: sources failed: {failed}", file=sys.stderr)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--recompute", action="store_true", help="recalculate all history with current config")
    ap.add_argument("--render-only", action="store_true", help="rebuild the dashboard without fetching")
    args = ap.parse_args()
    try:
        sys.exit(run(recompute=args.recompute, render_only=args.render_only))
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
