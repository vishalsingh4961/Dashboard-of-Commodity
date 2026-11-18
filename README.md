# LME–SHFE arbitrage & spread dashboard (Cu, Al, Zn + gold, silver, crude)

A GitHub Actions job runs every weekday evening (Dubai time). It downloads LME and
SHFE prices and USD/CNY, calculates the import arbitrage and spreads, and publishes
a dashboard page on GitHub Pages that you can open on any device.

**What it shows for copper, aluminium and zinc**

- Import P/L (CNY/t): spot (SHFE 1st month vs LME cash) and 3M (SHFE 3rd month vs LME 3M), with a window open / near / closed status
- The SHFE/LME price ratio against the breakeven ratio
- The breakeven CIF premium (the most an importer could pay) against your premium
- The LME cash–3M spread, the SHFE 1st–2nd and 1st–3rd month spreads, and LME stocks
- **Gold:** Shanghai premium (SHFE most-active, CNY/g) over the LBMA gold price in USD/oz, plus import P/L and SHFE calendar spread
- **Silver:** Shanghai premium (SHFE most-active ex-VAT, CNY/kg) over COMEX silver, plus import P/L and calendar spread
- **Crude:** INE SC (most-active, converted to USD/bbl) minus Brent, Brent–WTI, and the SC calendar spread
- History charts (1M / 3M / 6M / 1Y / All), a data table, and every input used each day

## Formulas

```
Import cost  = (LME + CIF premium) × USD/CNY × (1 + duty) × (1 + VAT) + misc costs
Import P/L   = SHFE − import cost                    (> 0 → import window open)
Ratio        = SHFE ÷ LME            Breakeven ratio = import cost ÷ LME
B/E premium  = (SHFE − misc) ÷ (USD/CNY × (1+duty) × (1+VAT)) − LME
Spreads      = near − far            (positive = backwardation)
```

SHFE prices are settlement prices in CNY/t and include 13% VAT. "1st month" is the
contract for next calendar month (连一) and "3rd month" is three months out (连三).

Gold, silver and crude use the **most-active** contract (highest open interest), because
liquidity there isn't in the nearby month:

```
Shanghai premium = China price ÷ (USD/CNY × units × (1 + VAT)) − international      [USD/oz or USD/bbl]
Import P/L       = China price − (international + your cost) × USD/CNY × units × (1+duty) × (1+VAT)
Main − next      = most-active minus the next most-active later contract
units: gold 1/31.1035 (oz → g), silver 32.1507 (oz → kg), crude 1
```

Default VAT: silver 13% (SHFE silver includes VAT), gold 0% (SHFE gold trades VAT-free),
crude 0% (INE SC is quoted net of duty and VAT). All are editable in `config.yaml` → `others`.

## Setup (about 10 minutes, one time)

1. **Create a repository** on GitHub, e.g. `lme-shfe-dashboard`. See the note on
   privacy below before choosing public or private.
2. **Upload the files.** Unzip, then on the repo page click *Add file → Upload files*
   and drag in everything.
   *Mac/iPad note:* the `.github` folder is hidden and often gets skipped. If you don't see
   `.github/workflows/daily.yml` in the repo afterwards, click *Add file → Create new file*,
   type `.github/workflows/daily.yml` as the name, paste the file's contents and commit.
3. **Let the workflow push.** *Settings → Actions → General → Workflow permissions →*
   select **Read and write permissions** → Save.
4. **Turn on Pages.** *Settings → Pages → Build and deployment → Source: Deploy from a
   branch → Branch: `main`, folder `/docs`* → Save.
5. **Run it once.** *Actions → Daily LME-SHFE update → Run workflow.* The first run
   downloads about 6 months of history (a few minutes). Your dashboard will be at
   `https://<your-username>.github.io/<repo-name>/`.
6. **Enter your assumptions** in `config.yaml` (pencil icon on GitHub): premium per
   metal, `premium_as_of`, the duty rate for your origin, misc costs, and under `others`
   your all-in import cost for gold and silver (`cost_usd`, USD/oz). Then set
   `assumptions_confirmed: true`. Saving the file triggers a rebuild.

