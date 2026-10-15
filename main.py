import json
import os
import re
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
import requests
from pybit.unified_trading import HTTP

# ==================== НАСТРОЙКИ (ПО УМОЛЧАНИЮ) ====================
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
if not TELEGRAM_BOT_TOKEN:
    print("✗ Ошибка: TELEGRAM_BOT_TOKEN не найден в переменных окружения!")
    exit(1)

# --- Настройки Bybit Testnet API ---
BYBIT_API_KEY = os.environ.get('BYBIT_API_KEY', '')
BYBIT_SECRET_KEY = os.environ.get('BYBIT_SECRET_KEY', '')

# Инициализация клиента Bybit Testnet (UTA)
bybit_session = HTTP(
    testnet=True,
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_SECRET_KEY
)

# --- Настройки торгового модуля ---
TRADE_LEVERAGE = 1             # Первое плечо
TRADE_AMOUNT_USDT = 100.0      # $100 на позицию
MAX_OPEN_POSITIONS = 2         # Максимум 2 позиции одновременно
TAKE_PROFIT_PCT = 0.25         # Тейк-профит 0.25%

# --- Настройки условия сигнала по умолчанию ---
DEFAULT_MIN_PRICE_DROP = -0.15      
DEFAULT_MAX_PRICE_DROP = -0.50      
DEFAULT_MIN_OI_DROP = -0.5         

DEFAULT_MIN_24H_TREND = 1.0        
DEFAULT_MAX_24H_TREND = 10.0       

DEFAULT_MIN_24H_VOLUME_USDT = 1_000_000  

# --- Фильтр фандинга: теперь ищем положительный фандинг (> 0) ---
DEFAULT_FUNDING_FILTER_ONLY_NEGATIVE = False  

STOCKS_TICKERS = [
    'MTB', 'COF', 'KKR', 'TFC', 'USB', 'FITB', 'RF', 'BEN', 'MS', 'PGR', 'AFG', 'TROW', 'CIM', 'MFA', 'SOFI', 'UPST', 'INTU', 'HDB', 'BBD',
    'NVDA', 'AAPL', 'MSFT', 'GOOG', 'AMZN', 'META', 'TSLA', 'AMD', 'ADBE', 'MRVL', 'QCOM', 'SNPS', 'CRWD', 'DDOG', 'TXN', 'FTNT', 'OKTA', 'TWLO', 'BOX', 'JBL', 'HPE', 'DELL', 'NTAP', 'CDNS', 'SMCI', 'ARM', 'NBIS', 'SONY', 'BB', 'SKHY', 'AXTI', 'QNT', 'CBRS',
    'COST', 'WMT', 'YUM', 'YUMC', 'WEN', 'KHC', 'MO', 'ULTA', 'ROST', 'DLTR', 'BBWI', 'MAT', 'DKNG', 'RCL', 'LYFT', 'GRAB', 'MELI', 'BMBL', 'BYND', 'SIG', 'HTHT',
    'MRK', 'ABT', 'BSX', 'DHR', 'VRTX', 'REGN', 'AMGN', 'GILD', 'INCY', 'ILMN', 'HCA', 'MCK', 'CAH', 'MOH',
    'GEV', 'HON', 'UPS', 'DAL', 'PCAR', 'IR', 'ITW', 'SWK', 'GNRC', 'NOC', 'WM', 'RSG', 'VMC', 'SHW', 'IP', 'XEL', 'SRE', 'PEG', 'PPL', 'OXY', 'CTRA', 'APA', 'LYB', 'EMN', 'WPM', 'NEM', 'FCEL', 'FLNC',
    'NFLX', 'TMUS', 'SPGI', 'ACN', 'BKNG', 'TRV', 'MET', 'LPL', 'NWS', 'FOX', 'RBLX', 'ROKU', 'SNAP', 'PENN', 'LAUR', 'DXC', 'SPCE', 'RKLB', 'EC', 'ICL', 'LBTYK', 'TME',
    'SPY', 'QQQ', 'IWM', 'URNM', 'UVXY', 'SQQQ', 'SOXL', 'KORU', 'DRAM', 'PSA'
]

EXCLUDED_STOCKS = set(STOCKS_TICKERS) | {f"{t}USDT" for t in STOCKS_TICKERS}

TIME_WINDOW = 60 * 3              
POLL_INTERVAL = 5                
COOLDOWN_MINUTES = 10             
DAILY_ALERT_LIMIT = 100           

session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
session.mount('https://', adapter)

