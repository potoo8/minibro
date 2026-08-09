from __future__ import annotations

import argparse
import os
import sqlite3
import json
import time
from datetime import datetime
from functools import wraps
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from flask import (
    Flask,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "stockbroker.db"
ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"


def load_local_env_file() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


load_local_env_file()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
app.config["REAL_QUOTE_CACHE_SECONDS"] = 300
app.config["REAL_CHART_CACHE_SECONDS"] = 3600
app.config["SYMBOL_SEARCH_CACHE_SECONDS"] = 43200
app.config["MARKET_DATA_PROVIDER"] = "Alpha Vantage"

REAL_QUOTE_CACHE: dict[str, tuple[float, dict]] = {}
REAL_CHART_CACHE: dict[str, tuple[float, list[dict]]] = {}
SYMBOL_SEARCH_CACHE: dict[str, tuple[float, list[dict]]] = {}
SYMBOL_NAME_CACHE: dict[str, tuple[float, str]] = {}
FEATURED_SYMBOLS = ["AAPL", "MSFT", "NVDA", "AMZN"]

TIMEFRAME_OPTIONS = {
    "1D": {"label": "1D", "mode": "intraday", "interval": "5min"},
    "7D": {"label": "7D", "mode": "daily", "days": 7},
    "1M": {"label": "1M", "mode": "daily", "days": 30},
    "3M": {"label": "3M", "mode": "daily", "days": 90},
    "1Y": {"label": "1Y", "mode": "daily", "days": 252},
}


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_: object) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            cash_balance REAL NOT NULL DEFAULT 10000,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS holdings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            shares INTEGER NOT NULL,
            avg_cost REAL NOT NULL DEFAULT 0,
            UNIQUE(user_id, symbol),
            FOREIGN KEY (user_id) REFERENCES users (id)
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            company_name TEXT NOT NULL,
            trade_type TEXT NOT NULL,
            shares INTEGER NOT NULL,
            price REAL NOT NULL,
            total REAL NOT NULL,
            order_kind TEXT NOT NULL DEFAULT 'market',
            realized_pl REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        );

        CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, symbol),
            FOREIGN KEY (user_id) REFERENCES users (id)
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            company_name TEXT NOT NULL,
            side TEXT NOT NULL,
            order_type TEXT NOT NULL,
            shares INTEGER NOT NULL,
            limit_price REAL,
            stop_price REAL,
            status TEXT NOT NULL DEFAULT 'open',
            status_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            executed_at TEXT,
            executed_price REAL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        );
        """
    )
    ensure_column(db, "holdings", "avg_cost", "REAL NOT NULL DEFAULT 0")
    ensure_column(db, "transactions", "order_kind", "TEXT NOT NULL DEFAULT 'market'")
    ensure_column(db, "transactions", "realized_pl", "REAL NOT NULL DEFAULT 0")
    db.commit()


def ensure_column(db: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table_name})").fetchall()}
    if column_name not in columns:
        db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            flash("Please log in first.", "warning")
            return redirect(url_for("login"))
        return view(**kwargs)

    return wrapped_view


@app.before_request
def load_logged_in_user() -> None:
    init_db()
    user_id = session.get("user_id")
    g.user = None
    if user_id is not None:
        g.user = get_db().execute(
            "SELECT id, username, cash_balance FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        for notice in process_open_orders(user_id):
            flash(notice, "info")


def normalize_timeframe(raw_value: str | None) -> str:
    if raw_value in TIMEFRAME_OPTIONS:
        return raw_value
    return "1M"


def market_data_enabled() -> bool:
    return bool(os.environ.get("ALPHA_VANTAGE_API_KEY", "").strip())


def fetch_alpha_vantage_json(params: dict[str, str]) -> tuple[dict | None, str | None]:
    api_key = os.environ.get("ALPHA_VANTAGE_API_KEY", "").strip()
    if not api_key:
        return None, "Add an ALPHA_VANTAGE_API_KEY environment variable to enable real market data."

    query = urlencode({**params, "apikey": api_key})
    try:
        with urlopen(f"{ALPHA_VANTAGE_URL}?{query}", timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return None, "The market data request failed."

    if "Note" in payload:
        return None, "Alpha Vantage rate limit reached. Try again in a minute."
    if "Information" in payload and not payload.get("bestMatches"):
        return None, payload["Information"]
    if "Error Message" in payload:
        return None, payload["Error Message"]
    return payload, None


def search_symbols(query: str, limit: int = 8) -> tuple[list[dict], str | None]:
    normalized_query = query.strip()
    if not normalized_query:
        return [], None

    cache_key = normalized_query.lower()
    cached_search = SYMBOL_SEARCH_CACHE.get(cache_key)
    now = time.time()
    if cached_search and now - cached_search[0] < app.config["SYMBOL_SEARCH_CACHE_SECONDS"]:
        return cached_search[1][:limit], None

    payload, error = fetch_alpha_vantage_json(
        {
            "function": "SYMBOL_SEARCH",
            "keywords": normalized_query,
        }
    )
    if payload is None:
        return [], error

    matches: list[dict] = []
    for item in payload.get("bestMatches", []):
        symbol = item.get("1. symbol", "").strip().upper()
        if not symbol:
            continue
        score_text = item.get("9. matchScore", "0")
        try:
            match_score = float(score_text)
        except ValueError:
            match_score = 0.0
        match = {
            "symbol": symbol,
            "name": item.get("2. name", symbol).strip() or symbol,
            "type": item.get("3. type", "").strip() or "Unknown",
            "region": item.get("4. region", "").strip() or "Unknown",
            "currency": item.get("8. currency", "").strip() or "",
            "match_score": match_score,
        }
        matches.append(match)
        SYMBOL_NAME_CACHE[symbol] = (now, match["name"])

    matches.sort(
        key=lambda item: (
            item["region"] != "United States",
            item["type"] not in {"Equity", "ETF"},
            -item["match_score"],
            item["symbol"],
        )
    )
    limited_matches = matches[:limit]
    SYMBOL_SEARCH_CACHE[cache_key] = (now, limited_matches)
    return limited_matches, None


def fetch_symbol_name(symbol: str) -> str:
    normalized_symbol = symbol.upper().strip()
    cached_name = SYMBOL_NAME_CACHE.get(normalized_symbol)
    now = time.time()
    if cached_name and now - cached_name[0] < app.config["SYMBOL_SEARCH_CACHE_SECONDS"]:
        return cached_name[1]

    matches, _ = search_symbols(normalized_symbol, limit=5)
    for item in matches:
        if item["symbol"] == normalized_symbol:
            SYMBOL_NAME_CACHE[normalized_symbol] = (now, item["name"])
            return item["name"]
    return normalized_symbol


def create_cash_transaction(
    user_id: int,
    transaction_type: str,
    amount: float,
    created_at: str | None = None,
) -> None:
    timestamp = created_at or datetime.utcnow().isoformat()
    get_db().execute(
        """
        INSERT INTO transactions
        (user_id, symbol, company_name, trade_type, shares, price, total, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            "CASH",
            "Cash Balance",
            transaction_type,
            0,
            round(amount, 2),
            round(amount, 2),
            timestamp,
        ),
    )


