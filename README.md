# MiniBro

Beginner-friendly paper trading website built with Flask and SQLite.

## Features

- User registration and login
- Starter cash balance of $10,000
- Watchlist support
- Market, limit, and stop orders
- Stock detail pages with charts
- Buy and sell orders
- Portfolio dashboard
- Profit and loss tracking
- Account performance chart
- Transaction history
- Intraday and historical stock charts

## Tech stack

- Python 3.11+
- Flask
- SQLite
- HTML / Jinja templates
- CSS

## Run locally

1. Create a virtual environment:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

2. Install dependencies:

   ```powershell
   python -m pip install -r requirements.txt
   ```

3. Add your local environment variables in `.env`:

   ```text
   ALPHA_VANTAGE_API_KEY=your_api_key_here
   ```

4. Start the app:

   ```powershell
   python app.py
   ```

5. Open `http://127.0.0.1:5000`

## Real stock data

The app uses Alpha Vantage as its market data source.

1. Create a free API key at [Alpha Vantage](https://www.alphavantage.co/support/#api-key).
2. Save it in a local `.env` file:

   ```text
   ALPHA_VANTAGE_API_KEY=your_api_key_here
   ```

3. Start the app and browse normally. Quotes, portfolio values, charts, and paper-trade prices will use Alpha Vantage.

The app uses Alpha Vantage's official `GLOBAL_QUOTE` and `TIME_SERIES_DAILY` endpoints, with a small cache to reduce rate-limit issues. If no API key is set, or the API limit is hit, the app cannot load market data and will show a warning.

## Notes

- Without an API key, the app cannot load prices or charts.
- The SQLite database file is created automatically as `stockbroker.db`.