users = {
    '5296533274': {
        'active': True,
        'alert_counts': {},
        'settings': {
            'min_price_drop': DEFAULT_MIN_PRICE_DROP,
            'max_price_drop': DEFAULT_MAX_PRICE_DROP,
            'min_oi_drop': DEFAULT_MIN_OI_DROP,
            'min_24h_trend': DEFAULT_MIN_24H_TREND,
            'max_24h_trend': DEFAULT_MAX_24H_TREND,
            'min_24h_volume': DEFAULT_MIN_24H_VOLUME_USDT,
            'only_negative_funding': DEFAULT_FUNDING_FILTER_ONLY_NEGATIVE
        }
    }
}

historical_data = {}
last_alert_time = {}
traded_today_symbols = set()  # Список монет, по которым сегодня уже открывались сделки
data_lock = threading.Lock()


def get_ye_time():
    return datetime.now(timezone.utc) + timedelta(hours=5)


def get_user_settings(chat_id):
    if chat_id not in users:
        return {
            'min_price_drop': DEFAULT_MIN_PRICE_DROP,
            'max_price_drop': DEFAULT_MAX_PRICE_DROP,
            'min_oi_drop': DEFAULT_MIN_OI_DROP,
            'min_24h_trend': DEFAULT_MIN_24H_TREND,
            'max_24h_trend': DEFAULT_MAX_24H_TREND,
            'min_24h_volume': DEFAULT_MIN_24H_VOLUME_USDT,
            'only_negative_funding': DEFAULT_FUNDING_FILTER_ONLY_NEGATIVE
        }
    if 'settings' not in users[chat_id]:
        users[chat_id]['settings'] = {
            'min_price_drop': DEFAULT_MIN_PRICE_DROP,
            'max_price_drop': DEFAULT_MAX_PRICE_DROP,
            'min_oi_drop': DEFAULT_MIN_OI_DROP,
            'min_24h_trend': DEFAULT_MIN_24H_TREND,
            'max_24h_trend': DEFAULT_MAX_24H_TREND,
            'min_24h_volume': DEFAULT_MIN_24H_VOLUME_USDT,
            'only_negative_funding': DEFAULT_FUNDING_FILTER_ONLY_NEGATIVE
        }
    return users[chat_id]['settings']


def get_alert_count(chat_id, symbol):
    if chat_id not in users:
        return 0
    return users[chat_id]['alert_counts'].get(symbol, 0)


def increment_alert_count(chat_id, symbol):
    if chat_id in users:
        users[chat_id]['alert_counts'][symbol] = get_alert_count(chat_id, symbol) + 1


def can_send_alert(chat_id, symbol):
    if chat_id not in users or not users[chat_id]['active']:
        return False
    if get_alert_count(chat_id, symbol) >= DAILY_ALERT_LIMIT:
        return False
    return True


def calculate_change(old, new):
    if old == 0:
        return 0.0
    return ((new - old) / old) * 100


def generate_links(symbol):
    encoded_symbol = urllib.parse.quote(symbol)
    return {
        'coinglass': f"https://www.coinglass.com/tv/Bybit_{encoded_symbol}",
        'tradingview': f"https://www.tradingview.com/chart/?symbol=BYBIT%3A{encoded_symbol}.P",
        'binance': f"https://www.binance.com/ru/trade/{encoded_symbol}",
        'bybit': f"https://www.bybit.com/trade/usdt/{encoded_symbol}"
    }


def send_telegram_notification(chat_id, message, symbol):
    if not can_send_alert(chat_id, symbol):
        return False

    increment_alert_count(chat_id, symbol)
    current_count = get_alert_count(chat_id, symbol)
    monospace_symbol = f"<code>{symbol}</code>"

    def wrap_numbers(text):
        return re.sub(r'([+-]?\d+(?:\.\d+)?%)', r'<code>\1</code>', text)

    message = wrap_numbers(message)
    links = generate_links(symbol)
    message_with_links = (
        f"{message}\n\n"
        f"🔗 <b>Быстрый анализ:</b>\n"
        f"• 📊 <a href='{links['coinglass']}'>Coinglass TV</a>\n"
        f"• 📈 <a href='{links['tradingview']}'>TradingView</a>\n"
        f"• 💰 <a href='{links['binance']}'>Binance</a>\n"
        f"• ⚡ <a href='{links['bybit']}'>Bybit</a>\n\n"
        f"📊 <b>Алертов по {monospace_symbol} сегодня:</b> <code>{current_count}/{DAILY_ALERT_LIMIT}</code>"
    )

    message_with_links = message_with_links.replace(symbol, monospace_symbol)

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': message_with_links,
        'parse_mode': 'HTML',
        'disable_web_page_preview': True
    }
    try:
        response = session.post(url, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"✗ Ошибка отправки в TG: {repr(e)}")
        return False