After that it runs by itself at **18:30 and 22:00 Dubai, Monday–Friday**. To change
the times, edit the `cron` lines in `.github/workflows/daily.yml` (they're in UTC; Dubai = UTC+4).

## Adding or removing an instrument

Gold, silver and crude are entries under `others:` in `config.yaml`. Delete an entry to drop
it. To change the international benchmark, edit its `intl` list, e.g. use COMEX gold instead
of LBMA with `intl: [{yahoo: GC=F}, {sina: GC}]`.

## Updating premiums

Edit `config.yaml` and change `premium_usd` and `premium_as_of`. Each run recalculates the
last 7 days (`recompute_window_days`); older days keep the premium that was used then.
To recalculate *all* history with the current values: *Actions → Run workflow →* tick
**Recalculate ALL history**.

## Data sources

| Data | Primary | Fallback |
|---|---|---|
| LME cash, 3M, stocks | Westmetall (official LME prices) | Sina LME 3M close (no cash price) |
| SHFE settlement prices (Cu, Al, Zn, Au, Ag) | shfe.com.cn daily data file | Sina per-contract daily data |
| INE SC crude settlement prices | ine.cn daily data file | Sina per-contract daily data |
| Gold, USD/oz | LBMA gold price via Westmetall | Yahoo GC=F (COMEX), then Sina |
| Silver, USD/oz | COMEX front month via Yahoo SI=F | Sina |
| Brent / WTI, USD/bbl | Yahoo BZ=F / CL=F (front month) | Sina |
| USD/CNY | ECB reference rate via frankfurter.dev | open.er-api.com (latest only) |

Timing: China prices are Shanghai settlement prices; the LBMA gold price is set in London, and
COMEX silver, Brent and WTI are New York closes from the same day. Part of each day's premium
change is just that time gap. Front-month futures also carry some contango compared with spot.

Each run's source status is shown at the bottom of the dashboard. If a source fails,
the page shows a red banner and keeps the last good data. Premiums aren't downloaded:
SMM/Fastmarkets premiums are paywalled, so you enter them in `config.yaml`.

Fallbacks are best-effort. These are public web pages, not paid feeds: if a site changes
its layout or blocks GitHub's servers, that source stops until the parser is updated.

## Privacy and licensing

- On a **public** repo, the dashboard URL is public too (unlisted, but anyone with the
  link can open it). Pages on a **private** repo needs a paid GitHub plan.
- LME prices are licensed by the LME. Keep the dashboard for your own use and don't
  share it on.

## Run locally (optional)

```bash
pip install -r requirements.txt
python -m arbdash.update              # fetch + calculate + build docs/index.html
python -m arbdash.update --render-only
python -m arbdash.update --recompute
python -m pytest -q                   # offline tests with synthetic data
```

## Files

```
config.yaml                 your premiums, duty, costs, settings
arbdash/sources.py          downloaders (LME, SHFE/INE, gold/silver/crude, FX) with fallbacks
arbdash/calc.py             arbitrage and spread maths
arbdash/render.py           builds docs/index.html
arbdash/template.html       dashboard page (Chart.js, light/dark)
arbdash/update.py           daily entry point
data/*.csv                  raw prices (lme, shfe, intl, fx) + calculated metrics
                            (metrics.csv = base metals, others.csv = gold/silver/crude)
docs/index.html             the published dashboard
docs/assets/chart.umd.min.js  Chart.js 4.6.1 (MIT), bundled so the page has no CDN dependency
.github/workflows/daily.yml schedule
tests/test_pipeline.py      offline tests
```

## Troubleshooting

- **Workflow fails at "Commit"** → step 3 (Read and write permissions) isn't set.
- **Page shows 404** → wait 1–2 minutes after the first run, and check step 4.
- **Schedule stopped** → GitHub pauses scheduled workflows in repos with no activity for 60 days.
  Re-enable it in the Actions tab.
- **Numbers look off** → check the *Your assumptions* table on the page. Duty must be a
  decimal (`0.02`, not `2`).

Indicative analytics only, not trading advice.
