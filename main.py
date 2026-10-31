import json
import os
import re
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
import requests

# ==================== НАСТРОЙКИ (ПО УМОЛЧАНИЮ) ====================
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
if not TELEGRAM_BOT_TOKEN:
    print("✗ Ошибка: TELEGRAM_BOT_TOKEN не найден в переменных окружения!")
    exit(1)

# --- Настройки условия сигнала по умолчанию ---
DEFAULT_MIN_PRICE_RISE = 15
DEFAULT_MAX_PRICE_RISE = 100

# --- Настройки диапазона тренда (24h) по умолчанию ---
DEFAULT_MIN_24H_TREND = -100
DEFAULT_MAX_24H_TREND = -10

# --- Фильтр по суточному объему (в USDT) по умолчанию ---
DEFAULT_MIN_24H_VOLUME_USDT = 100_000

# --- Фильтр отрицательного фандинга по умолчанию ---
DEFAULT_FUNDING_FILTER_ONLY_NEGATIVE = False

# --- Параметры опроса API и контроля ---
TIME_WINDOW = 60 * 180
POLL_INTERVAL = 5
COOLDOWN_MINUTES = 60
DAILY_ALERT_LIMIT = 100

session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
session.mount('https://', adapter)

users = {
    '5296533274': {
        'active': True,
        'alert_counts': {},
        'settings': {
            'min_price_rise': DEFAULT_MIN_PRICE_RISE,
            'max_price_rise': DEFAULT_MAX_PRICE_RISE,
            'min_24h_trend': DEFAULT_MIN_24H_TREND,
            'max_24h_trend': DEFAULT_MAX_24H_TREND,
            'min_24h_volume': DEFAULT_MIN_24H_VOLUME_USDT,
            'only_negative_funding': DEFAULT_FUNDING_FILTER_ONLY_NEGATIVE
        }
    }
}

historical_data = {}
last_alert_time = {}
data_lock = threading.Lock()


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def get_ye_time():
    return datetime.now(timezone.utc) + timedelta(hours=5)


