# Market Intelligence Terminal — Free Hybrid Edition

A Streamlit market dashboard that does **not require a broker API**. It combines embedded TradingView widgets with best-effort public historical market data from Yahoo Finance through `yfinance`. Calculated indicators use actual returned OHLC data only; missing values are shown as unavailable rather than fabricated.

## Included
- Embedded TradingView interactive charts, market overview, stock heatmap and market news widget
- Best-effort daily history for NIFTY 50, BANK NIFTY, India VIX, Brent crude, INFY, WIPRO and selected stocks
- SMA/EMA (5, 11, 20, 50, 100, 200), RSI (14), ADR (5/10/14/20), ATR (14), returns and classic pivot levels
- Best-effort sector index performance vs NIFTY 50, with unavailable tickers omitted rather than filled with guesses
- Official NSE FII/DII and option-chain links; no invented FII/DII or options metrics
- Source bar timestamps, app refresh timestamp, errors when sources return no data, CSV export

## Important data limitations
- `yfinance` is an unofficial open-source library using Yahoo Finance's publicly available endpoints. Its documentation says it is intended for research/educational use and personal use; consult Yahoo's terms before use. Public data can be delayed, stale, incomplete or unavailable.
- TradingView widgets provide their own data; coverage and delay vary by instrument and exchange. A free widget is **not a guarantee of real-time NSE data**.
- The dashboard refresh timestamp is not the exchange timestamp. The last returned source-bar timestamp is displayed separately.
- FII/DII, option-chain, PCR, max pain, Greeks, open-interest changes and tick-by-tick NSE data are not fabricated or scraped into the app. They remain official-source links until a dependable permitted feed or genuine file is supplied.
- Use the app for personal research. Check the data provider's licence/terms before redistribution or commercial deployment.

## Run locally
```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## Deploy using GitHub + Streamlit Community Cloud
1. Create a GitHub repository.
2. Upload `app.py`, `requirements.txt`, and `README.md` to the repository root.
3. Open https://share.streamlit.io/ and sign in with GitHub.
4. Create an app, select your repository and branch (`main`), and set main file path to `app.py`.
5. Deploy. After code changes are pushed to GitHub, the app redeploys.

Official deployment guide: https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app

## Data and widget references
- TradingView widgets: https://www.tradingview.com/widget/
- TradingView widget documentation: https://www.tradingview.com/widget-docs/getting-started/
- yfinance documentation and disclaimer: https://ranaroussi.github.io/yfinance/
- Yahoo Finance market coverage and delays: https://help.yahoo.com/kb/SLN2310.html
- NSE reports: https://www.nseindia.com/all-reports
- Nifty Indices historical data: https://www.niftyindices.com/reports/historical-data
- NSE FII/DII: https://www.nseindia.com/reports/fii-dii

## Data columns and calculations
Indicators are calculated from returned daily bars. SMA/EMA values are blank until enough observations exist. RSI uses Wilder-style exponential smoothing. ADR is the average daily high-low range for the requested number of rows (5/10/14/20). Pivot levels use the prior returned candle. Sector relative return is sector return minus NIFTY return over the selected returned lookback; it is not actual money-flow data.

## Expanded modules (13–22)
- Market regime/trend strength: heuristic ADX, EMA alignment, RSI, ATR and realized volatility from returned daily bars.
- Options analytics: optional user-uploaded option-chain CSV; PCR/max-pain calculations require mapped real columns and are not live-feed metrics.
- Risk sizing calculator: capital risk budget, stop distance and quantity/lot estimate; not a broker margin engine.
- Simple SMA crossover backtest: transparent long-only historical example with user-set approximate costs; not walk-forward validated.
- Market breadth: optional constituent CSV (`Date`, `Symbol`, `Close`) to calculate share of symbols above 20/50/200-day averages.
- Economic events: editable/importable/exportable calendar; events are user-entered, not an automatic calendar feed.
- Trade journal: editable/importable/exportable journal; export regularly because hosted app sessions do not guarantee persistence.
- Data-quality monitor: returned-row counts, last source candle timestamps, fetch errors and provenance.
- Cross-asset performance: best-effort public history for selected global assets, where the source returns data.
- Transparent setup scanner: simple daily EMA/RSI rules on configured dashboard instruments only, not a full-market scanner.

All additional modules explicitly show unavailable states when source data is missing. They do not create synthetic prices, option-chain values, FII/DII values or event announcements. Some CSV-based modules require the user to supply genuine data.
