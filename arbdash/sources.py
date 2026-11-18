"""Data fetchers for LME, SHFE/INE, international prices and USD/CNY.

Every fetcher returns a tidy pandas DataFrame and raises SourceError on failure,
so the caller can fall back to the next source and log what happened.

Primary / fallback sources
--------------------------
LME      : Westmetall official cash + 3M + stocks   -> Sina LME 3M close (no cash)
SHFE/INE : exchange daily data file (kxYYYYMMDD.dat) -> Sina per-contract daily K-lines
Gold     : LBMA gold price via Westmetall            -> Yahoo GC=F -> Sina GC
Silver   : COMEX front month via Yahoo SI=F          -> Sina SI
Brent/WTI: Yahoo BZ=F / CL=F                         -> Sina OIL / CL
FX       : ECB reference USD/CNY via frankfurter.dev -> open.er-api.com (latest only)
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import StringIO

import pandas as pd
import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TIMEOUT = 20
_T0 = time.time()


class SourceError(RuntimeError):
    pass


def log(msg: str) -> None:
    """Progress line for the GitHub Actions log."""
    print(f"[{time.time() - _T0:6.1f}s] {msg}", file=sys.stderr, flush=True)


def _get(url: str, *, params=None, headers=None, retries: int = 3, pause: float = 2.0) -> requests.Response:
    """GET with retries. Returns the response (status 200) or raises SourceError."""
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=h, timeout=(10, TIMEOUT))
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                raise SourceError(f"404 {url}")
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:  # network error, retry
            last = str(e)
        time.sleep(pause * (attempt + 1))
    raise SourceError(f"{url} failed: {last}")


def _num(x):
    """'13,610.00' -> 13610.0 ; '', '-', None -> NaN."""
    if x is None:
        return float("nan")
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace(",", "")
    if s in ("", "-", "--", "nan", "None"):
        return float("nan")
    try:
        return float(s)
    except ValueError:
        return float("nan")


# ---------------------------------------------------------------------------
# LME — Westmetall
# ---------------------------------------------------------------------------
WESTMETALL_URL = "https://www.westmetall.com/en/markdaten.php"


def _parse_westmetall(html: str) -> pd.DataFrame:
    tables = pd.read_html(StringIO(html), thousands=None, decimal=".")
    rows = []
    for t in tables:
        if t.shape[1] < 2:
            continue
        t = t.copy()
        t.columns = [str(c) for c in range(t.shape[1])]
        for _, r in t.iterrows():
            d = pd.to_datetime(str(r["0"]).strip(), format="%d. %B %Y", errors="coerce")
            if pd.isna(d):
                continue  # header / spacer rows
            cash = _num(r["1"])
            three = _num(r["2"]) if t.shape[1] > 2 else float("nan")
            stock = _num(r["3"]) if t.shape[1] > 3 else float("nan")
            if pd.isna(cash) and pd.isna(three):
                continue
            rows.append({"date": d.date(), "cash": cash, "three_m": three, "stock": stock})
    if not rows:
        raise SourceError("Westmetall: no price rows found (page layout may have changed)")
    df = pd.DataFrame(rows).drop_duplicates("date").sort_values("date")
    return df


def fetch_lme_westmetall(field: str, start: dt.date) -> pd.DataFrame:
    """Official LME cash settlement, 3-month and stock, from `start` to today."""
    years = sorted({start.year, dt.date.today().year})
    frames = []
    for y in range(years[0], years[-1] + 1):
        params = {"action": "table", "field": field}
        if y != dt.date.today().year:
            params["year"] = y
        r = _get(WESTMETALL_URL, params=params)
        frames.append(_parse_westmetall(r.text))
        time.sleep(1)
    df = pd.concat(frames).drop_duplicates("date").sort_values("date")
    df = df[df["date"] >= start]
    df["source"] = "westmetall"
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# LME — Sina fallback (3M electronic close only)
# ---------------------------------------------------------------------------
SINA_GLOBAL_URL = (
    "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_S{tag}=/"
    "GlobalFuturesService.getGlobalFuturesDailyKLine"
)


def fetch_lme_sina(symbol: str, start: dt.date) -> pd.DataFrame:
    tag = dt.date.today().strftime("%Y_%m_%d")
    r = _get(
        SINA_GLOBAL_URL.format(tag=tag),
        params={"symbol": symbol, "_": tag, "source": "web"},
        headers={"Referer": "https://finance.sina.com.cn/"},
    )
    txt = r.text
    i, j = txt.find("["), txt.rfind("]")
    if i < 0 or j < 0:
        raise SourceError(f"Sina LME {symbol}: unexpected response")
    data = json.loads(txt[i : j + 1])
    df = pd.DataFrame(data)
    if df.empty or "close" not in df:
        raise SourceError(f"Sina LME {symbol}: empty")
    df["date"] = pd.to_datetime(df["date"]).dt.date
    out = pd.DataFrame(
        {
            "date": df["date"],
            "cash": float("nan"),
            "three_m": df["close"].map(_num),
            "stock": float("nan"),
            "source": "sina-3m",
        }
    )
    return out[out["date"] >= start].reset_index(drop=True)


def fetch_sina_global(symbol: str, start: dt.date) -> pd.DataFrame:
    """Sina overseas futures daily close (e.g. GC, SI, OIL, CL). Returns date, price."""
    df = fetch_lme_sina(symbol, start)
    return pd.DataFrame({"date": df["date"], "price": df["three_m"], "source": f"sina:{symbol}"})


# ---------------------------------------------------------------------------
# International prices — Westmetall single-value tables (LBMA gold) and Yahoo
# ---------------------------------------------------------------------------
def fetch_westmetall_series(field: str, start: dt.date) -> pd.DataFrame:
    """A Westmetall table with one value column, e.g. USD_ozt_London (LBMA gold, USD/oz)."""
    df = fetch_lme_westmetall(field, start)
    return pd.DataFrame({"date": df["date"], "price": df["cash"], "source": f"westmetall:{field}"})


YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"


def fetch_yahoo(symbol: str, start: dt.date) -> pd.DataFrame:
    """Daily closes from Yahoo Finance's chart API (futures such as GC=F, SI=F, BZ=F, CL=F)."""
    p1 = int(dt.datetime.combine(start, dt.time()).replace(tzinfo=dt.timezone.utc).timestamp())
    p2 = int(dt.datetime.now(dt.timezone.utc).timestamp()) + 86400
    r = _get(YAHOO_URL.format(sym=symbol), params={"period1": p1, "period2": p2, "interval": "1d"})
    try:
        res = r.json()["chart"]["result"][0]
        ts = res["timestamp"]
        close = res["indicators"]["quote"][0]["close"]
        offset = int(res.get("meta", {}).get("gmtoffset", 0))
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise SourceError(f"Yahoo {symbol}: unexpected response ({e})")
    rows = [
        {"date": dt.datetime.fromtimestamp(t + offset, dt.timezone.utc).date(), "price": float(c)}
        for t, c in zip(ts, close)
        if c is not None
    ]
    if not rows:
        raise SourceError(f"Yahoo {symbol}: no data")
    df = pd.DataFrame(rows).drop_duplicates("date", keep="last")
    df["source"] = f"yahoo:{symbol}"
    return df[df["date"] >= start].reset_index(drop=True)