def send_log_to_telegram(text):
    """Отправка системных логов и торговых событий в Telegram всем активным пользователям"""
    for chat_id in users:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            'chat_id': chat_id,
            'text': f"🤖 <b>[Бот Testnet]:</b> {text}",
            'parse_mode': 'HTML'
        }
        try:
            session.post(url, json=payload, timeout=10)
        except Exception:
            pass


# ==================== ТОРГОВЫЙ МОДУЛЬ BYBIT TESTNET ====================
def get_active_positions_count():
    try:
        resp = bybit_session.get_positions(category="linear", settleCoin="USDT")
        if resp.get('retCode') == 0:
            positions = resp['result']['list']
            active = [p for p in positions if float(p.get('size', 0)) > 0]
            return len(active)
    except Exception as e:
        print(f"Ошибка получения позиций: {e}")
    return 0


def open_short_position(symbol, current_price):
    with data_lock:
        if symbol in traded_today_symbols:
            return
        if get_active_positions_count() >= MAX_OPEN_POSITIONS:
            return

        # Отмечаем, что по этой монете сегодня сделка открыта
        traded_today_symbols.add(symbol)

    try:
        # 1. Установка плеча
        try:
            bybit_session.set_leverage(
                category="linear",
                symbol=symbol,
                buyLeverage=str(TRADE_LEVERAGE),
                sellLeverage=str(TRADE_LEVERAGE)
            )
        except Exception:
            pass # Если плечо уже выставлено, игнорируем ошибку

        # 2. Расчет количества монет на $100
        # Получаем спецификацию контракта для правильного шага количества
        instr_info = bybit_session.get_instruments_info(category="linear", symbol=symbol)
        lot_size_filter = instr_info['result']['list'][0]['lotSizeFilter']
        min_qty = float(lot_size_filter['minOrderQty'])
        qty_step = float(lot_size_filter['qtyStep'])

        raw_qty = TRADE_AMOUNT_USDT / current_price
        # Округляем до шага qty_step
        precision = len(str(qty_step).split('.')[-1]) if '.' in str(qty_step) else 0
        qty = round(raw_qty - (raw_qty % qty_step), precision)
        if qty < min_qty:
            qty = min_qty

        # 3. Расчет Тейк-Профита (0.25% ниже цены входа для Short)
        tp_price = round(current_price * (1 - TAKE_PROFIT_PCT / 100), 2)

        # 4. Открытие рыночного ордера SHORT
        order_resp = bybit_session.place_order(
            category="linear",
            symbol=symbol,
            side="Sell",
            orderType="Market",
            qty=str(qty),
            takeProfit=str(tp_price),
            tpTriggerBy="LastPrice",
            timeInForce="GoodTillCancel"
        )

        if order_resp.get('retCode') == 0:
            msg = (
                f"🟢 <b>Бот открыл сделку (Short)!</b>\n\n"
                f"• Монета: <code>{symbol}</code>\n"
                f"• Цена входа: <code>{current_price}</code>\n"
                f"• Объем: <code>${TRADE_AMOUNT_USDT}</code> (Плечо {TRADE_LEVERAGE}x)\n"
                f"• Тейк-профит: <code>{tp_price} (+{TAKE_PROFIT_PCT}%)</code>\n"
                f"• Стоп-лосс: <b>Отсутствует</b>"
            )
            send_log_to_telegram(msg)
            print(f"✓ Открыт шорт по {symbol} по цене {current_price}")
        else:
            print(f"✗ Ошибка открытия ордера по {symbol}: {order_resp.get('retMsg')}")

    except Exception as e:
        print(f"✗ Исключение при открытии ордера {symbol}: {e}")


def monitor_positions_and_pnl():
    """Фоновый мониторинг открытых позиций для отслеживания тейков и просадок > 1%"""
    notified_drawdowns = set()

    while True:
        try:
            resp = bybit_session.get_positions(category="linear", settleCoin="USDT")
            if resp.get('retCode') == 0:
                positions = resp['result']['list']
                for pos in positions:
                    size = float(pos.get('size', 0))
                    if size <= 0:
                        continue

                    symbol = pos['symbol']
                    unrealised_pnl = float(pos.get('unrealisedPnl', 0))
                    position_value = float(pos.get('positionValue', TRADE_AMOUNT_USDT))
                    
                    # Расчет текущего убытка в %
                    pnl_pct = (unrealised_pnl / position_value) * 100

                    # Уведомление об убытке > 1% (срабатывает 1 раз при переходе порога)
                    if pnl_pct <= -1.0:
                        if symbol not in notified_drawdowns:
                            send_log_to_telegram(
                                f"⚠️ <b>Внимание! Убыток по позиции превысил 1%!</b>\n\n"
                                f"• Монета: <code>{symbol}</code>\n"
                                f"• Текущий убыток: <code>{pnl_pct:.2f}%</code> "
                                f"(PnL: <code>${unrealised_pnl:.2f}</code>)"
                            )
                            notified_drawdowns.add(symbol)
                    else:
                        if symbol in notified_drawdowns:
                            notified_drawdowns.remove(symbol)

            time.sleep(10)
        except Exception:
            time.sleep(15)