def get_watchlist(user_id: int) -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT symbol, created_at FROM watchlist WHERE user_id = ? ORDER BY symbol",
        (user_id,),
    ).fetchall()


def get_open_orders(user_id: int) -> list[sqlite3.Row]:
    return get_db().execute(
        """
        SELECT id, symbol, company_name, side, order_type, shares, limit_price, stop_price, status, created_at
        FROM orders
        WHERE user_id = ? AND status = 'open'
        ORDER BY created_at DESC
        """,
        (user_id,),
    ).fetchall()


def build_watchlist_cards(user_id: int) -> list[dict]:
    cards = []
    for item in get_watchlist(user_id):
        quote, error = get_quote(item["symbol"])
        if quote is None:
            continue
        if error:
            flash(error, "warning")
        cards.append(
            {
                "symbol": quote["symbol"],
                "name": quote["name"],
                "price": quote["price"],
                "daily_change": quote["daily_change"],
                "daily_change_pct": quote["daily_change_pct"],
            }
        )
    return cards


def fetch_real_quote(symbol: str) -> tuple[dict | None, str | None]:
    symbol = symbol.upper().strip()
    cached_quote = REAL_QUOTE_CACHE.get(symbol)
    now = time.time()
    if cached_quote and now - cached_quote[0] < app.config["REAL_QUOTE_CACHE_SECONDS"]:
        return cached_quote[1], None

    payload, error = fetch_alpha_vantage_json(
        {
            "function": "GLOBAL_QUOTE",
            "symbol": symbol,
        }
    )
    if payload is None:
        if error and "rate limit" in error.lower():
            return None, "Alpha Vantage rate limit reached. Try the refresh button again in a minute."
        if error and "accepted" in error.lower():
            return None, "That symbol was not accepted by the real quote API."
        return None, error
    if error:
        return None, "That symbol was not accepted by the real quote API."

    quote = payload.get("Global Quote", {})
    price_text = quote.get("05. price")
    if not price_text:
        return None, "No real quote was returned for that symbol."

    previous_close = quote.get("08. previous close") or price_text
    change = quote.get("09. change") or "0"
    change_percent = quote.get("10. change percent", "0%").replace("%", "")
    latest_day = quote.get("07. latest trading day", "")

    quote_data = {
        "symbol": symbol,
        "name": fetch_symbol_name(symbol),
        "price": round(float(price_text), 2),
        "daily_change": round(float(change), 2),
        "daily_change_pct": round(float(change_percent), 2),
        "previous_close": round(float(previous_close), 2),
        "source": "real",
        "as_of": latest_day or "Latest market close",
    }
    REAL_QUOTE_CACHE[symbol] = (now, quote_data)
    return quote_data, None