def fetch_intl(spec: list[dict], start: dt.date) -> tuple[pd.DataFrame, str, list[str]]:
    """Try each source in `spec` in order, e.g. [{"westmetall": "USD_ozt_London"}, {"yahoo": "GC=F"}].
    Returns (frame, source used, errors)."""
    errors = []
    for s in spec:
        (kind, sym), = s.items()
        try:
            if kind == "westmetall":
                return fetch_westmetall_series(sym, start), f"westmetall {sym}", errors
            if kind == "yahoo":
                return fetch_yahoo(sym, start), f"yahoo {sym}", errors
            if kind == "sina":
                return fetch_sina_global(sym, start), f"sina {sym}", errors
            errors.append(f"unknown source {kind}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{kind} {sym}: {e}")
    raise SourceError("; ".join(errors) or "no sources configured")


# ---------------------------------------------------------------------------
# SHFE / INE — official daily file (same JSON format on both exchanges)
# ---------------------------------------------------------------------------
EXCHANGE_URLS = {
    "shfe": ("https://www.shfe.com.cn/data/tradedata/future/dailydata/kx{d}.dat", "https://www.shfe.com.cn/"),
    "ine": ("https://www.ine.cn/data/tradedata/future/dailydata/kx{d}.dat", "https://www.ine.cn/"),
}
SHFE_DAILY_URL = EXCHANGE_URLS["shfe"][0]
SHFE_HEADERS = {"Referer": "https://www.shfe.com.cn/"}


