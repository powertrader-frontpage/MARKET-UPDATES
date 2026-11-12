# Market Intelligence Terminal

A Streamlit-based market dashboard and NSE 500 stock-screening workspace. The project combines broker-connected market data, public-source fallbacks, technical analysis, sector leadership and breakout research.

## Data sources

- **FYERS:** Broker-connected index/live quote data and the primary source for the NSE 500 screener when valid credentials are configured.
- **Yahoo Finance:** Public historical-data fallback where FYERS data is unavailable or fails validation. Data may be delayed, incomplete or unavailable.
- **OilPrice.com:** Brent crude quote source used by the dashboard.
- **NSE:** Official market reports and FII/DII data where available through the dashboard's source integrations.

FYERS credentials belong in Streamlit Community Cloud Secrets. Never commit API keys, app secrets or access tokens to this repository.

## Main modules

- `app.py` — Market Intelligence Terminal dashboard and navigation.
- `nse500_screener.py` — NSE 500 technical screening, Weinstein stage analysis, base progression and daily breakout watch.
- `explosive_breakout_scanner.py` — Explosive breakout research scanner.
- `historical_breakout_postmortem.py` — Historical breakout review.
- `b1_fundamental_screener.py` — B1 fundamental screening tab.
- `data/weekly_rotation_history.json` — Weekly sector-rotation history used by the dashboard.
- `scripts/capture_dashboard.py` — Captures a dashboard screenshot on NSE trading days when run by the scheduled GitHub Actions workflow.

## NSE 500 screener notes

The screener uses completed weekly bars for its Weinstein stage analysis and completed daily bars for its daily breakout checks. Its base-counting logic and screening thresholds are part of the existing strategy; change them only after reviewing and testing the impact.

FYERS data is validated against the public historical series before use. If credentials are absent or data does not pass validation, the screener can fall back to Yahoo Finance. A fallback source can have different update timing, so check the source and last-updated information shown in the app.

## Run locally

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## Deploy with Streamlit Community Cloud

1. Connect this GitHub repository to Streamlit Community Cloud.
2. Select the intended branch and set the main file path to `app.py`.
3. Add the required FYERS credentials in the app's Secrets settings.
4. Deploy and verify the source timestamps and connection status in the dashboard.

The repository also retains a `render.yaml` configuration for a possible alternative Render deployment. It is not required for the current Streamlit Community Cloud deployment.

## GitHub Actions

- **Python syntax check:** Compiles the dashboard and scanner modules on pushes to the configured development branches and on pull requests targeting `main`.
- **Morning dashboard screenshot:** Runs at 07:00 IST, Monday to Friday, checks the NSE trading calendar, and uploads a screenshot artifact when capture succeeds. It does not publish to social media.

## Data limitations

Market-data availability, timestamps and delays depend on the upstream provider and instrument. A successful app refresh does not guarantee that every source has published a new market bar. Review the displayed source date and status before making trading decisions.

This project is a research and decision-support tool, not investment advice or an execution system. Confirm prices, corporate actions, exchange status and broker data independently before placing trades.