# ==================== ТЕЛЕГРАМ БОТ ====================
def handle_telegram_updates():
    last_update_id = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {'timeout': 20, 'offset': last_update_id + 1}
            response = session.get(url, params=params, timeout=25)
            data = response.json()

            if data.get('ok'):
                for update in data['result']:
                    last_update_id = update['update_id']
                    if 'message' not in update:
                        continue

                    message = update['message']
                    chat_id = str(message['chat']['id'])
                    text = message.get('text', '').strip()
                    text_lower = text.lower()

                    if chat_id not in users:
                        users[chat_id] = {
                            'active': True,
                            'alert_counts': {},
                            'settings': {
                                'min_price_drop': DEFAULT_MIN_PRICE_DROP,
                                'max_price_drop': DEFAULT_MAX_PRICE_DROP,
                                'min_oi_drop': DEFAULT_MIN_OI_DROP,
                                'min_24h_trend': DEFAULT_MIN_24H_TREND,
                                'max_24h_trend': DEFAULT_MAX_24H_TREND,
                                'min_24h_volume': DEFAULT_MIN_24H_VOLUME_USDT,
                                'only_negative_funding': DEFAULT_FUNDING_FILTER_ONLY_NEGATIVE
                            }
                        }

                    url_send = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

                    if text_lower == '/start':
                        help_text = (
                            f"✅ <b>Торговый бот Testnet запущен и работает!</b>\n\n"
                            f"⚙️ <b>Режим торговли:</b>\n"
                            f"• Демо-счет Bybit (Testnet)\n"
                            f"• Направление: Short\n"
                            f"• Объем: $100 | Плечо: 1x\n"
                            f"• Тейк-профит: 0.25% | Стоп-лосс: нет\n"
                            f"• Максимум позиций: 2\n\n"
                            f"Команды: /settings, /stats"
                        )
                        session.post(url_send, json={'chat_id': chat_id, 'text': help_text, 'parse_mode': 'HTML'})

                    elif text_lower == '/settings':
                        st = get_user_settings(chat_id)
                        vol_m = st['min_24h_volume'] / 1_000_000
                        settings_text = (
                            f"⚙️ <b>Текущие настройки скринера:</b>\n\n"
                            f"📉 <b>Падение цены от High (5м):</b> от <code>{st['min_price_drop']}%</code> до <code>{st['max_price_drop']}%</code>\n"
                            f"📊 <b>Падение ОИ от High (5м):</b> порог <code>{st.get('min_oi_drop', DEFAULT_MIN_OI_DROP)}%</code>\n"
                            f"💵 <b>Мин. объем 24h:</b> <code>${vol_m:.2f}M</code>"
                        )
                        session.post(url_send, json={'chat_id': chat_id, 'text': settings_text, 'parse_mode': 'HTML'})
            time.sleep(1)
        except Exception:
            time.sleep(3)


def check_and_reset_at_midnight():
    now = get_ye_time()
    reset_time = now.replace(hour=5, minute=0, second=0, microsecond=0)
    if now >= reset_time:
        reset_time += timedelta(days=1)

    while True:
        try:
            now = get_ye_time()
            if now >= reset_time:
                for chat_id in users:
                    users[chat_id]['alert_counts'] = {}
                with data_lock:
                    traded_today_symbols.clear()
                print("🔄 Суточные лимиты и списки сделок сброшены.")
                reset_time += timedelta(days=1)
            time.sleep(30)
        except Exception:
            time.sleep(30)


