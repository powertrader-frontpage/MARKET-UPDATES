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


## V3 additions inspired by reference dashboard examples

- **Industry intelligence:** Upload daily `Date, Symbol, Industry, Close` history to calculate equal-weighted 1M/3M industry returns and the share of available constituents above 50/200-day moving averages.
- **Delivery & money-flow proxies:** Upload `Symbol, Date, Close, Volume, DeliveryPct` data (optional `Industry`, `TradedValue`) to inspect delivery and volume changes. These are activity proxies and do not prove institutional buying.
- **Daily market briefing:** Produces a source-labelled summary from the market history already fetched by the app; it does not fabricate news or causal explanations.

Example CSV rows in the app are schema examples only, not market data. These modules remain blank/unavailable until genuine inputs are provided. Export important results because hosted session storage is not guaranteed to persist.


## V4 feature completion update

Added:
- Macro dashboard and uploadable verified geopolitical event/macro observations.
- Multi-stock breadth and technical scanner (SMA20/50/200, EMA20, RSI14) using uploaded OHLC data.
- Option-chain analysis calculating PCR (OI), estimated max pain and OI by strike when required columns are provided.
- Futures basis and OI snapshot analysis from uploaded data.
- Stock deep-dive candlestick chart and indicators.
- Transparent SMA crossover backtest with configurable transaction-cost assumption and buy-and-hold comparison.
- Source-quality audit showing returned rows, latest source candle, data age and OHLC completeness.

Important: no broker API is required, but these modules cannot manufacture unavailable datasets. Derivatives, delivery, FII/DII and verified event history require genuine source files or separate public-source integrations. The backtest is a simple research baseline and omits slippage, taxes, survivorship bias and corporate-action pitfalls unless input data accounts for them.


## Reduced-manual-work update
- Industry Intelligence and Market Breadth / Stock Scanner now include a button to fetch daily history for a broad selected large-cap NSE-listed universe from Yahoo Finance via yfinance. Data is cached for up to six hours to reduce repeated requests. You can still override with a CSV.
- The automatic universe is a selected large-cap list, not the full NSE market or an official index constituent file; industry labels are broad classifications.
- Delivery percentage and derivatives option-chain/futures positioning still require a reliable source or uploaded genuine snapshots. These are not fabricated because they are not reliably supplied by the same free historical price feed.
- On Streamlit Community Cloud, cache/data availability can reset during restarts or sleep; press the fetch button when you want a new retrieval. Check the source timestamp and do not assume real-time data.
