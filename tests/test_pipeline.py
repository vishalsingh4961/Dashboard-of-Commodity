"""Offline tests: parsers, formulas and a full run with synthetic data.

Run:  python -m pytest -q      (or: python tests/test_pipeline.py)
No network needed — every fetcher is replaced with fake data.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from arbdash import calc, sources, update  # noqa: E402


# ---------------------------------------------------------------- parsers
WESTMETALL_HTML = """
<html><body>
<table>
<tr><th>date</th><th>LME Aluminium Cash-Settlement</th><th>LME Aluminium 3-month</th><th>LME Aluminium stock</th></tr>
<tr><td>16. November 2026</td><td>3,332.00</td><td>3,020.00</td><td>221,900</td></tr>
<tr><td>13. November 2026</td><td>3,150.50</td><td>3,018.00</td><td>224,150</td></tr>
<tr><th>date</th><th>LME Aluminium Cash-Settlement</th><th>LME Aluminium 3-month</th><th>LME Aluminium stock</th></tr>
<tr><td>30. October 2026</td><td>3,114.00</td><td>3,080.00</td><td>-</td></tr>
</table>
</body></html>
"""


def test_parse_westmetall():
    df = sources._parse_westmetall(WESTMETALL_HTML)
    assert list(df["date"]) == [dt.date(2026, 10, 30), dt.date(2026, 11, 13), dt.date(2026, 11, 16)]
    r = df[df["date"] == dt.date(2026, 11, 16)].iloc[0]
    assert r["cash"] == 3332.0 and r["three_m"] == 3020.0 and r["stock"] == 221900
    assert math.isnan(df.iloc[0]["stock"])


def test_parse_westmetall_single_column():
    html = """<table><tr><th>date</th><th>Gold London Fixing</th></tr>
    <tr><td>16. November 2026</td><td>3,860.55</td></tr><tr><td>13. November 2026</td><td>3,898.15</td></tr></table>"""
    df = sources._parse_westmetall(html)
    assert list(df["cash"]) == [3898.15, 3860.55] and df["three_m"].isna().all()


def test_parse_shfe():
    payload = {"o_curinstrument": [
        {"PRODUCTID": "cu_f    ", "PRODUCTGROUPID": "cu", "DELIVERYMONTH": "2612", "SETTLEMENTPRICE": 99000, "CLOSEPRICE": 99100, "OPENINTEREST": 150000, "VOLUME": 80000},
        {"PRODUCTID": "cu_f    ", "PRODUCTGROUPID": "cu", "DELIVERYMONTH": "小计", "SETTLEMENTPRICE": ""},
        {"PRODUCTID": "cu_tas", "PRODUCTGROUPID": "cu", "DELIVERYMONTH": "2612", "SETTLEMENTPRICE": 1},
        {"PRODUCTID": "ad_f", "PRODUCTGROUPID": "ad", "DELIVERYMONTH": "2612", "SETTLEMENTPRICE": 20000},
        {"PRODUCTID": "al_f", "PRODUCTGROUPID": "al", "DELIVERYMONTH": "2701", "SETTLEMENTPRICE": "21,500", "CLOSEPRICE": "", "OPENINTEREST": "300000", "VOLUME": "1"},
    ]}
    rows = sources._shfe_rows(payload, dt.date(2026, 11, 17), {"cu", "al", "zn"})
    assert [(r["metal"], r["contract"]) for r in rows] == [("cu", "2612"), ("al", "2701")]
    assert rows[1]["settle"] == 21500.0 and math.isnan(rows[1]["close"])


# ---------------------------------------------------------------- formulas
CFG = {
    "vat": 0.13, "fx": {"manual_usdcny": None},
    "metals": {"cu": {"name": "Copper", "shfe_code": "cu", "premium_usd": 50, "duty": 0.02, "misc_cny": 150}},
    "settings": {"recompute_window_days": 7},
}


def test_formulas_by_hand():
    d = dt.date(2026, 11, 16)
    lme = pd.DataFrame([{"date": d, "metal": "cu", "cash": 10000.0, "three_m": 9900.0, "stock": 1000}])
    shfe = pd.DataFrame([
        {"date": d, "metal": "cu", "contract": c, "settle": s, "close": s, "open_interest": oi, "volume": 1}
        for c, s, oi in [("2611", 83000, 10), ("2612", 82000, 500), ("2701", 81800, 300), ("2702", 81500, 100)]
    ])
    fx = pd.DataFrame([{"date": dt.date(2026, 11, 14), "usdcny": 7.0}])  # Friday rate used for Monday (as-of)
    out = calc.compute(lme, shfe, fx, CFG, today=d)
    r = out.iloc[0]
    mult = 7.0 * 1.02 * 1.13
    cost = (10000 + 50) * mult + 150
    assert r["shfe_m1"] == 82000 and r["shfe_m2"] == 81800 and r["shfe_m3"] == 81500
    assert r["shfe_m1_contract"] == "cu2612" and r["shfe_main_contract"] == "cu2612"
    assert abs(r["import_cost_spot"] - cost) < 1e-6
    assert abs(r["arb_spot"] - (82000 - cost)) < 1e-6
    assert abs(r["ratio_spot"] - 8.2) < 1e-9
    assert abs(r["be_ratio_spot"] - cost / 10000) < 1e-9
    assert r["lme_c3m"] == 100 and r["shfe_m1_m2"] == 200 and r["shfe_m1_m3"] == 500
    # breakeven premium makes P/L exactly zero
    be = r["be_premium_spot"]
    assert abs(((10000 + be) * mult + 150) - 82000) < 1e-6
    # P/L in CNY equals (breakeven premium - premium) converted
    assert abs(r["arb_spot"] - (be - 50) * mult) < 1e-6
    cost3 = (9900 + 50) * mult + 150
    assert abs(r["arb_3m"] - (81500 - cost3)) < 1e-6


def test_term_structure_by_hand():
    d = dt.date(2026, 11, 16)
    cfg = json.loads(json.dumps(CFG)); cfg["metals"]["cu"]["rent_usd_day"] = 0.6; cfg["settings"]["prompt_days"] = 91
    lme = pd.DataFrame([{"date": d, "metal": "cu", "cash": 10000.0, "three_m": 10060.0, "stock": 1}])   # contango 60
    shfe = pd.DataFrame(columns=["date", "metal", "contract", "settle", "open_interest"])
    fx = pd.DataFrame([{"date": d, "usdcny": 7.0}])
    rates = pd.DataFrame([{"date": dt.date(2026, 11, 14), "sofr_pct": 3.6}])
    r = calc.compute(lme, shfe, fx, cfg, today=d, rates=rates).iloc[0]
    fc = 10000 * 0.036 * 91 / 360 + 0.6 * 91
    assert r["lme_c3m"] == -60 and abs(r["full_carry_usd"] - fc) < 1e-9
    assert abs(r["pct_full_carry"] - 60 / fc * 100) < 1e-9
    assert abs(r["lme_c3m_ann_pct"] - (-60 / 10000 * 360 / 91 * 100)) < 1e-9
    from arbdash import render
    df = pd.DataFrame({"date": [dt.date(2026, 11, i) for i in (10, 11, 12, 13, 16, 17)],
                       "lme_c3m": [-5.0, 3.0, 10.0, 12.0, 1.0, 20.0]})
    t = render.term_stats(df, band=2)
    assert t["state"] == "back" and t["streak"] == 1 and t["since"] == "2026-11-17"   # 1.0 is "flat" inside the band
    assert t["d1"] == 19.0 and t["w1"] == 25.0 and t["min"] == -5.0 and t["max"] == 20.0 and t["pctile"] == 100.0


def test_frozen_history():
    d_old, d_new = dt.date(2026, 10, 1), dt.date(2026, 11, 16)
    lme = pd.DataFrame([{"date": d, "metal": "cu", "cash": 10000.0, "three_m": 9900.0, "stock": 1} for d in (d_old, d_new)])
    shfe = pd.DataFrame([{"date": d, "metal": "cu", "contract": calc._month_code(d, 1), "settle": 82000, "close": 1, "open_interest": 1, "volume": 1} for d in (d_old, d_new)])
    fx = pd.DataFrame([{"date": d_old, "usdcny": 7.0}])
    first = calc.compute(lme, shfe, fx, CFG, today=d_new)
    cfg2 = json.loads(json.dumps(CFG)); cfg2["metals"]["cu"]["premium_usd"] = 200
    second = calc.compute(lme, shfe, fx, cfg2, existing=first, today=d_new)
    old = second[second["date"] == d_old].iloc[0]; new = second[second["date"] == d_new].iloc[0]
    assert old["premium_usd"] == 50 and new["premium_usd"] == 200
    third = calc.compute(lme, shfe, fx, cfg2, existing=first, recompute_all=True, today=d_new)
    assert (third["premium_usd"] == 200).all()


def test_others_by_hand():
    d = dt.date(2026, 11, 16)
    cfg = {"fx": {"manual_usdcny": None}, "settings": {"recompute_window_days": 7}, "others": {
        "au": {"name": "Gold", "kind": "gold", "code": "au", "cost_usd": 2, "duty": 0.0, "vat": 0.0},
        "ag": {"name": "Silver", "kind": "silver", "code": "ag", "cost_usd": 0.5, "duty": 0.01, "vat": 0.13},
        "sc": {"name": "Crude", "kind": "crude", "code": "sc", "cost_usd": 0, "duty": 0.0, "vat": 0.0},
    }}
    ex = pd.DataFrame([
        {"date": d, "metal": "au", "contract": "2612", "settle": 905.0, "open_interest": 100},
        {"date": d, "metal": "au", "contract": "2702", "settle": 910.0, "open_interest": 300},   # main
        {"date": d, "metal": "au", "contract": "2704", "settle": 914.0, "open_interest": 50},
        {"date": d, "metal": "au", "contract": "2706", "settle": 918.0, "open_interest": 80},    # next most-active later
        {"date": d, "metal": "ag", "contract": "2702", "settle": 13000.0, "open_interest": 500},
        {"date": d, "metal": "ag", "contract": "2703", "settle": 13050.0, "open_interest": 10},
        {"date": d, "metal": "sc", "contract": "2601", "settle": 470.0, "open_interest": 40},
        {"date": d, "metal": "sc", "contract": "2602", "settle": 468.0, "open_interest": 20},
    ])
    intl = pd.DataFrame([
        {"date": d, "series": "au", "price": 4000.0}, {"date": d, "series": "ag", "price": 50.0},
        {"date": d, "series": "sc", "price": 65.0}, {"date": d, "series": "sc_2", "price": 61.5},
    ])
    fx = pd.DataFrame([{"date": d, "usdcny": 7.0}])
    out = calc.compute_others(ex, intl, fx, cfg, today=d).set_index("inst")

    g = out.loc["au"]; conv = 7.0 / 31.1034768
    assert g["cn_main_contract"] == "au2702" and g["cn_next_contract"] == "au2706" and g["cn_spread"] == 910 - 918
    assert abs(g["premium_usd"] - (910 / conv - 4000)) < 1e-9
    assert abs(g["arb_cn"] - (910 - 4002 * conv)) < 1e-9
    assert abs(g["net_usd"] - (g["premium_usd"] - 2)) < 1e-9          # no duty/VAT: net = premium - cost

    s_ = out.loc["ag"]; k = 7.0 * 1000 / 31.1034768
    assert abs(s_["premium_usd"] - (13000 / 1.13 / k - 50)) < 1e-9
    assert abs(s_["arb_cn"] - (13000 - 50.5 * k * 1.01 * 1.13)) < 1e-9
    assert abs(s_["intl_cn"] - 50 * k * 1.13) < 1e-9

    c = out.loc["sc"]
    assert c["cn_main_contract"] == "sc2601" and c["cn_spread"] == 2.0
    assert abs(c["premium_usd"] - (470 / 7 - 65)) < 1e-9 and abs(c["intl_spread"] - 3.5) < 1e-9


# ---------------------------------------------------------------- full run
def _fake_market(today: dt.date, n_days: int = 200, seed: int = 7):
    rng = np.random.default_rng(seed)
    days = [today - dt.timedelta(days=i) for i in range(n_days)][::-1]
    days = [d for d in days if d.weekday() < 5]
    base = {"cu": (13600, 60, 40), "al": (3150, 20, 30), "zn": (3745, 15, 25)}
    lme_rows, shfe_rows, fx_rows, intl_rows = [], [], [], []
    fx = 7.10
    lvl = {k: v[0] * 0.92 for k, v in base.items()}
    for d in days:
        fx += rng.normal(0, 0.008)
        fx_rows.append({"date": d, "usdcny": round(fx, 4), "source": "ecb"})
        for k, (p0, c3, vol) in base.items():
            lvl[k] *= math.exp(rng.normal(0.0004, 0.011))
            cash = lvl[k]; three = cash - c3 + rng.normal(0, c3 / 3)
            lme_rows.append({"date": d, "metal": k, "cash": round(cash, 2), "three_m": round(three, 2), "stock": int(2e5 + rng.normal(0, 4e3)), "source": "westmetall"})
            china = cash * fx * 1.13 * (1 + rng.normal(0.004, 0.006))
            step = rng.normal(30 if k != "zn" else 60, 20)
            for j in range(0, 13):
                y, m = sources._add_months(d, j)
                shfe_rows.append({"date": d, "metal": k, "contract": f"{y % 100:02d}{m:02d}",
                                  "settle": round(china - j * step), "close": 0,
                                  "open_interest": 1e5 if j == 2 else 3e4, "volume": 1, "source": "shfe"})
        # precious metals and crude
        for k, p0, vol in (("au", 3900, 0.009), ("ag", 50, 0.016), ("sc", 65, 0.018)):
            lvl[k] = lvl.get(k, p0) * math.exp(rng.normal(0, vol))
            intl_rows.append({"date": d, "series": k, "price": round(lvl[k], 3), "source": "fake"})
            if k == "sc":
                intl_rows.append({"date": d, "series": "sc_2", "price": round(lvl[k] - 3.5 + rng.normal(0, 0.4), 3), "source": "fake"})
                cn = (lvl[k] + rng.normal(0.5, 1.2)) * fx
            elif k == "au":
                cn = (lvl[k] + rng.normal(12, 8)) * fx / 31.1034768
            else:
                cn = (lvl[k] + rng.normal(0.3, 0.4)) * fx * 1000 / 31.1034768 * 1.13
            for j in range(0, 13):
                y, m = sources._add_months(d, j)
                shfe_rows.append({"date": d, "metal": k, "contract": f"{y % 100:02d}{m:02d}",
                                  "settle": round(cn * (1 + 0.002 * j), 2), "close": 0,
                                  "open_interest": {2: 1e5, 4: 6e4}.get(j, 1e4), "volume": 1, "source": "fake"})
    return pd.DataFrame(lme_rows), pd.DataFrame(shfe_rows), pd.DataFrame(fx_rows), pd.DataFrame(intl_rows)


def run_demo(out_dir: Path, today: dt.date) -> Path:
    """Copy the repo to out_dir, swap fetchers for fake data and run one update."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    shutil.copytree(ROOT, out_dir, ignore=shutil.ignore_patterns("data", "__pycache__", ".git"))
    (out_dir / "data").mkdir()
    lme, shfe, fx, intl = _fake_market(today)

    def f_lme(field, start):
        code = {"LME_Cu_cash": "cu", "LME_Al_cash": "al", "LME_Zn_cash": "zn"}[field]
        df = lme[(lme["metal"] == code) & (lme["date"] >= start)].drop(columns=["metal"])
        return df.reset_index(drop=True)

    def f_shfe(days, codes, exchange="shfe"):
        return shfe[shfe["date"].isin(set(days)) & shfe["metal"].isin(codes)].reset_index(drop=True)

    def f_sofr(start, end):
        days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
        return pd.DataFrame([{"date": d, "sofr_pct": 3.6} for d in days if d.weekday() < 5]), "fake"

    def f_intl(spec, start):
        (kind, sym), = spec[0].items()
        series = {"USD_ozt_London": "au", "SI=F": "ag", "BZ=F": "sc", "CL=F": "sc_2"}[sym]
        df = intl[(intl["series"] == series) & (intl["date"] >= start)].drop(columns=["series"])
        return df.reset_index(drop=True), f"{kind} {sym}", []

    def f_fx(start, end):
        return fx[(fx["date"] >= start) & (fx["date"] <= end)].reset_index(drop=True)

    orig = (sources.fetch_lme_westmetall, sources.fetch_shfe_range, sources.fetch_fx_frankfurter, sources.fetch_intl, sources.fetch_sofr, update.ROOT, update.DATA, update.DOCS)
    try:
        sources.fetch_lme_westmetall, sources.fetch_shfe_range, sources.fetch_fx_frankfurter, sources.fetch_intl, sources.fetch_sofr = f_lme, f_shfe, f_fx, f_intl, f_sofr
        update.ROOT, update.DATA, update.DOCS = out_dir, out_dir / "data", out_dir / "docs"
        cfg_path = out_dir / "config.yaml"
        txt = cfg_path.read_text().replace("premium_usd: 0 ", "premium_usd: 60 ")
        txt = txt.replace("title: LME", "title: SAMPLE DATA (synthetic) · LME")
        cfg_path.write_text(txt)
        _orig_load = update.load_config
        update.load_config = lambda path=cfg_path: _orig_load(path)
        update.run(today=today)
        update.run(today=today)  # second run must be idempotent
        update.load_config = _orig_load
    finally:
        sources.fetch_lme_westmetall, sources.fetch_shfe_range, sources.fetch_fx_frankfurter, sources.fetch_intl, sources.fetch_sofr, update.ROOT, update.DATA, update.DOCS = orig
    return out_dir