def fetch_tickers_data():
    url = "https://api.bybit.com/v5/market/tickers"
    params = {"category": "linear"}
    try:
        response = session.get(url, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get('retCode') == 0:
                return data['result']['list']
    except Exception as e:
        print(f"✗ Ошибка обращения к Bybit REST API: {e}")
    return []


def process_market_data(tickers):
    timestamp = int(datetime.now().timestamp())

    with data_lock:
        for ticker in tickers:
            symbol = ticker.get('symbol', '')
            if not symbol.endswith('USDT'):
                continue

            if symbol in EXCLUDED_STOCKS:
                continue

            try:
                price = float(ticker.get('lastPrice', 0))
                prev_price_24h = float(ticker.get('prevPrice24h', 0))
                volume_24h = float(ticker.get('turnover24h', 0))
                open_interest = float(ticker.get('openInterestValue', 0))
                funding_rate_pct = float(ticker.get('fundingRate', 0)) * 100
            except (ValueError, TypeError):
                continue

            if price <= 0 or prev_price_24h <= 0:
                continue

            trend_24h_pct = calculate_change(prev_price_24h, price)

            if symbol not in historical_data:
                historical_data[symbol] = {'price': [], 'oi': []}

            data = historical_data[symbol]
            data['price'].append({'value': price, 'timestamp': timestamp})
            if open_interest > 0:
                data['oi'].append({'value': open_interest, 'timestamp': timestamp})

            data['price'] = [x for x in data['price'] if timestamp - x['timestamp'] <= TIME_WINDOW]
            data['oi'] = [x for x in data['oi'] if timestamp - x['timestamp'] <= TIME_WINDOW]

            if len(data['price']) > 1 and len(data['oi']) > 1:
                max_price = max(x['value'] for x in data['price'])
                price_drop = calculate_change(max_price, price)

                max_oi = max(x['value'] for x in data['oi'])
                current_oi = data['oi'][-1]['value']
                oi_drop_from_high_pct = calculate_change(max_oi, current_oi)

                last_time = last_alert_time.get(symbol, 0)

                for chat_id in list(users.keys()):
                    st = get_user_settings(chat_id)

                    # Условие фандинга: только ПОЛОЖИТЕЛЬНЫЙ (> 0)
                    if funding_rate_pct <= 0:
                        continue

                    if volume_24h < st['min_24h_volume']:
                        continue

                    if not (st['min_24h_trend'] <= trend_24h_pct <= st['max_24h_trend']):
                        continue

                    target_oi_drop = st.get('min_oi_drop', DEFAULT_MIN_OI_DROP)
                    if oi_drop_from_high_pct > target_oi_drop:
                        continue

                    min_d = min(st['min_price_drop'], st['max_price_drop'])
                    max_d = max(st['min_price_drop'], st['max_price_drop'])

                    if min_d <= price_drop <= max_d:
                        if timestamp - last_time >= (COOLDOWN_MINUTES * 60):
                            msg = (
                                f"📉 <b>{symbol}</b>: Слив цены + ОИ от High!\n\n"
                                f"📉 <b>Падение цены от High (5м):</b> <code>{price_drop:.2f}%</code>\n"
                                f"📊 <b>Падение ОИ от High (5м):</b> <code>{oi_drop_from_high_pct:.2f}%</code>\n"
                                f"💸 <b>Фандинг (Bybit):</b> <code>+{funding_rate_pct:.4f}%</code>\n"
                                f"📊 <b>Тренд 24h:</b> <code>{trend_24h_pct:.2f}%</code>\n"
                                f"💵 <b>Объем 24h:</b> <code>${volume_24h/1_000_000:.2f}M</code>"
                            )

                            threading.Thread(
                                target=send_telegram_notification,
                                args=(chat_id, msg, symbol),
                                daemon=True
                            ).start()

                            last_alert_time[symbol] = timestamp

                            # ПРОВЕРКА УСЛОВИЙ ДЛЯ ТОРГОВЛИ:
                            # 1. За текущий день по сигналу пришло максимум до 3 уведомлений (1, 2 или 3)
                            current_alert_count = get_alert_count(chat_id, symbol)
                            if 1 <= current_alert_count <= 3:
                                # 2. По этой монете сделка сегодня еще не открывалась
                                if symbol not in traded_today_symbols:
                                    threading.Thread(
                                        target=open_short_position,
                                        args=(symbol, price),
                                        daemon=True
                                    ).start()


def main():
    print("=== Запуск Торгового Бот-Скринера (Demo Testnet Bybit) ===")
    send_log_to_telegram("Бот начал работу, всё хорошо! 🚀")

    threading.Thread(target=handle_telegram_updates, daemon=True).start()
    threading.Thread(target=check_and_reset_at_midnight, daemon=True).start()
    threading.Thread(target=monitor_positions_and_pnl, daemon=True).start()

    while True:
        start_time = time.time()
        
        tickers = fetch_tickers_data()
        if tickers:
            process_market_data(tickers)

        elapsed = time.time() - start_time
        sleep_time = max(1, POLL_INTERVAL - elapsed)
        time.sleep(sleep_time)


if __name__ == "__main__":
    main()