def _shfe_rows(payload: dict, day: dt.date, codes: set[str]) -> list[dict]:
    rows = []
    for row in payload.get("o_curinstrument", []):
        month = str(row.get("DELIVERYMONTH", "")).strip()
        if not re.fullmatch(r"\d{4}", month):
            continue  # skips 小计 / 合计 subtotal rows
        pid = str(row.get("PRODUCTID", "")).strip().lower()
        group = str(row.get("PRODUCTGROUPID", "")).strip().lower()
        code = pid[:-2] if pid.endswith("_f") else (group if not pid else None)
        if code not in codes:
            continue  # other products, TAS / EFP lines
        rows.append(
            {
                "date": day,
                "metal": code,
                "contract": month,  # YYMM
                "settle": _num(row.get("SETTLEMENTPRICE")),
                "close": _num(row.get("CLOSEPRICE")),
                "open_interest": _num(row.get("OPENINTEREST")),
                "volume": _num(row.get("VOLUME")),
                "source": "exchange",
            }
        )
    return rows


def fetch_shfe_day(day: dt.date, codes: set[str], exchange: str = "shfe") -> pd.DataFrame:
    """All contracts for the given product codes on one trading day (exchange = shfe or ine).
    Returns an empty frame for non-trading days."""
    url, referer = EXCHANGE_URLS[exchange]
    try:
        r = _get(url.format(d=day.strftime("%Y%m%d")), headers={"Referer": referer}, retries=2)
    except SourceError as e:
        if str(e).startswith("404"):
            return pd.DataFrame()
        raise
    try:
        payload = json.loads(r.text)
    except json.JSONDecodeError as e:
        raise SourceError(f"{exchange.upper()} {day}: not JSON ({e})")
    df = pd.DataFrame(_shfe_rows(payload, day, codes))
    if not df.empty:
        df["source"] = exchange
    return df


def fetch_shfe_range(days: list[dt.date], codes: set[str], exchange: str = "shfe", workers: int = 8,
                     budget_s: float = 600) -> pd.DataFrame:
    """Fetch many trading days. Probes the most recent weekdays first so an unreachable
    exchange fails fast (and the caller can fall back), then downloads the rest in parallel."""
    weekdays = [d for d in days if d.weekday() < 5]
    if not weekdays:
        raise SourceError(f"{exchange.upper()}: no weekdays in range")
    frames, errors = [], []

    # 1) probe: newest weekdays, stop at the first success
    probe = weekdays[::-1][:4]
    ok = False
    for d in probe:
        try:
            df = fetch_shfe_day(d, codes, exchange)
            ok = True
            if not df.empty:
                frames.append(df)
            break
        except SourceError as e:
            errors.append(str(e))
    if not ok:
        raise SourceError(f"{exchange.upper()} unreachable: {errors[-1] if errors else 'no response'}")
    log(f"{exchange.upper()}: reachable, fetching {len(weekdays)} days with {workers} workers")

    # 2) the rest in parallel
    rest = [d for d in weekdays if d not in probe[: len(errors) + 1]]
    done, t0 = 0, time.time()
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futs = {pool.submit(fetch_shfe_day, d, codes, exchange): d for d in sorted(rest, reverse=True)}
        for f in as_completed(futs):
            done += 1
            if time.time() - t0 > budget_s:
                log(f"{exchange.upper()}: time budget reached after {done}/{len(rest)} days; the rest next run")
                break
            try:
                df = f.result()
                if not df.empty:
                    frames.append(df)
            except SourceError as e:
                errors.append(f"{futs[f]}: {e}")
            if done % 25 == 0:
                log(f"{exchange.upper()}: {done}/{len(rest)} days")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if not frames:
        raise SourceError(f"{exchange.upper()}: no data returned ({len(errors)} errors)")

    # 3) retry failed days slowly (the exchange sometimes resets parallel connections)
    failed = sorted({d for d in rest if any(e.startswith(f"{d}:") for e in errors)})
    still = []
    if failed:
        log(f"{exchange.upper()}: retrying {len(failed)} failed days one by one")
        for d in failed:
            time.sleep(1.0)
            try:
                df = fetch_shfe_day(d, codes, exchange)
                if not df.empty:
                    frames.append(df)
            except SourceError:
                still.append(d)
        if still:
            log(f"{exchange.upper()}: {len(still)} days still missing; they will be retried next run")
    out = pd.concat(frames, ignore_index=True)
    out.attrs["failed_days"] = [d.isoformat() for d in still]
    return out