def execute_trade(
    user_id: int,
    symbol: str,
    side: str,
    shares: int,
    price: float,
    order_kind: str = "market",
) -> tuple[bool, str | None, float]:
    db = get_db()
    symbol = symbol.upper().strip()
    company_name = fetch_symbol_name(symbol)
    holding = db.execute(
        "SELECT shares, avg_cost FROM holdings WHERE user_id = ? AND symbol = ?",
        (user_id, symbol),
    ).fetchone()
    owned_shares = holding["shares"] if holding else 0
    avg_cost = holding["avg_cost"] if holding else 0.0
    total_cost = round(price * shares, 2)
    realized_pl = 0.0

    user = db.execute("SELECT cash_balance FROM users WHERE id = ?", (user_id,)).fetchone()
    cash_balance = user["cash_balance"]

    if side == "buy":
        if cash_balance < total_cost:
            return False, "You do not have enough cash for that trade.", realized_pl
        new_shares = owned_shares + shares
        new_avg_cost = round(((owned_shares * avg_cost) + total_cost) / new_shares, 4)
        db.execute(
            """
            INSERT INTO holdings (user_id, symbol, shares, avg_cost)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, symbol) DO UPDATE SET shares = excluded.shares, avg_cost = excluded.avg_cost
            """,
            (user_id, symbol, new_shares, new_avg_cost),
        )
        db.execute(
            "UPDATE users SET cash_balance = cash_balance - ? WHERE id = ?",
            (total_cost, user_id),
        )
    elif side == "sell":
        if owned_shares < shares:
            return False, "You cannot sell more shares than you own.", realized_pl
        new_shares = owned_shares - shares
        realized_pl = round((price - avg_cost) * shares, 2)
        if new_shares == 0:
            db.execute(
                "DELETE FROM holdings WHERE user_id = ? AND symbol = ?",
                (user_id, symbol),
            )
        else:
            db.execute(
                "UPDATE holdings SET shares = ?, avg_cost = ? WHERE user_id = ? AND symbol = ?",
                (new_shares, avg_cost, user_id, symbol),
            )
        db.execute(
            "UPDATE users SET cash_balance = cash_balance + ? WHERE id = ?",
            (total_cost, user_id),
        )
    else:
        return False, "Invalid trade type.", realized_pl

    db.execute(
        """
        INSERT INTO transactions
        (user_id, symbol, company_name, trade_type, shares, price, total, order_kind, realized_pl, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            symbol,
            company_name,
            side,
            shares,
            round(price, 2),
            total_cost,
            order_kind,
            realized_pl,
            datetime.utcnow().isoformat(),
        ),
    )
    return True, None, realized_pl


def process_open_orders(user_id: int) -> list[str]:
    if not market_data_enabled():
        return []

    db = get_db()
    open_orders = get_open_orders(user_id)
    notices: list[str] = []
    for order in open_orders:
        quote, error = get_quote(order["symbol"])
        if quote is None:
            if error:
                notices.append(error)
            continue

        current_price = quote["price"]
        should_execute = False
        if order["order_type"] == "limit":
            if order["side"] == "buy" and order["limit_price"] is not None and current_price <= order["limit_price"]:
                should_execute = True
            if order["side"] == "sell" and order["limit_price"] is not None and current_price >= order["limit_price"]:
                should_execute = True
        elif order["order_type"] == "stop":
            if order["side"] == "buy" and order["stop_price"] is not None and current_price >= order["stop_price"]:
                should_execute = True
            if order["side"] == "sell" and order["stop_price"] is not None and current_price <= order["stop_price"]:
                should_execute = True

        if not should_execute:
            continue

        success, trade_error, _ = execute_trade(
            user_id,
            order["symbol"],
            order["side"],
            order["shares"],
            current_price,
            order_kind=order["order_type"],
        )
        if success:
            db.execute(
                """
                UPDATE orders
                SET status = 'filled', executed_at = ?, executed_price = ?, status_reason = ''
                WHERE id = ?
                """,
                (datetime.utcnow().isoformat(), current_price, order["id"]),
            )
            notices.append(
                f"{order['order_type'].title()} order filled: {order['side'].title()} {order['shares']} {order['symbol']} at {currency_filter(current_price)}."
            )
        else:
            db.execute(
                """
                UPDATE orders
                SET status = 'rejected', status_reason = ?
                WHERE id = ?
                """,
                (trade_error or "Order could not be executed.", order["id"]),
            )
            notices.append(
                f"{order['order_type'].title()} order for {order['symbol']} was rejected: {trade_error}"
            )
    if open_orders:
        db.commit()
    return notices


def fetch_real_chart_series(symbol: str, days: int = 30) -> tuple[list[dict] | None, str | None]:
    api_key = os.environ.get("ALPHA_VANTAGE_API_KEY", "").strip()
    if not api_key:
        return None, "Add an ALPHA_VANTAGE_API_KEY environment variable to enable real chart history."

    symbol = symbol.upper().strip()
    cache_key = f"{symbol}:{days}"
    cached_chart = REAL_CHART_CACHE.get(cache_key)
    now = time.time()
    if cached_chart and now - cached_chart[0] < app.config["REAL_CHART_CACHE_SECONDS"]:
        return cached_chart[1], None

    params = urlencode(
        {
            "function": "TIME_SERIES_DAILY",
            "symbol": symbol,
            "outputsize": "compact",
            "apikey": api_key,
        }
    )

    try:
        with urlopen(f"{ALPHA_VANTAGE_URL}?{params}", timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return None, "The chart data request failed."

    if "Note" in payload:
        return None, "Alpha Vantage rate limit reached. Try chart refresh again in a minute."
    if "Error Message" in payload:
        return None, "That symbol was not accepted by the real chart API."

    series = payload.get("Time Series (Daily)", {})
    if not series:
        return None, "No real chart history was returned for that symbol."

    chart_points = []
    for date_key in sorted(series.keys())[-days:]:
        candle = series[date_key]
        open_value = candle.get("1. open")
        high_value = candle.get("2. high")
        low_value = candle.get("3. low")
        close_value = candle.get("4. close")
        if not all([open_value, high_value, low_value, close_value]):
            continue
        chart_points.append(
            {
                "date": date_key,
                "open": round(float(open_value), 2),
                "high": round(float(high_value), 2),
                "low": round(float(low_value), 2),
                "close": round(float(close_value), 2),
            }
        )

    if not chart_points:
        return None, "No real chart history was returned for that symbol."
    REAL_CHART_CACHE[cache_key] = (now, chart_points)
    return chart_points, None


def fetch_intraday_chart_series(symbol: str, interval: str = "5min") -> tuple[list[dict] | None, str | None]:
    api_key = os.environ.get("ALPHA_VANTAGE_API_KEY", "").strip()
    if not api_key:
        return None, "Add an ALPHA_VANTAGE_API_KEY environment variable to enable intraday chart history."

    symbol = symbol.upper().strip()
    cache_key = f"{symbol}:intraday:{interval}"
    cached_chart = REAL_CHART_CACHE.get(cache_key)
    now = time.time()
    if cached_chart and now - cached_chart[0] < app.config["REAL_QUOTE_CACHE_SECONDS"]:
        return cached_chart[1], None

    params = urlencode(
        {
            "function": "TIME_SERIES_INTRADAY",
            "symbol": symbol,
            "interval": interval,
            "adjusted": "false",
            "extended_hours": "false",
            "outputsize": "compact",
            "apikey": api_key,
        }
    )

    try:
        with urlopen(f"{ALPHA_VANTAGE_URL}?{params}", timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return None, "The intraday chart data request failed."

    if "Note" in payload:
        return None, "Alpha Vantage rate limit reached. Try chart refresh again in a minute."
    if "Error Message" in payload:
        return None, "That symbol was not accepted by the intraday chart API."

    series_key = next((key for key in payload if key.startswith("Time Series (")), None)
    if not series_key:
        return None, "No intraday chart history was returned for that symbol."

    raw_series = payload.get(series_key, {})
    if not raw_series:
        return None, "No intraday chart history was returned for that symbol."

    latest_timestamp = max(raw_series.keys())
    latest_day = latest_timestamp.split(" ")[0]

    chart_points = []
    for timestamp in sorted(raw_series.keys()):
        if not timestamp.startswith(latest_day):
            continue
        candle = raw_series[timestamp]
        open_value = candle.get("1. open")
        high_value = candle.get("2. high")
        low_value = candle.get("3. low")
        close_value = candle.get("4. close")
        if not all([open_value, high_value, low_value, close_value]):
            continue
        chart_points.append(
            {
                "date": timestamp,
                "open": round(float(open_value), 2),
                "high": round(float(high_value), 2),
                "low": round(float(low_value), 2),
                "close": round(float(close_value), 2),
            }
        )

    if not chart_points:
        return None, "No intraday chart history was returned for that symbol."

    REAL_CHART_CACHE[cache_key] = (now, chart_points)
    return chart_points, None


def build_svg_points(prices: list[float], width: int = 420, height: int = 180, padding: int = 16) -> str:
    if not prices:
        return ""
    if len(prices) == 1:
        y = height / 2
        return f"{padding},{y:.2f} {width - padding},{y:.2f}"

    min_price = min(prices)
    max_price = max(prices)
    span = max(max_price - min_price, 1)
    step_x = (width - padding * 2) / (len(prices) - 1)

    points = []
    for index, price in enumerate(prices):
        x = padding + index * step_x
        normalized = (price - min_price) / span
        y = height - padding - normalized * (height - padding * 2)
        points.append(f"{x:.2f},{y:.2f}")
    return " ".join(points)


def build_candlestick_svg(series: list[dict], width: int = 420, height: int = 220, padding: int = 18) -> str:
    if not series:
        return ""

    lows = [point["low"] for point in series]
    highs = [point["high"] for point in series]
    min_price = min(lows)
    max_price = max(highs)
    span = max(max_price - min_price, 1)
    step_x = (width - padding * 2) / max(len(series) - 1, 1)
    body_width = max(step_x * 0.45, 4)

    def to_y(price: float) -> float:
        normalized = (price - min_price) / span
        return height - padding - normalized * (height - padding * 2)

    shapes = []
    for index, candle in enumerate(series):
        x = padding + index * step_x
        open_y = to_y(candle["open"])
        close_y = to_y(candle["close"])
        high_y = to_y(candle["high"])
        low_y = to_y(candle["low"])
        body_top = min(open_y, close_y)
        body_height = max(abs(close_y - open_y), 2)
        css_class = "candle-up" if candle["close"] >= candle["open"] else "candle-down"
        shapes.append(f'<line x1="{x:.2f}" y1="{high_y:.2f}" x2="{x:.2f}" y2="{low_y:.2f}" class="candle-wick {css_class}" />')
        shapes.append(
            f'<rect x="{(x - body_width / 2):.2f}" y="{body_top:.2f}" width="{body_width:.2f}" '
            f'height="{body_height:.2f}" rx="1.5" class="candle-body {css_class}" />'
        )
    return "".join(shapes)


def get_chart_data(symbol: str, timeframe_key: str = "1M") -> tuple[dict | None, str | None]:
    quote, quote_warning = get_quote(symbol)
    if not quote:
        return None, "Unknown stock symbol."

    timeframe_key = normalize_timeframe(timeframe_key)
    timeframe = TIMEFRAME_OPTIONS[timeframe_key]
    warning = quote_warning
    if timeframe["mode"] == "intraday":
        series, warning = fetch_intraday_chart_series(symbol, interval=timeframe["interval"])
    else:
        series, warning = fetch_real_chart_series(symbol, days=timeframe["days"])
    if series is None:
        return None, warning or "No chart history was returned for that symbol."
    source = "real"
    as_of = (
        f"Intraday prices through {series[-1]['date']}"
        if timeframe["mode"] == "intraday"
        else f"Daily closes through {series[-1]['date']}"
    )
    if timeframe["mode"] == "intraday":
        start_label = series[0]["date"][11:16]
        end_label = series[-1]["date"][11:16]
    else:
        start_label = series[0]["date"]
        end_label = series[-1]["date"]

    prices = [point["close"] for point in series]
    labels = [point["date"] for point in series]
    return (
        {
            "symbol": symbol.upper().strip(),
            "name": quote["name"],
            "source": source,
            "as_of": as_of,
            "timeframe_key": timeframe_key,
            "timeframe_label": TIMEFRAME_OPTIONS[timeframe_key]["label"],
            "latest_price": prices[-1],
            "open_price": series[0]["open"],
            "min_price": min(prices),
            "max_price": max(prices),
            "change": round(prices[-1] - prices[0], 2),
            "change_pct": round(((prices[-1] - prices[0]) / prices[0]) * 100, 2),
            "line_points": build_svg_points(prices),
            "mini_line_points": build_svg_points(prices, width=420, height=88, padding=10),
            "candles_svg": build_candlestick_svg(series),
            "labels": labels,
            "start_label": start_label,
            "end_label": end_label,
            "prices": prices,
            "series": series,
        },
        warning,
    )


def build_portfolio_history(chart_cards: list[dict], cash_balance: float) -> dict | None:
    chart_cards_with_series = [chart for chart in chart_cards if chart.get("series")]
    if not chart_cards_with_series:
        return None

    dates = sorted({point["date"] for chart in chart_cards_with_series for point in chart["series"]})
    position_maps = {
        chart["symbol"]: {point["date"]: point["close"] for point in chart["series"]}
        for chart in chart_cards_with_series
    }
    latest_seen = {chart["symbol"]: None for chart in chart_cards_with_series}

    values = []
    for date_key in dates:
        total_value = cash_balance
        for chart in chart_cards_with_series:
            maybe_price = position_maps[chart["symbol"]].get(date_key)
            if maybe_price is not None:
                latest_seen[chart["symbol"]] = maybe_price
            price = latest_seen[chart["symbol"]]
            if price is None:
                continue
            total_value += chart["shares"] * price
        values.append(round(total_value, 2))

    start_value = values[0]
    end_value = values[-1]
    return {
        "line_points": build_svg_points(values, width=860, height=220, padding=20),
        "labels": dates,
        "values": values,
        "start_value": start_value,
        "end_value": end_value,
        "change": round(end_value - start_value, 2),
        "change_pct": round(((end_value - start_value) / start_value) * 100, 2) if start_value else 0,
        "min_value": min(values),
        "max_value": max(values),
    }


def build_account_performance_chart(user_id: int, cash_balance: float, timeframe_key: str = "1M") -> tuple[dict | None, list[str]]:
    holdings = get_db().execute(
        "SELECT symbol, shares FROM holdings WHERE user_id = ? ORDER BY symbol",
        (user_id,),
    ).fetchall()
    chart_cards = []
    warnings: list[str] = []
    for holding in holdings:
        chart_data, warning = get_chart_data(holding["symbol"], timeframe_key=timeframe_key)
        if chart_data is None:
            if warning:
                warnings.append(f"{holding['symbol']}: {warning}")
            continue
        chart_cards.append({**chart_data, "shares": holding["shares"]})
    performance = build_portfolio_history(chart_cards, cash_balance)
    return performance, warnings


def get_quote(symbol: str) -> tuple[dict | None, str | None]:
    symbol = symbol.upper().strip()
    return fetch_real_quote(symbol)


def get_user_holdings(user_id: int) -> tuple[list[dict], list[str]]:
    rows = get_db().execute(
        "SELECT symbol, shares, avg_cost FROM holdings WHERE user_id = ? ORDER BY symbol",
        (user_id,),
    ).fetchall()

    holdings = []
    warnings: list[str] = []
    for row in rows:
        quote, error = get_quote(row["symbol"])
        if not quote:
            if error:
                warnings.append(f"{row['symbol']}: {error}")
            holdings.append(
                {
                    "symbol": row["symbol"],
                    "shares": row["shares"],
                    "price": None,
                    "market_value": None,
                    "name": fetch_symbol_name(row["symbol"]),
                    "source": "unavailable",
                    "avg_cost": row["avg_cost"],
                    "cost_basis": round(row["shares"] * row["avg_cost"], 2),
                    "unrealized_pl": None,
                    "unrealized_pl_pct": None,
                    "market_data_available": False,
                }
            )
            continue
        market_value = round(row["shares"] * quote["price"], 2)
        cost_basis = round(row["shares"] * row["avg_cost"], 2)
        unrealized_pl = round(market_value - cost_basis, 2)
        unrealized_pl_pct = round((unrealized_pl / cost_basis) * 100, 2) if cost_basis else 0
        holdings.append(
            {
                "symbol": row["symbol"],
                "shares": row["shares"],
                "price": quote["price"],
                "market_value": market_value,
                "name": quote["name"],
                "source": quote["source"],
                "avg_cost": row["avg_cost"],
                "cost_basis": cost_basis,
                "unrealized_pl": unrealized_pl,
                "unrealized_pl_pct": unrealized_pl_pct,
                "market_data_available": True,
            }
        )
    return holdings, warnings


def get_portfolio_totals(user_id: int, cash_balance: float) -> dict:
    holdings, warnings = get_user_holdings(user_id)
    holdings_value = round(sum(item["market_value"] or 0 for item in holdings), 2)
    total_value = round(cash_balance + holdings_value, 2)
    total_cost_basis = round(sum(item["cost_basis"] for item in holdings), 2)
    unrealized_pl = round(sum(item["unrealized_pl"] or 0 for item in holdings), 2)
    unrealized_pl_pct = round((unrealized_pl / total_cost_basis) * 100, 2) if total_cost_basis else 0
    realized_pl = get_db().execute(
        "SELECT COALESCE(SUM(realized_pl), 0) FROM transactions WHERE user_id = ?",
        (user_id,),
    ).fetchone()[0]
    return {
        "holdings": holdings,
        "holdings_value": holdings_value,
        "total_value": total_value,
        "warnings": warnings,
        "unrealized_pl": unrealized_pl,
        "unrealized_pl_pct": unrealized_pl_pct,
        "realized_pl": round(realized_pl, 2),
    }


@app.route("/")
def index():
    featured_quotes = []
    for symbol in FEATURED_SYMBOLS:
        quote, _ = get_quote(symbol)
        if quote:
            featured_quotes.append(quote)
    return render_template(
        "index.html",
        featured_quotes=featured_quotes,
        auto_refresh_enabled=False,
        market_data_provider=app.config["MARKET_DATA_PROVIDER"],
        market_data_enabled=market_data_enabled(),
    )


@app.route("/register", methods=("GET", "POST"))
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        error = None
        if len(username) < 3:
            error = "Username must be at least 3 characters."
        elif len(password) < 6:
            error = "Password must be at least 6 characters."
        elif password != confirm_password:
            error = "Passwords do not match."

        db = get_db()
        existing_user = db.execute(
            "SELECT id FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if existing_user is not None:
            error = "That username is already taken."

        if error is None:
            db.execute(
                """
                INSERT INTO users (username, password_hash, cash_balance, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    username,
                    generate_password_hash(password),
                    10000.0,
                    datetime.utcnow().isoformat(),
                ),
            )
            db.commit()
            flash("Account created. You can log in now.", "success")
            return redirect(url_for("login"))

        flash(error, "danger")

    return render_template("register.html")


