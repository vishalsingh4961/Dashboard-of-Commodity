"""Render docs/index.html from the metrics table."""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

TEMPLATE = Path(__file__).with_name("template.html")

SERIES = [
    "lme_cash", "lme_3m", "lme_c3m", "lme_stock",
    "shfe_m1", "shfe_m2", "shfe_m3", "shfe_m1_m2", "shfe_m1_m3",
    "shfe_m1_contract", "shfe_m3_contract", "shfe_main_contract",
    "usdcny", "premium_usd", "duty", "misc_cny",
    "import_cost_spot", "arb_spot", "ratio_spot", "be_ratio_spot", "be_premium_spot",
    "import_cost_3m", "arb_3m", "ratio_3m", "be_ratio_3m", "shfe_lme_spread_usd",
]


def _clean(v):
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if hasattr(v, "item"):  # numpy scalar
        return _clean(v.item())
    if isinstance(v, str) and v.lower() == "nan":
        return None
    return v


def _row(r: pd.Series | None) -> dict | None:
    if r is None:
        return None
    return {k: _clean(r.get(k)) for k in ["date", *SERIES]}


OTHER_SERIES = [
    "cn_main", "cn_main_contract", "cn_next", "cn_next_contract", "cn_spread",
    "intl", "intl2", "intl_spread", "usdcny", "cost_usd", "duty", "vat",
    "cn_usd", "premium_usd", "intl_cn", "import_cost_cn", "arb_cn", "net_usd",
]
UNITS = {
    "gold": {"cn": "CNY/g", "intl": "USD/oz"},
    "silver": {"cn": "CNY/kg", "intl": "USD/oz"},
    "crude": {"cn": "CNY/bbl", "intl": "USD/bbl"},
}


def build_others(others: pd.DataFrame | None, cfg: dict) -> list[dict]:
    out = []
    for key, o in (cfg.get("others") or {}).items():
        df = others[others["inst"] == key].sort_values("date") if others is not None and not others.empty else pd.DataFrame()
        series = {"date": [str(d) for d in df["date"]]} if not df.empty else {"date": []}
        for k in OTHER_SERIES:
            series[k] = [_clean(v) for v in df[k]] if (not df.empty and k in df) else []
        full = df.dropna(subset=["arb_cn"]) if not df.empty else df
        latest = full.iloc[-1] if len(full) else (df.iloc[-1] if len(df) else None)
        prev = full.iloc[-2] if len(full) > 1 else None
        row = (lambda r: None if r is None else {k: _clean(r.get(k)) for k in ["date", *OTHER_SERIES]})
        out.append(
            {
                "code": key,
                "name": o.get("name", key.upper()),
                "kind": o["kind"],
                "units": UNITS[o["kind"]],
                "intl_label": o.get("intl_label", "International"),
                "intl2_label": o.get("intl2_label"),
                "cost_usd": float(o.get("cost_usd") or 0),
                "duty": float(o.get("duty") or 0),
                "vat": float(o.get("vat") or 0),
                "near_band": float(o.get("near_band") or 0),
                "latest": row(latest),
                "prev": row(prev),
                "series": series,
            }
        )
    return out


def build_payload(metrics: pd.DataFrame, cfg: dict, log: dict, now: dt.datetime | None = None,
                  others: pd.DataFrame | None = None) -> dict:
    st = cfg.get("settings", {})
    tz = ZoneInfo(st.get("display_tz", "UTC"))
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(tz).date()

    metals = []
    for code, m in cfg["metals"].items():
        df = metrics[metrics["metal"] == code].sort_values("date") if not metrics.empty else pd.DataFrame()
        series = {"date": [str(d) for d in df["date"]]} if not df.empty else {"date": []}
        for k in SERIES:
            series[k] = [_clean(v) for v in df[k]] if (not df.empty and k in df) else []

        full = df.dropna(subset=["arb_spot"]) if not df.empty else df
        latest = full.iloc[-1] if len(full) else (df.iloc[-1] if len(df) else None)
        prev = full.iloc[-2] if len(full) > 1 else None
        last_lme = df.dropna(subset=["lme_cash"])["date"].max() if not df.empty and df["lme_cash"].notna().any() else None
        last_shfe = df.dropna(subset=["shfe_m1"])["date"].max() if not df.empty and df["shfe_m1"].notna().any() else None

        as_of = m.get("premium_as_of")
        try:
            age = (today - pd.to_datetime(as_of).date()).days if as_of else None
        except (ValueError, TypeError):
            age = None
        metals.append(
            {
                "code": code,
                "name": m.get("name", code.upper()),
                "premium_usd": _clean(float(m.get("premium_usd") or 0)),
                "premium_as_of": str(as_of) if as_of else None,
                "premium_age_days": age,
                "duty": float(m.get("duty") or 0),
                "misc_cny": float(m.get("misc_cny") or 0),
                "latest": _row(latest),
                "prev": _row(prev),
                "last_lme": _clean(last_lme),
                "last_shfe": _clean(last_shfe),
                "series": series,
            }
        )

    fx_vals = metrics.dropna(subset=["usdcny"]).sort_values("date") if not metrics.empty else pd.DataFrame()
    if others is not None and not others.empty and others["usdcny"].notna().any():
        o_fx = others.dropna(subset=["usdcny"]).sort_values("date")
        if fx_vals.empty or str(o_fx["date"].iloc[-1]) > str(fx_vals["date"].iloc[-1]):
            fx_vals = o_fx
    fx = None
    if len(fx_vals):
        r = fx_vals.iloc[-1]
        fx = {"value": _clean(r["usdcny"]), "date": _clean(r["date"])}

    return {
        "title": st.get("title", "LME–SHFE arbitrage & spreads"),
        "updated_local": now.astimezone(tz).strftime("%d %b %Y, %H:%M"),
        "tz": st.get("display_tz", "UTC"),
        "today": today.isoformat(),
        "confirmed": bool(cfg.get("assumptions_confirmed")),
        "vat": float(cfg.get("vat", 0.13)),
        "manual_fx": (cfg.get("fx") or {}).get("manual_usdcny"),
        "fx": fx,
        "near_band": float(st.get("near_band_cny", 100)),
        "stale_days": int(st.get("stale_premium_days", 14)),
        "metals": metals,
        "others": build_others(others, cfg),
        "log": log or {},
    }


def render(metrics: pd.DataFrame, cfg: dict, log: dict, now: dt.datetime | None = None,
           others: pd.DataFrame | None = None) -> str:
    payload = build_payload(metrics, cfg, log, now, others)
    blob = json.dumps(payload, ensure_ascii=False, allow_nan=False, default=str).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8")
    return html.replace("__TITLE__", _esc(payload["title"])).replace("/*__DATA__*/null", blob)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
