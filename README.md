# Bitcoin Paper Bot V3 — Railway cloud edition

**This app runs a simulation only.** It never makes real exchange orders, doesn't accept trading/API keys, and cannot earn real money. It uses the original V2 strategy rules, not an AI price predictor.

The previous Windows V3 build only ran while your PC was on. This cloud edition keeps the Python paper-trading loop running on a compatible long-running hosting plan, saves the virtual state to a persistent disk, and serves a **password-protected web dashboard**.

## Deploy to Railway (recommended, paid always-on)

*Current in November 2026:* Railway Hobby begins at **US$5 per month with US$5 included usage**; usage-based costs can be higher. The Free tier has only US$1 monthly usage and may pause or stop — don't depend on it for continuous monitoring. Consult [Railway's current pricing](https://railway.com/pricing) before paying.

1. Extract the ZIP file to a folder on your PC. It contains `bot.py`, `legacy_v1.py`, `v2_engine.py`, `cloud_app.py`, `Dockerfile`, `railway.json`, and this README. The Docker setup uses only Python's standard library.
2. On [GitHub](https://github.com/new), create a **private repository**. Upload the extracted project files (NOT the ZIP itself) into the **repository root**. You can use GitHub's **Add file → Upload files**. Keep `Dockerfile` and `railway.json` at the repo root, not inside another directory. Don't upload your local `results/` folder or personal secrets.
3. In [Railway](https://railway.com/), start a new project, select **Deploy from GitHub repo**, and connect that repository. Railway will detect the Dockerfile. (A new unverified trial may restrict outgoing calls to Kraken; if you see data errors, check outbound network limits.)
4. **Before trusting the deployment:** Railway service **Variables → New Variable**: set `DASHBOARD_PASSWORD` to a long, random, **private** password at least 12 characters long. Do **not** paste it into ChatGPT or GitHub. Service will refuse to start without this variable.
5. Attach a **persistent volume** to this service, with mount path **`/data`** (on Railway project canvas, right-click service → Attach Volume; enter mount path). **This is essential:** the trading state and equity history would otherwise reset when Railway redeploys the container. The Dockerfile already configures `BOT_RESULTS_DIR=/data`. Do not mount `/app`.
6. Choose an **always-on** plan and keep **one replica** for this service. `railway.json` requests an `ALWAYS` restart policy (available on paid plans). Don't turn on Railway serverless sleeping or cron on this service.
7. Deploy. Railway service **Settings → Networking → Generate Domain**. Visit the HTTPS `*.up.railway.app` domain. Your browser will ask for a username/password: **username `viewer`**, password is the `DASHBOARD_PASSWORD` you entered in Railway variables. Never visit the raw HTTP version.
8. Check Railway **Logs**: expect `PAPER MODE`, `Cloud dashboard server listening`, and repeated `BTC $... | Equity $... | simulated events ...`. If the dashboard says *Data request failed*, Kraken did not respond properly; no simulated fill is produced for that failed poll.
9. Stop the old PC-based V2/V3 `03_paper_live.bat` if you only want one bot. **The new cloud bot starts its own fresh $1,000 virtual account**; it doesn't migrate your Windows paper-trading history or positions.

## Dashboard and files

- Visit the generated HTTPS domain on PC or phone from anywhere; it requires HTTP Basic authentication. The main page refreshes every 30 seconds and explains why the strategy is waiting.
- `/health` is publicly accessible for Railway's initial liveness check, but this says only the webserver is running; **not** that Kraken market data is arriving or that the strategy is profitable.
- `paper_state_v3.json` and `paper_equity_v3.csv` live on the `/data` volume. They are deliberately NOT served over HTTP.
- The dashboard can show zero trade events for weeks; the 200-hour trend filter blocks many trades. A bot making no trades is not a profitable bot.
- `comparison.html` is **not** automatically generated on Railway. Run `02_download_history_and_test.bat` locally in the original Windows V3 to test longer history, or run the `history` command in a safe Python environment that has Kraken/Coinbase public access.

## Technical details

- Docker image uses Python 3.12 with **no external packages**. Source files preserve V2 rules.
- Only one trading loop runs per container. The `run_paper()` loop polls Kraken roughly once a minute; the decision engine only enters on closed-candle signals and may exit using observed live price. The simulator does **not** place orders.
- HTTPS is supplied by Railway's public domain, while the app itself speaks HTTP inside Railway. The Basic authentication prompt is safe only through the **HTTPS** domain. The long password is never stored in source files or served in HTML.
- Do not deploy multiple replicas: separate processes writing a single state file could corrupt virtual results or duplicate events.
- Files are kept on the mounted volume when the container restarts; backups, billing status and deletions are still your responsibility. Cloud hosting is **not** guaranteed continuous operation.
- Market/API outages are displayed as data errors. The dashboard's `/health` endpoint does not continuously attest that the strategy loop is progressing.
- No financial guarantees; backtests, paper fills, slippage and assumed fees are not investment performance.

## Test locally without any real trades

```bash
python -m unittest discover -s tests -v
```

To run the cloud web server on your PC for a smoke test:

```bash
# Linux/macOS shell example — use a local-only network; local HTTP is not encrypted
export BOT_RESULTS_DIR="$PWD/results"
export DASHBOARD_PASSWORD="choose-a-long-unique-password"
export PORT=8080
python cloud_app.py
```

This mode is intended to deploy behind Railway HTTPS. Running it directly on a public server without TLS exposes your Basic Auth password.