@app.route("/login", methods=("GET", "POST"))
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = get_db().execute(
            "SELECT * FROM users WHERE username = ?",
            (username,),
        ).fetchone()

        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Incorrect username or password.", "danger")
        else:
            session.clear()
            session["user_id"] = user["id"]
            flash(f"Welcome back, {user['username']}!", "success")
            return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("index"))


@app.route("/dashboard")
@login_required
def dashboard():
    totals = get_portfolio_totals(g.user["id"], g.user["cash_balance"])
    for warning in dict.fromkeys(totals["warnings"]):
        flash(warning, "warning")
    performance_chart, performance_warnings = build_account_performance_chart(g.user["id"], g.user["cash_balance"])
    for warning in dict.fromkeys(performance_warnings):
        flash(warning, "warning")
    transactions = get_db().execute(
        """
        SELECT symbol, company_name, trade_type, shares, price, total, order_kind, realized_pl, created_at
        FROM transactions
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT 8
        """,
        (g.user["id"],),
    ).fetchall()
    return render_template(
        "dashboard.html",
        holdings=totals["holdings"],
        holdings_value=totals["holdings_value"],
        total_value=totals["total_value"],
        unrealized_pl=totals["unrealized_pl"],
        unrealized_pl_pct=totals["unrealized_pl_pct"],
        realized_pl=totals["realized_pl"],
        transactions=transactions,
        watchlist=build_watchlist_cards(g.user["id"]),
        open_orders=get_open_orders(g.user["id"]),
        performance_chart=performance_chart,
        auto_refresh_enabled=False,
        market_data_provider=app.config["MARKET_DATA_PROVIDER"],
        market_data_enabled=market_data_enabled(),
    )


