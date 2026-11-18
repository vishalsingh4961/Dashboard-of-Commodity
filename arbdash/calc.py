"""Arbitrage and spread calculations.

All prices per tonne. SHFE prices are CNY and include VAT; LME prices are USD.

Import cost (CNY/t)  = (LME + CIF premium) x USDCNY x (1 + duty) x (1 + VAT) + misc costs
Import P/L  (CNY/t)  = SHFE - import cost                       (> 0 => import window open)
Ratio                = SHFE / LME
Breakeven ratio      = import cost / LME
Breakeven premium    = (SHFE - misc) / (USDCNY x (1+duty) x (1+VAT)) - LME
                       (highest CIF premium an importer could pay and still break even)

"Spot" legs use SHFE 1st month (连一) vs LME cash.
"3M"   legs use SHFE 3rd month (连三) vs LME 3-month.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd


def _month_code(d: dt.date, k: int) -> str:
    m = d.month - 1 + k
    y, mm = d.year + m // 12, m % 12 + 1
    return f"{y % 100:02d}{mm:02d}"


def shfe_curve(shfe: pd.DataFrame) -> pd.DataFrame:
    """One row per (date, metal): 1st/2nd/3rd-month settlement prices + most-active contract."""
    if shfe.empty:
        return pd.DataFrame(columns=["date", "metal"])
    out = []
    for (d, metal), g in shfe.groupby(["date", "metal"]):
        g = g.dropna(subset=["settle"])
        if g.empty:
            continue
        by_c = g.set_index("contract")
        row = {"date": d, "metal": metal}
        for k in (1, 2, 3):
            c = _month_code(d, k)
            row[f"shfe_m{k}"] = float(by_c["settle"].get(c, np.nan))
            row[f"shfe_m{k}_contract"] = f"{metal}{c}" if c in by_c.index else ""
        if g["open_interest"].notna().any():
            main = g.loc[g["open_interest"].idxmax()]
            row["shfe_main"] = float(main["settle"])
            row["shfe_main_contract"] = f"{metal}{main['contract']}"
        else:
            row["shfe_main"], row["shfe_main_contract"] = np.nan, ""
        out.append(row)
    return pd.DataFrame(out)


def compute(
    lme: pd.DataFrame,
    shfe: pd.DataFrame,
    fx: pd.DataFrame,
    cfg: dict,
    existing: pd.DataFrame | None = None,
    recompute_all: bool = False,
    today: dt.date | None = None,
) -> pd.DataFrame:
    """Build the metrics table. Rows older than the recompute window keep their stored values."""
    today = today or dt.date.today()
    vat = float(cfg.get("vat", 0.13))
    manual_fx = (cfg.get("fx") or {}).get("manual_usdcny")
    # tolerate missing raw files (e.g. a source has never succeeded yet)
    if lme.empty:
        lme = pd.DataFrame(columns=["date", "metal", "cash", "three_m", "stock"])
    if shfe.empty:
        shfe = pd.DataFrame(columns=["date", "metal", "contract", "settle", "open_interest"])
    if fx.empty:
        fx = pd.DataFrame(columns=["date", "usdcny"])
    curve = shfe_curve(shfe)

    fx = fx.dropna(subset=["usdcny"]).sort_values("date").copy()
    fx["date"] = pd.to_datetime(fx["date"])

    frames = []
    for metal, m in cfg["metals"].items():
        l = lme[lme["metal"] == metal][["date", "cash", "three_m", "stock"]]
        c = curve[curve["metal"] == metal].drop(columns=["metal"]) if not curve.empty else pd.DataFrame(columns=["date"])
        df = pd.merge(l, c, on="date", how="outer").sort_values("date")
        if df.empty:
            continue
        df["date"] = pd.to_datetime(df["date"])
        if manual_fx:
            df["usdcny"] = float(manual_fx)
        elif not fx.empty:
            df = pd.merge_asof(df, fx[["date", "usdcny"]], on="date", direction="backward", tolerance=pd.Timedelta(days=5))
        else:
            df["usdcny"] = np.nan
        df["metal"] = metal
        df = df.rename(columns={"cash": "lme_cash", "three_m": "lme_3m", "stock": "lme_stock"})

        prem = float(m.get("premium_usd") or 0)
        duty = float(m.get("duty") or 0)
        misc = float(m.get("misc_cny") or 0)
        df["premium_usd"], df["duty"], df["vat"], df["misc_cny"] = prem, duty, vat, misc
        frames.append(df)

    if not frames:
        return existing if existing is not None else pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    for col in ["lme_cash", "lme_3m", "lme_stock", "shfe_m1", "shfe_m2", "shfe_m3", "shfe_main", "usdcny"]:
        if col not in df:
            df[col] = np.nan
    df = _derive(df)
    df["date"] = df["date"].dt.date

    df = _freeze(df, existing, cfg, today, recompute_all, "metal")
    return df.sort_values(["metal", "date"]).reset_index(drop=True)


def _freeze(df, existing, cfg, today, recompute_all, key):
    """Keep stored rows older than the recompute window; take fresh values for the rest."""
    if existing is None or existing.empty or recompute_all:
        return df
    window = int(cfg.get("settings", {}).get("recompute_window_days", 7))
    cutoff = today - dt.timedelta(days=window)
    ex = existing.copy()
    ex["date"] = pd.to_datetime(ex["date"]).dt.date
    frozen = ex[ex["date"] < cutoff]
    keys = set(zip(frozen["date"], frozen[key]))
    fresh = df[[(d, k) not in keys for d, k in zip(df["date"], df[key])]]
    return pd.concat([frozen, fresh], ignore_index=True)


# ---------------------------------------------------------------------------
# Precious metals and crude
# ---------------------------------------------------------------------------
OZ_G = 31.1034768
# multiply a USD-per-intl-unit price by FX x K to get CNY per China unit
UNIT_K = {"gold": 1 / OZ_G, "silver": 1000 / OZ_G, "crude": 1.0}
CN_UNIT = {"gold": "CNY/g", "silver": "CNY/kg", "crude": "CNY/bbl"}
INTL_UNIT = {"gold": "USD/oz", "silver": "USD/oz", "crude": "USD/bbl"}


def active_contracts(ex: pd.DataFrame) -> pd.DataFrame:
    """Per (date, metal): most-active contract by open interest (main) and the most-active later one (next)."""
    out = []
    if ex.empty:
        return pd.DataFrame(columns=["date", "metal"])
    for (d, metal), g in ex.groupby(["date", "metal"]):
        g = g.dropna(subset=["settle"])
        if g.empty or not g["open_interest"].notna().any():
            continue
        g = g.sort_values("contract")
        main = g.loc[g["open_interest"].idxmax()]
        later = g[g["contract"] > main["contract"]]
        row = {"date": d, "metal": metal, "cn_main": float(main["settle"]), "cn_main_contract": f"{metal}{main['contract']}"}
        if not later.empty and later["open_interest"].notna().any():
            nxt = later.loc[later["open_interest"].idxmax()]
            row["cn_next"], row["cn_next_contract"] = float(nxt["settle"]), f"{metal}{nxt['contract']}"
        else:
            row["cn_next"], row["cn_next_contract"] = np.nan, ""
        out.append(row)
    return pd.DataFrame(out)


def compute_others(
    ex: pd.DataFrame,
    intl: pd.DataFrame,
    fx: pd.DataFrame,
    cfg: dict,
    existing: pd.DataFrame | None = None,
    recompute_all: bool = False,
    today: dt.date | None = None,
) -> pd.DataFrame:
    """China (SHFE/INE most-active) vs international, per instrument in cfg['others'].

    cn_usd        China price in international units, VAT removed (no duty)
    premium_usd   cn_usd - international          (Shanghai premium; crude: SC - Brent)
    import cost   (intl + cost_usd) x FX x K x (1+duty) x (1+VAT)        [China units]
    arb_cn        China price - import cost                              [China units]
    net_usd       arb_cn expressed per oz / bbl
    """
    today = today or dt.date.today()
    others = cfg.get("others") or {}
    if not others:
        return pd.DataFrame()
    if ex.empty:
        ex = pd.DataFrame(columns=["date", "metal", "contract", "settle", "open_interest"])
    if intl.empty:
        intl = pd.DataFrame(columns=["date", "series", "price"])
    if fx.empty:
        fx = pd.DataFrame(columns=["date", "usdcny"])
    manual_fx = (cfg.get("fx") or {}).get("manual_usdcny")
    fx = fx.dropna(subset=["usdcny"]).sort_values("date").copy()
    fx["date"] = pd.to_datetime(fx["date"])
    act = active_contracts(ex[ex["metal"].isin(list(others))])

    frames = []
    for key, o in others.items():
        kind = o["kind"]
        a = act[act["metal"] == key].drop(columns=["metal"]) if not act.empty else pd.DataFrame(columns=["date"])
        i1 = intl[intl["series"] == key][["date", "price"]].rename(columns={"price": "intl"})
        i2 = intl[intl["series"] == f"{key}_2"][["date", "price"]].rename(columns={"price": "intl2"})
        df = a.merge(i1, on="date", how="outer").merge(i2, on="date", how="outer")
        if df.empty:
            continue
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")
        if manual_fx:
            df["usdcny"] = float(manual_fx)
        elif not fx.empty:
            df = pd.merge_asof(df, fx[["date", "usdcny"]], on="date", direction="backward", tolerance=pd.Timedelta(days=5))
        else:
            df["usdcny"] = np.nan
        for col in ["cn_main", "cn_next", "intl", "intl2"]:
            if col not in df:
                df[col] = np.nan
        df["inst"], df["kind"] = key, kind
        df["cost_usd"] = float(o.get("cost_usd") or 0)
        df["duty"] = float(o.get("duty") or 0)
        df["vat"] = float(o.get("vat") or 0)
        k = UNIT_K[kind]
        conv = df["usdcny"] * k * (1 + df["vat"])
        mult = conv * (1 + df["duty"])
        df["cn_spread"] = df["cn_main"] - df["cn_next"]
        df["intl_spread"] = df["intl"] - df["intl2"]
        df["cn_usd"] = df["cn_main"] / conv
        df["premium_usd"] = df["cn_usd"] - df["intl"]
        df["intl_cn"] = df["intl"] * conv
        df["import_cost_cn"] = (df["intl"] + df["cost_usd"]) * mult
        df["arb_cn"] = df["cn_main"] - df["import_cost_cn"]
        df["net_usd"] = df["arb_cn"] / mult
        df["date"] = df["date"].dt.date
        frames.append(df)

    if not frames:
        return existing if existing is not None else pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = _freeze(df, existing, cfg, today, recompute_all, "inst")
    return df.sort_values(["inst", "date"]).reset_index(drop=True)


OTHER_COLUMNS = [
    "date", "inst", "kind",
    "cn_main", "cn_main_contract", "cn_next", "cn_next_contract", "cn_spread",
    "intl", "intl2", "intl_spread", "usdcny", "cost_usd", "duty", "vat",
    "cn_usd", "premium_usd", "intl_cn", "import_cost_cn", "arb_cn", "net_usd",
]


def tidy_others(df: pd.DataFrame) -> pd.DataFrame:
    for c in OTHER_COLUMNS:
        if c not in df:
            df[c] = np.nan
    df = df[OTHER_COLUMNS].copy()
    for c in df.columns:
        if c in ("usdcny", "duty", "vat"):
            df[c] = df[c].astype(float).round(4)
        elif df[c].dtype.kind == "f":
            df[c] = df[c].round(3)
    return df


def _derive(df: pd.DataFrame) -> pd.DataFrame:
    """Recalculate every derived column from the stored inputs (prices, FX, assumptions)."""
    mult = df["usdcny"] * (1 + df["duty"]) * (1 + df["vat"])
    df["lme_c3m"] = df["lme_cash"] - df["lme_3m"]
    df["shfe_m1_m2"] = df["shfe_m1"] - df["shfe_m2"]
    df["shfe_m1_m3"] = df["shfe_m1"] - df["shfe_m3"]

    df["import_cost_spot"] = (df["lme_cash"] + df["premium_usd"]) * mult + df["misc_cny"]
    df["arb_spot"] = df["shfe_m1"] - df["import_cost_spot"]
    df["ratio_spot"] = df["shfe_m1"] / df["lme_cash"]
    df["be_ratio_spot"] = df["import_cost_spot"] / df["lme_cash"]
    df["be_premium_spot"] = (df["shfe_m1"] - df["misc_cny"]) / mult - df["lme_cash"]

    df["import_cost_3m"] = (df["lme_3m"] + df["premium_usd"]) * mult + df["misc_cny"]
    df["arb_3m"] = df["shfe_m3"] - df["import_cost_3m"]
    df["ratio_3m"] = df["shfe_m3"] / df["lme_3m"]
    df["be_ratio_3m"] = df["import_cost_3m"] / df["lme_3m"]

    # SHFE 1st month converted to USD, VAT taken out, minus LME cash: the raw cross-market spread
    df["shfe_usd_exvat"] = df["shfe_m1"] / (1 + df["vat"]) / df["usdcny"]
    df["shfe_lme_spread_usd"] = df["shfe_usd_exvat"] - df["lme_cash"]
    return df


COLUMNS = [
    "date", "metal",
    "lme_cash", "lme_3m", "lme_c3m", "lme_stock",
    "shfe_m1", "shfe_m2", "shfe_m3", "shfe_m1_contract", "shfe_m2_contract", "shfe_m3_contract",
    "shfe_main", "shfe_main_contract", "shfe_m1_m2", "shfe_m1_m3",
    "usdcny", "premium_usd", "duty", "vat", "misc_cny",
    "import_cost_spot", "arb_spot", "ratio_spot", "be_ratio_spot", "be_premium_spot",
    "import_cost_3m", "arb_3m", "ratio_3m", "be_ratio_3m",
    "shfe_usd_exvat", "shfe_lme_spread_usd",
]


def tidy(df: pd.DataFrame) -> pd.DataFrame:
    for c in COLUMNS:
        if c not in df:
            df[c] = np.nan
    df = df[COLUMNS].copy()
    four_dp = ["usdcny", "ratio_spot", "be_ratio_spot", "ratio_3m", "be_ratio_3m", "duty", "vat"]
    for c in df.columns:
        if c in four_dp:
            df[c] = df[c].astype(float).round(4)
        elif df[c].dtype.kind == "f":
            df[c] = df[c].round(2)
    return df