# ---------------------------------------------------------------------------
# SHFE — Sina fallback (per-contract daily K-lines)
# ---------------------------------------------------------------------------
SINA_INNER_URL = (
    "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_{sym}{tag}=/"
    "InnerFuturesNewService.getDailyKLine"
)


def _add_months(d: dt.date, k: int) -> tuple[int, int]:
    m = d.month - 1 + k
    return d.year + m // 12, m % 12 + 1


def fetch_shfe_sina(codes: set[str], start: dt.date) -> pd.DataFrame:
    today = dt.date.today()
    tag = today.strftime("%Y_%m_%d")
    frames, attempts, failures, t0 = [], 0, 0, time.time()
    for code in sorted(codes):
        for k in range(0, 13):
            if time.time() - t0 > 300:
                log("Sina SHFE fallback: time budget reached")
                break
            if attempts >= 3 and failures == attempts:
                raise SourceError("Sina unreachable")
            attempts += 1
            y, m = _add_months(today, k)
            month = f"{y % 100:02d}{m:02d}"
            sym = f"{code.upper()}{month}"
            try:
                r = _get(
                    SINA_INNER_URL.format(sym=sym, tag=tag),
                    params={"symbol": sym, "type": tag},
                    headers={"Referer": "https://finance.sina.com.cn/"},
                    retries=1,
                )
            except SourceError:
                failures += 1
                continue
            txt = r.text
            i, j = txt.find("["), txt.rfind("]")
            if i < 0 or j < 0:
                continue
            try:
                data = json.loads(txt[i : j + 1])
            except json.JSONDecodeError:
                continue
            if not data:
                continue
            df = pd.DataFrame(data)
            df["date"] = pd.to_datetime(df["d"]).dt.date
            df = df[df["date"] >= start]
            frames.append(
                pd.DataFrame(
                    {
                        "date": df["date"],
                        "metal": code,
                        "contract": month,
                        "settle": df.get("s", pd.Series(dtype=float)).map(_num),
                        "close": df["c"].map(_num),
                        "open_interest": df.get("p", pd.Series(dtype=float)).map(_num),
                        "volume": df["v"].map(_num),
                        "source": "sina",
                    }
                )
            )
            time.sleep(0.4)
    if not frames:
        raise SourceError("Sina SHFE: no data returned")
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# FX — USD/CNY
# ---------------------------------------------------------------------------
FRANKFURTER = "https://api.frankfurter.dev/v1/{start}..{end}"
ER_API = "https://open.er-api.com/v6/latest/USD"


def fetch_fx_frankfurter(start: dt.date, end: dt.date) -> pd.DataFrame:
    """Daily ECB USD/CNY. Requested in 60-day chunks so long ranges stay daily."""
    rows = []
    a = start
    while a <= end:
        b = min(a + dt.timedelta(days=60), end)
        r = _get(FRANKFURTER.format(start=a.isoformat(), end=b.isoformat()), params={"base": "USD", "symbols": "CNY"})
        rates = r.json().get("rates", {})
        rows += [{"date": dt.date.fromisoformat(d), "usdcny": v["CNY"]} for d, v in rates.items() if "CNY" in v]
        a = b + dt.timedelta(days=1)
    if not rows:
        raise SourceError("frankfurter: no rates")
    df = pd.DataFrame(rows).drop_duplicates("date")
    df["source"] = "ecb"
    return df.sort_values("date").reset_index(drop=True)


def fetch_fx_latest_er() -> pd.DataFrame:
    r = _get(ER_API)
    js = r.json()
    rate = js.get("rates", {}).get("CNY")
    if not rate:
        raise SourceError("er-api: no CNY rate")
    return pd.DataFrame([{"date": dt.date.today(), "usdcny": float(rate), "source": "er-api"}])