@app.route("/balance", methods=("POST",))
@login_required
def update_balance():
    action = request.form.get("action", "").strip().lower()
    amount_text = request.form.get("amount", "").strip()

    try:
        amount = round(float(amount_text), 2)
    except ValueError:
        amount = 0

    if amount <= 0:
        flash("Enter a cash amount greater than $0.00.", "danger")
        return redirect(url_for("dashboard"))

    db = get_db()
    error = None
    success_message = None

    if action == "deposit":
        db.execute(
            "UPDATE users SET cash_balance = cash_balance + ? WHERE id = ?",
            (amount, g.user["id"]),
        )
        create_cash_transaction(g.user["id"], "deposit", amount)
        success_message = f"Added {currency_filter(amount)} to your cash balance."
    elif action == "withdraw":
        if g.user["cash_balance"] < amount:
            error = "You cannot withdraw more cash than you currently have."
        else:
            db.execute(
                "UPDATE users SET cash_balance = cash_balance - ? WHERE id = ?",
                (amount, g.user["id"]),
            )
            create_cash_transaction(g.user["id"], "withdraw", -amount)
            success_message = f"Removed {currency_filter(amount)} from your cash balance."
    else:
        error = "Invalid cash action."

    if error:
        flash(error, "danger")
        return redirect(url_for("dashboard"))

    db.commit()
    flash(success_message, "success")
    return redirect(url_for("dashboard"))