def get_user_settings(chat_id):
    if chat_id not in users:
        return {
            'min_price_rise': DEFAULT_MIN_PRICE_RISE,
            'max_price_rise': DEFAULT_MAX_PRICE_RISE,
            'min_24h_trend': DEFAULT_MIN_24H_TREND,
            'max_24h_trend': DEFAULT_MAX_24H_TREND,
            'min_24h_volume': DEFAULT_MIN_24H_VOLUME_USDT,
            'only_negative_funding': DEFAULT_FUNDING_FILTER_ONLY_NEGATIVE
        }

    if 'settings' not in users[chat_id]:
        users[chat_id]['settings'] = {
            'min_price_rise': DEFAULT_MIN_PRICE_RISE,
            'max_price_rise': DEFAULT_MAX_PRICE_RISE,
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


# ==================== ТЕЛЕГРАМ БОТ ====================
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
        print(f"✓ Сигнал роста цены по {symbol} отправлен пользователю {chat_id}")
        return True
    except Exception as e:
        print(f"✗ Ошибка отправки в TG: {repr(e)}")
        return False


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
                                'min_price_rise': DEFAULT_MIN_PRICE_RISE,
                                'max_price_rise': DEFAULT_MAX_PRICE_RISE,
                                'min_24h_trend': DEFAULT_MIN_24H_TREND,
                                'max_24h_trend': DEFAULT_MAX_24H_TREND,
                                'min_24h_volume': DEFAULT_MIN_24H_VOLUME_USDT,
                                'only_negative_funding': DEFAULT_FUNDING_FILTER_ONLY_NEGATIVE
                            }
                        }

                    url_send = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

                    if text_lower == '/start':
                        help_text = (
                            f"✅ <b>Бот мониторинга запущен!</b>\n\n"
                            f"⚙️ <b>Команды управления:</b>\n"
                            f"• /settings — посмотреть текущие настройки\n"
                            f"• /set <code>[параметр] [значение]</code> — изменить настройку\n"
                            f"• /stats — статистика алертов\n\n"
                            f"<b>Доступные параметры для /set:</b>\n"
                            f"• <code>min_rise</code> — минимальный рост цены от Low за 5м (напр. 1.0)\n"
                            f"• <code>max_rise</code> — максимальный рост цены от Low за 5м (напр. 1.5)\n"
                            f"• <code>vol</code> — мин. объем за 24ч в млн $ (напр. 10)\n"
                            f"• <code>min_trend</code> — мин. тренд 24h в % (напр. -10)\n"
                            f"• <code>max_trend</code> — макс. тренд 24h в % (напр. 0)\n"
                            f"• <code>funding</code> — только отрицательный фандинг (<code>on</code> / <code>off</code>)\n\n"
                            f"<i>Примеры:</i>\n"
                            f"<code>/set min_rise 1.0</code>\n"
                            f"<code>/set max_rise 1.5</code>"
                        )
                        session.post(
                            url_send,
                            json={
                                'chat_id': chat_id,
                                'text': help_text,
                                'parse_mode': 'HTML'
                            }
                        )

                    elif text_lower == '/settings':
                        st = get_user_settings(chat_id)
                        vol_m = st['min_24h_volume'] / 1_000_000

                        funding_status = (
                            "🔴 Только отрицательный (< 0%)"
                            if st.get('only_negative_funding', True)
                            else "⚪ Любой фандинг (выкл)"
                        )

                        settings_text = (
                            f"⚙️ <b>Текущие настройки для сигнала:</b>\n\n"
                            f"📈 <b>Рост цены от Low (5м):</b> "
                            f"от <code>{st['min_price_rise']}%</code> "
                            f"до <code>{st['max_price_rise']}%</code>\n"
                            f"📊 <b>Тренд 24h:</b> "
                            f"от <code>{st['min_24h_trend']}%</code> "
                            f"до <code>{st['max_24h_trend']}%</code>\n"
                            f"💵 <b>Мин. объем 24h:</b> <code>${vol_m:.2f}M</code>\n"
                            f"💸 <b>Фильтр фандинга:</b> {funding_status}"
                        )

                        session.post(
                            url_send,
                            json={
                                'chat_id': chat_id,
                                'text': settings_text,
                                'parse_mode': 'HTML'
                            }
                        )

                    elif text_lower.startswith('/set'):
                        parts = text.split()

                        if len(parts) == 3:
                            param = parts[1].lower()
                            val_str = parts[2].lower()
                            st = get_user_settings(chat_id)

                            if param in ['funding', 'funding_filter']:
                                if val_str in ['on', '1', 'true', 'yes', 'вкл']:
                                    st['only_negative_funding'] = True
                                    reply = (
                                        "✅ Фильтр включен: алерты "
                                        "<b>только с отрицательным фандингом (< 0%)</b>."
                                    )
                                elif val_str in ['off', '0', 'false', 'no', 'выкл']:
                                    st['only_negative_funding'] = False
                                    reply = (
                                        "⚪ Фильтр фандинга выключен: "
                                        "алерты приходят при любом фандинге."
                                    )
                                else:
                                    reply = (
                                        "❌ Для фандинга используйте: "
                                        "<code>/set funding on</code> или "
                                        "<code>/set funding off</code>"
                                    )

                            else:
                                try:
                                    val = float(val_str)

                                    if param in ['min_rise', 'min_price_rise']:
                                        st['min_price_rise'] = val
                                        reply = (
                                            f"✅ Минимальный рост цены от Low: "
                                            f"<code>{val}%</code>"
                                        )

                                    elif param in ['max_rise', 'max_price_rise']:
                                        st['max_price_rise'] = val
                                        reply = (
                                            f"✅ Максимальный рост цены от Low: "
                                            f"<code>{val}%</code>"
                                        )

                                    elif param in ['vol', 'volume']:
                                        st['min_24h_volume'] = val * 1_000_000
                                        reply = (
                                            f"✅ Мин. объем 24ч: "
                                            f"<code>${val}M</code> USDT"
                                        )

                                    elif param == 'min_trend':
                                        st['min_24h_trend'] = val
                                        reply = (
                                            f"✅ Нижняя граница тренда 24h: "
                                            f"<code>{val}%</code>"
                                        )

                                    elif param == 'max_trend':
                                        st['max_24h_trend'] = val
                                        reply = (
                                            f"✅ Верхняя граница тренда 24h: "
                                            f"<code>{val}%</code>"
                                        )

                                    else:
                                        reply = "❌ Неизвестный параметр."

                                except ValueError:
                                    reply = "❌ Значение должно быть числом!"

                            session.post(
                                url_send,
                                json={
                                    'chat_id': chat_id,
                                    'text': reply,
                                    'parse_mode': 'HTML'
                                }
                            )

                        else:
                            session.post(
                                url_send,
                                json={
                                    'chat_id': chat_id,
                                    'text': "❌ Формат: <code>/set [параметр] [значение]</code>",
                                    'parse_mode': 'HTML'
                                }
                            )

                    elif text_lower == '/stats':
                        counts = users.get(chat_id, {}).get('alert_counts', {})
                        stats_text = "📊 <b>Статистика алертов за сегодня:</b>\n\n"

                        if counts:
                            for sym, count in sorted(
                                counts.items(),
                                key=lambda x: x[1],
                                reverse=True
                            )[:20]:
                                stats_text += (
                                    f"• <code>{sym}</code>: "
                                    f"{count}/{DAILY_ALERT_LIMIT}\n"
                                )
                        else:
                            stats_text += "Сегодня алертов еще не было."

                        session.post(
                            url_send,
                            json={
                                'chat_id': chat_id,
                                'text': stats_text,
                                'parse_mode': 'HTML'
                            }
                        )

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

                print("🔄 Суточные лимиты сброшены.")
                reset_time += timedelta(days=1)

            time.sleep(30)

        except Exception:
            time.sleep(30)