def test_full_run():
    today = dt.date(2026, 11, 17)
    out = run_demo(Path(tempfile.mkdtemp()) / "repo", today)
    m = pd.read_csv(out / "data" / "metrics.csv")
    assert set(m["metal"]) == {"cu", "al", "zn"}
    assert m.duplicated(["date", "metal"]).sum() == 0
    latest = m[m["date"] == m["date"].max()]
    assert latest["arb_spot"].notna().all() and latest["shfe_m1_contract"].str.endswith("2612").all()
    assert latest["full_carry_usd"].notna().all() and latest["lme_c3m_ann_pct"].notna().all()
    o = pd.read_csv(out / "data" / "others.csv")
    assert set(o["inst"]) == {"au", "ag", "sc"} and o.duplicated(["date", "inst"]).sum() == 0
    lo = o[o["date"] == o["date"].max()].set_index("inst")
    assert lo["premium_usd"].notna().all() and lo.loc["sc", "intl_spread"] > 0
    html = (out / "docs" / "index.html").read_text()
    assert "/*__DATA__*/" not in html and '"metals"' in html and '"others"' in html and '"term": {' in html


def test_all_sources_down():
    """First run with every source failing must still produce a page (with error banners)."""
    out = Path(tempfile.mkdtemp()) / "repo"
    shutil.copytree(ROOT, out, ignore=shutil.ignore_patterns("data", "__pycache__", ".git"))
    (out / "data").mkdir()

    def boom(*a, **k):
        raise sources.SourceError("simulated outage")

    names = ["fetch_lme_westmetall", "fetch_lme_sina", "fetch_shfe_range", "fetch_shfe_sina", "fetch_fx_frankfurter", "fetch_fx_latest_er", "fetch_intl", "fetch_sofr"]
    saved = {n: getattr(sources, n) for n in names}
    saved_paths = (update.ROOT, update.DATA, update.DOCS, update.load_config)
    try:
        for n in names:
            setattr(sources, n, boom)
        update.ROOT, update.DATA, update.DOCS = out, out / "data", out / "docs"
        orig_load = saved_paths[3]
        update.load_config = lambda: orig_load(out / "config.yaml")
        update.run(today=dt.date(2026, 11, 17))
    finally:
        for n, f in saved.items():
            setattr(sources, n, f)
        update.ROOT, update.DATA, update.DOCS, update.load_config = saved_paths
    html = (out / "docs" / "index.html").read_text()
    assert "simulated outage" in html
    log = json.loads((out / "data" / "run_log.json").read_text())
    assert log["shfe"]["ok"] is False and log["fx"]["ok"] is False and log["ine"]["ok"] is False
    assert all(v["ok"] is False for v in log["intl"].values()) and log["rates"]["ok"] is False


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok ", name)