@app.route("/quote", methods=("GET", "POST"))
@login_required
def quote():
    quote_data = None
    search_results: list[dict] = []
    search_query = request.args.get("query", "").strip()
    symbol = request.args.get("symbol", "").strip()

    if symbol:
        quote_data, error = get_quote(symbol)
        if error:
            flash(error, "warning")
        elif quote_data is None:
            flash("No quote data was returned for that symbol.", "danger")
    elif search_query:
        search_results, error = search_symbols(search_query)
        if error:
            flash(error, "warning")
        elif not search_results:
            flash("No matching symbols were found for that search.", "warning")

    if request.method == "POST":
        submitted_query = request.form.get("query", "").strip()
        submitted_symbol = request.form.get("symbol", "").strip()
        if submitted_symbol:
            quote_data, error = get_quote(submitted_symbol)
            if error:
                flash(error, "warning")
            if quote_data is None:
                flash("No quote data was returned for that symbol.", "danger")
            search_query = submitted_symbol
        else:
            search_query = submitted_query
            search_results, error = search_symbols(search_query)
            if error:
                flash(error, "warning")
            elif not search_results:
                flash("No matching symbols were found for that search.", "warning")
    return render_template(
        "quote.html",
        quote_data=quote_data,
        search_query=search_query,
        search_results=search_results,
        watchlist_symbols={item["symbol"] for item in get_watchlist(g.user["id"])},
        auto_refresh_enabled=False,
        market_data_provider=app.config["MARKET_DATA_PROVIDER"],
        market_data_enabled=market_data_enabled(),
    )


