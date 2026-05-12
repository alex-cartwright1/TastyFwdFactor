# Calendar Spread Edge Screener

**Website:** [tastyfwdfactor.cch-uk.tech](https://tastyfwdfactor.cch-uk.tech)

A Tkinter GUI that scans a watchlist for calendar spread setups, and ranks them by forward factor. Also allows you to analyse individual trades with a real-time P/L chart, and track open positions with live P/L.

**Data sources:** Tastytrade API using DXLINK (option chains + quotes) and yfinance (earnings dates, market cap, dividend dates).

---

## Requirements

- Python 3.9+
- A [Tastytrade](https://tastytrade.com) account with OAuth credentials (Client Secret + Refresh Token — see [Getting credentials](#getting-tastytrade-credentials) below)

---

## Installation

### 1. Clone the repo

```bash
git clone https://github.com/alex-cartwright1/TastyFwdFactor.git
cd TastyFwdFactor
```

### 2. Install Python dependencies

```bash
pip install requests scipy numpy websocket-client pandas yfinance matplotlib keyring
```

`tkinter` ships with Python on Windows and macOS. On Linux you may need:

```bash
# Debian / Ubuntu
sudo apt install python3-tk

# Fedora / RHEL
sudo dnf install python3-tkinter
```

**Optional but recommended:**

| Package | Purpose |
|---|---|
| `matplotlib` | Interactive P/L chart — disabled gracefully if missing |
| `keyring` | Stores credentials in OS secret store instead of a JSON file |

> **Linux / WSL note:** `keyring` requires a running secret service (GNOME Keyring, KWallet). Without one the app falls back to `~/.config/calendar-spread/credentials.json` (mode 600) automatically. To get keyring working on WSL2, install `keyrings.cryptfile` as an alternative: `pip install keyrings.cryptfile`.

### 3. Prepare a watchlist CSV

The app expects a CSV file with a `TICKER` header in the first column. I have included a list of tickers with options traded on the CBOE (around ~5000) called full.csv. By default the script will use these, however feel free to use any list you want, provided it is in the below format:  

Example:

```
TICKER
AAPL
MSFT
NVDA
```

---

## Getting Tastytrade credentials

The app authenticates with the Tastytrade **production** API using OAuth long-lived tokens — no username/password is stored.

1. Log in to your Tastytrade account and open the **API** section of your account settings (using the web interface).
2. Create an OAuth application to obtain a **Client Secret**.
3. Create a grant for the OAuth application to obtain a **Refresh Token** for your account, make sure to only give the grant **read** access. 

Both values are entered in the app's sidebar when you launch it. They are saved to the OS keyring (or `~/.config/calendar-spread/credentials.json` if no keyring backend is available) and pre-filled on future launches.

---

## Running the app

```bash
python main.py
```

1. Enter your **Client Secret** and **Refresh Token** in the left sidebar.
2. Click **Filters & Scan Settings…** to configure your watchlist path, target DTEs, IV method, and any filters you want.
3. Click **Run Scanner**.

---

## Scan settings reference

Open **Filters & Scan Settings…** from the sidebar to configure the following.

### Scan parameters

| Setting | Default | Description |
|---|---|---|
| Watchlist CSV | `full.csv` | Path to your ticker list |
| Target Front DTE | 21 | Target days-to-expiry for the short leg |
| Target Back DTE | 45 | Target days-to-expiry for the long leg |
| DTE flex (± days) | 0 | `0` = pick the nearest available expiry; `>0` = restrict to target ± flex |
| IV Method | Midpoint | See [IV methods](#iv-methods) below |

### Filters

| Filter | Default | Description |
|---|---|---|
| Min Price ($) | 10.00 | Exclude stocks trading below this price |
| Min Market Cap ($B) | *(blank)* | Exclude tickers below this market cap in billions; blank = no filter |
| Exclude earnings before front-leg expiry | On | Remove tickers with a known earnings date inside the front leg |
| Exclude earnings before back-leg expiry | On | Remove tickers with a known earnings date inside the back leg |
| Exclude ex-dividend before front-leg expiry | Off | Remove tickers with an ex-dividend date inside the front leg |
| Exclude ex-dividend before back-leg expiry | Off | Remove tickers with an ex-dividend date inside the back leg |
| Exclude tickers with no recorded earnings date | Off | Removes ETFs and leveraged funds that never report earnings |

### Ticker info cache

Earnings dates, market caps, and dividend dates are fetched from yfinance and cached in `~/.config/calendar-spread/ticker_info.json` to avoid re-fetching on every scan.

| Setting | Default | Description |
|---|---|---|
| Refresh after (days) | 7 | Re-fetch data older than this many days; `0` = always re-fetch |
| Refresh ticker data now | — | Clears the cache immediately; the next scan will re-fetch everything |

---

## IV methods

| Method | Description |
|---|---|
| **Midpoint** | Solves Black-Scholes IV using `(bid + ask) / 2` for both legs |
| **Bid Front / Ask Back** | Uses the front-leg bid and back-leg ask — worst-case fill cost |
| **Provided Data** | Reserved; currently falls back to Midpoint (DXLink does not stream IV directly) |

---

## Understanding the results

Results are sorted by **Fwd Factor** (descending). Higher values indicate that the front leg's implied volatility is elevated relative to the term structure — the core edge in a calendar spread.

### Columns

| Column | Description |
|---|---|
| Ticker | Symbol |
| Price | Last trade price |
| Mkt Cap | Market capitalisation |
| Strike | ATM strike used |
| F-DTE / B-DTE | Actual days to expiry for front / back leg |
| F-Bid, F-Ask | Front-leg option quotes |
| B-Bid, B-Ask | Back-leg option quotes |
| Front IV / Back IV | Implied volatility for each leg |
| Fwd IV | Implied forward volatility between the two expirations |
| **Fwd Factor** | `(Front IV − Fwd IV) / Fwd IV` — higher is more interesting |
| Debit | Midpoint net debit to open: `(B-mid) − (F-mid)` |
| F-Spread / B-Spread | Bid-ask spread width for each leg |
| Earnings | Next earnings date (from yfinance) |

### Fwd Factor formula

```
fwd_iv     = sqrt( (t2 × back_iv² − t1 × front_iv²) / (t2 − t1) )
fwd_factor = (front_iv − fwd_iv) / fwd_iv
```

A positive Fwd Factor means the front leg carries higher implied vol than the forward vol implied by the back leg — the calendar spread is buying cheap back-leg vol relative to what the front leg is pricing in.

---

## Trade analysis panel

Select any row in the results table to populate the **Analyse Selected Trade** panel below it.

1. Enter the actual **Back Leg Paid** and **Front Leg Credit** from your fill.
2. The **Net Debit** field updates automatically; you can also edit it directly (use the radio buttons to choose which leg stays fixed).
3. Click **Calculate & chart** to see the real Fwd Factor and component IVs for your specific fill, plus an interactive P/L chart at front-leg expiration with a back-leg IV slider.

---

## Position tracker

The **Positions** tab lets you track open calendar spread positions with live P/L.

### Adding a position

- Click **Add Position** in the Positions tab, or select a row in the scan results and click **Add to Positions** to pre-populate the legs from that scan result.
- In the dialog, enter the ticker, strike, front and back expiry dates, and the prices you actually paid/received for each leg.

### Live P/L

Once positions are loaded, live quotes stream through the same DXLink connection used by the scanner. P/L is updated in real time as bid/ask prices change, and cells flash on quote updates.

Positions are saved automatically to `~/.config/calendar-spread/positions.json` and restored on the next launch.

---

## File locations

| Path | Contents |
|---|---|
| `debug.log` | Full debug log for the current session (overwritten on each launch) |
| `~/.config/calendar-spread/settings.json` | Scan settings and filters |
| `~/.config/calendar-spread/ticker_info.json` | Earnings / market cap / dividend cache |
| `~/.config/calendar-spread/positions.json` | Saved position tracker entries |
| `~/.config/calendar-spread/credentials.json` | OAuth credentials (only if OS keyring is unavailable; mode 600) |

---

## Troubleshooting

**Credentials warning in red**
The `keyring` package is installed but no compatible secret store was found (common on WSL2 or headless Linux). Credentials fall back to `~/.config/calendar-spread/credentials.json` with restricted permissions. Install `keyrings.cryptfile` for an encrypted file-based alternative: `pip install keyrings.cryptfile`.

**No quotes returned / DXLink timeout**
DXLink quote tokens expire. The app fetches a fresh token before each scan. If the scan stalls at Phase 2, check the Debug Log tab for `AUTH_STATE` errors and verify your refresh token is still valid.

**Empty results after scanning**
- Check that your watchlist CSV has a `TICKER` header and one symbol per line.
- Relax the earnings / market cap / price filters in **Filters & Scan Settings…**.
- Open the **Debug Log** tab to see per-ticker rejection reasons.

**`No module named 'tkinter'`**
Install the system `python3-tk` package (see [Installation](#installation) above).