# ==================== РАБОТА С REST API BYBIT ====================
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

            try:
                price = float(ticker.get('lastPrice', 0))
                prev_price_24h = float(ticker.get('prevPrice24h', 0))
                volume_24h = float(ticker.get('turnover24h', 0))
                funding_rate_pct = float(ticker.get('fundingRate', 0)) * 100

            except (ValueError, TypeError):
                continue

            if price <= 0 or prev_price_24h <= 0:
                continue

            trend_24h_pct = calculate_change(prev_price_24h, price)

            if symbol not in historical_data:
                historical_data[symbol] = {'price': []}

            data = historical_data[symbol]

            data['price'].append({
                'value': price,
                'timestamp': timestamp
            })

            # Очищаем данные за пределами 5-минутного окна
            data['price'] = [
                x for x in data['price']
                if timestamp - x['timestamp'] <= TIME_WINDOW
            ]

            if len(data['price']) > 1:
                # 1. Расчет роста цены от минимума за 5м
                min_price = min(x['value'] for x in data['price'])
                price_rise = calculate_change(min_price, price)

                last_time = last_alert_time.get(symbol, 0)

                for chat_id in list(users.keys()):
                    st = get_user_settings(chat_id)

                    # 1. Фильтр фандинга
                    if (
                        st.get('only_negative_funding', True)
                        and funding_rate_pct >= 0
                    ):
                        continue

                    # 2. Фильтр по объему
                    if volume_24h < st['min_24h_volume']:
                        continue

                    # 3. Фильтр по суточному тренду
                    if not (
                        st['min_24h_trend']
                        <= trend_24h_pct
                        <= st['max_24h_trend']
                    ):
                        continue

                    # 4. Условие роста цены от своего Low
                    min_r = min(
                        st['min_price_rise'],
                        st['max_price_rise']
                    )
                    max_r = max(
                        st['min_price_rise'],
                        st['max_price_rise']
                    )

                    if min_r <= price_rise <= max_r:
                        if timestamp - last_time >= (COOLDOWN_MINUTES * 60):
                            msg = (
                                f"📈 <b>{symbol}</b>: Рост цены от Low!\n\n"
                                f"📈 <b>Рост цены от Low (5м):</b> "
                                f"<code>{price_rise:.2f}%</code>\n"
                                f"💸 <b>Фандинг (Bybit):</b> "
                                f"<code>{funding_rate_pct:.4f}%</code>\n"
                                f"📊 <b>Тренд 24h (Bybit):</b> "
                                f"<code>{trend_24h_pct:.2f}%</code>\n"
                                f"💵 <b>Объем 24h:</b> "
                                f"<code>${volume_24h/1_000_000:.2f}M</code>\n"
                                f"⏱ <b>Окно анализа:</b> 5 мин."
                            )

                            threading.Thread(
                                target=send_telegram_notification,
                                args=(chat_id, msg, symbol),
                                daemon=True
                            ).start()

                            last_alert_time[symbol] = timestamp


# ==================== MAIN LOOP ====================
def main():
    print(
        "=== Запуск REST API Мониторинга "
        "(Рост цены от Low за 5 минут) ==="
    )

    threading.Thread(
        target=handle_telegram_updates,
        daemon=True
    ).start()

    threading.Thread(
        target=check_and_reset_at_midnight,
        daemon=True
    ).start()

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