@app.route("/trade/<symbol>", methods=("GET", "POST"))
@login_required
def trade(symbol: str):
    quote_data, warning = get_quote(symbol)
    if quote_data is None:
        flash("Unknown stock symbol.", "danger")
        return redirect(url_for("quote"))
    if warning:
        flash(warning, "warning")

    db = get_db()
    holding = db.execute(
        "SELECT shares FROM holdings WHERE user_id = ? AND symbol = ?",
        (g.user["id"], quote_data["symbol"]),
    ).fetchone()
    owned_shares = holding["shares"] if holding else 0

    if request.method == "POST":
        trade_type = request.form.get("trade_type", "buy")
        order_kind = request.form.get("order_kind", "market").strip().lower()
        shares_input = request.form.get("shares", "0")
        trigger_price_input = request.form.get("trigger_price", "").strip()

        try:
            shares = int(shares_input)
        except ValueError:
            shares = 0

        if shares <= 0:
            flash("Shares must be a whole number greater than 0.", "danger")
            return render_template(
                "trade.html",
                quote=quote_data,
                owned_shares=owned_shares,
                watchlist_symbols={item["symbol"] for item in get_watchlist(g.user["id"])},
                auto_refresh_enabled=False,
                market_data_provider=app.config["MARKET_DATA_PROVIDER"],
                market_data_enabled=market_data_enabled(),
            )
        error = None
        trigger_price = None
        if order_kind in {"limit", "stop"}:
            try:
                trigger_price = round(float(trigger_price_input), 2)
            except ValueError:
                trigger_price = None
            if trigger_price is None or trigger_price <= 0:
                error = "Enter a valid trigger price for limit and stop orders."

        if trade_type not in {"buy", "sell"}:
            error = "Invalid trade type."

        if error is None and order_kind == "market":
            success, error, _ = execute_trade(
                g.user["id"],
                quote_data["symbol"],
                trade_type,
                shares,
                quote_data["price"],
                order_kind="market",
            )
            if success:
                db.commit()
                flash(
                    f"{trade_type.title()} order completed for {shares} shares of {quote_data['symbol']}.",
                    "success",
                )
                return redirect(url_for("dashboard"))
        elif error is None:
            db.execute(
                """
                INSERT INTO orders
                (user_id, symbol, company_name, side, order_type, shares, limit_price, stop_price, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    g.user["id"],
                    quote_data["symbol"],
                    quote_data["name"],
                    trade_type,
                    order_kind,
                    shares,
                    trigger_price if order_kind == "limit" else None,
                    trigger_price if order_kind == "stop" else None,
                    datetime.utcnow().isoformat(),
                ),
            )
            db.commit()
            flash(
                f"{order_kind.title()} {trade_type} order placed for {shares} shares of {quote_data['symbol']}.",
                "success",
            )
            return redirect(url_for("orders"))

        flash(error, "danger")

    return render_template(
        "trade.html",
        quote=quote_data,
        owned_shares=owned_shares,
        watchlist_symbols={item["symbol"] for item in get_watchlist(g.user["id"])},
        auto_refresh_enabled=False,
        market_data_provider=app.config["MARKET_DATA_PROVIDER"],
        market_data_enabled=market_data_enabled(),
    )


@app.route("/stock/<symbol>")
@login_required
def stock_detail(symbol: str):
    quote_data, warning = get_quote(symbol)
    if quote_data is None:
        flash("Unknown stock symbol.", "danger")
        return redirect(url_for("quote"))
    if warning:
        flash(warning, "warning")

    timeframe_key = normalize_timeframe(request.args.get("range"))
    chart_data, chart_warning = get_chart_data(symbol, timeframe_key=timeframe_key)
    if chart_warning:
        flash(chart_warning, "warning")

    holding = get_db().execute(
        "SELECT shares, avg_cost FROM holdings WHERE user_id = ? AND symbol = ?",
        (g.user["id"], quote_data["symbol"]),
    ).fetchone()
    watchlist_symbols = {item["symbol"] for item in get_watchlist(g.user["id"])}
    timeframe_links = [
        {"key": key, "label": config["label"], "active": key == timeframe_key}
        for key, config in TIMEFRAME_OPTIONS.items()
    ]

    return render_template(
        "stock_detail.html",
        quote=quote_data,
        chart=chart_data,
        timeframe_links=timeframe_links,
        holding=holding,
        on_watchlist=quote_data["symbol"] in watchlist_symbols,
        auto_refresh_enabled=False,
        market_data_provider=app.config["MARKET_DATA_PROVIDER"],
        market_data_enabled=market_data_enabled(),
    )


@app.route("/watchlist/add/<symbol>", methods=("POST",))
@login_required
def add_to_watchlist(symbol: str):
    symbol = symbol.upper().strip()
    get_db().execute(
        """
        INSERT OR IGNORE INTO watchlist (user_id, symbol, created_at)
        VALUES (?, ?, ?)
        """,
        (g.user["id"], symbol, datetime.utcnow().isoformat()),
    )
    get_db().commit()
    flash(f"{symbol} added to your watchlist.", "success")
    return redirect(request.form.get("next") or url_for("quote", symbol=symbol))


@app.route("/watchlist/remove/<symbol>", methods=("POST",))
@login_required
def remove_from_watchlist(symbol: str):
    symbol = symbol.upper().strip()
    get_db().execute(
        "DELETE FROM watchlist WHERE user_id = ? AND symbol = ?",
        (g.user["id"], symbol),
    )
    get_db().commit()
    flash(f"{symbol} removed from your watchlist.", "info")
    return redirect(request.form.get("next") or url_for("dashboard"))


@app.route("/orders")
@login_required
def orders():
    orders_data = get_db().execute(
        """
        SELECT id, symbol, company_name, side, order_type, shares, limit_price, stop_price, status, status_reason, created_at, executed_at, executed_price
        FROM orders
        WHERE user_id = ?
        ORDER BY created_at DESC
        """,
        (g.user["id"],),
    ).fetchall()
    return render_template("orders.html", orders=orders_data, auto_refresh_enabled=False)


@app.route("/orders/<int:order_id>/cancel", methods=("POST",))
@login_required
def cancel_order(order_id: int):
    updated = get_db().execute(
        """
        UPDATE orders
        SET status = 'canceled', status_reason = 'Canceled by user'
        WHERE id = ? AND user_id = ? AND status = 'open'
        """,
        (order_id, g.user["id"]),
    )
    get_db().commit()
    if updated.rowcount:
        flash("Order canceled.", "info")
    else:
        flash("That order could not be canceled.", "warning")
    return redirect(url_for("orders"))


@app.route("/history")
@login_required
def history():
    transactions = get_db().execute(
        """
        SELECT symbol, company_name, trade_type, shares, price, total, order_kind, realized_pl, created_at
        FROM transactions
        WHERE user_id = ?
        ORDER BY created_at DESC
        """,
        (g.user["id"],),
    ).fetchall()
    return render_template("history.html", transactions=transactions)


@app.route("/charts")
@login_required
def charts():
    timeframe_key = normalize_timeframe(request.args.get("range"))
    db_holdings = get_db().execute(
        """
        SELECT h.symbol, h.shares
        FROM holdings h
        WHERE h.user_id = ?
        ORDER BY h.symbol
        """,
        (g.user["id"],),
    ).fetchall()

    chart_cards = []
    warnings: list[str] = []
    for holding in db_holdings:
        chart_data, warning = get_chart_data(holding["symbol"], timeframe_key=timeframe_key)
        if chart_data is None:
            if warning:
                warnings.append(f"{holding['symbol']}: {warning}")
            chart_cards.append(
                {
                    "symbol": holding["symbol"],
                    "name": fetch_symbol_name(holding["symbol"]),
                    "shares": holding["shares"],
                    "source": "unavailable",
                    "as_of": "Chart data unavailable",
                    "timeframe_key": timeframe_key,
                    "timeframe_label": TIMEFRAME_OPTIONS[timeframe_key]["label"],
                    "chart_available": False,
                }
            )
            continue
        if warning:
            warnings.append(warning)
        chart_cards.append(
            {
                **chart_data,
                "shares": holding["shares"],
                "chart_available": True,
            }
        )

    for warning in dict.fromkeys(warnings):
        flash(warning, "warning")

    portfolio_chart = build_portfolio_history(chart_cards, g.user["cash_balance"])
    timeframe_links = [
        {
            "key": key,
            "label": config["label"],
        "active": key == timeframe_key,
        }
        for key, config in TIMEFRAME_OPTIONS.items()
    ]

    return render_template(
        "charts.html",
        chart_cards=chart_cards,
        timeframe_key=timeframe_key,
        timeframe_links=timeframe_links,
        portfolio_chart=portfolio_chart,
        auto_refresh_enabled=False,
        market_data_provider=app.config["MARKET_DATA_PROVIDER"],
        market_data_enabled=market_data_enabled(),
    )


@app.template_filter("currency")
def currency_filter(value: float) -> str:
    return f"${value:,.2f}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the MiniBro Flask development server.")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface to bind to. Use 0.0.0.0 for local network access.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5000,
        help="Port to listen on.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable Flask debug mode.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    app.run(host=args.host, port=args.port, debug=args.debug)
