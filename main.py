import os
import asyncio
import logging
import time
import ccxt.async_support as ccxt
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
if not TELEGRAM_BOT_TOKEN:
    print("✗ Ошибка: TELEGRAM_BOT_TOKEN не найден в переменных окружения!")
    exit(1)

# ==================== НАСТРОЙКИ ПО УМОЛЧАНИЮ ====================
FUNDING_THRESHOLD_PCT = -0.5        # Порог отрицательного фандинга
DEFAULT_MIN_OI_GROWTH = 1.0         # Минимальный рост ОИ за 5м в % (положительный)
DEFAULT_MIN_24H_TREND = 0.0         # Нижняя граница положительного суточного тренда 24h
DEFAULT_MAX_24H_TREND = 10.0        # Верхняя граница суточного тренда 24h

CHECK_INTERVAL_SECONDS = 10         # Частота сканирования рынка
ALERT_COOLDOWN_SECONDS = 600        # Кулдаун на повторный алерт по монете (10 минут)
TIME_WINDOW = 300                   # Окно анализа: 5 минут (300 сек)

LAST_CHAT_ID = None

# Хранилище исторических данных для расчета роста ОИ: symbol -> {'oi': [...]}
historical_data = {}
sent_alerts = {}  # symbol -> timestamp последнего алерта

# Инициализация CCXT клиента только для Bybit
exchange = ccxt.bybit({'enableRateLimit': True})


def parse_symbol(symbol_str: str) -> str:
    """Приводит тикер к виду без слэша (например, BTCUSDT)"""
    if not symbol_str:
        return ""
    clean_sym = symbol_str.split(':')[0]
    return clean_sym.replace('/', '')


def calculate_change(old, new):
    if old == 0:
        return 0.0
    return ((new - old) / old) * 100


def get_minutes_to_next_funding(ticker_data: dict) -> float:
    next_time = ticker_data.get('fundingTimestamp') or ticker_data.get('nextFundingTime')
    if not next_time:
        return 0.0
    now_ms = time.time() * 1000
    diff_ms = next_time - now_ms
    return max(0.0, diff_ms / (1000 * 60))


def filter_duplicate_alert(symbol):
    """Проверка кулдауна на повторный алерт для монеты"""
    now = time.time()
    if symbol in sent_alerts:
        if now - sent_alerts[symbol] < ALERT_COOLDOWN_SECONDS:
            return False
    sent_alerts[symbol] = now
    return True


async def background_scanner(application):
    """Фоновый цикл проверки рынка Bybit (Положительный тренд 24h, Рост ОИ за 5м, Фандинг)"""
    await asyncio.sleep(3)
    
    while True:
        try:
            logger.info("Сканирование рынка Bybit (Положительный тренд + Рост ОИ + Фандинг)...")
            
            # Загружаем тикеры
            tickers = await exchange.fetch_tickers()
            # Загружаем данные по открытому интересу
            try:
                open_interests = await exchange.fetch_open_interests()
            except Exception:
                open_interests = {}

            timestamp = int(time.time())

            for sym, ticker in tickers.items():
                if 'USDT' not in sym or ':' in sym:
                    continue
                
                base_sym = parse_symbol(sym)
                price = ticker.get('last')
                trend_24h_pct = ticker.get('percentage')
                funding_rate = ticker.get('fundingRate')
                
                if not price or price <= 0:
                    continue

                # Расчет тренда 24h в процентах, если percentage недоступен
                if trend_24h_pct is None:
                    prev_p = ticker.get('previousClose')
                    trend_24h_pct = calculate_change(prev_p, price) if prev_p else 0.0

                funding_rate_pct = float(funding_rate) * 100 if funding_rate is not None else 0.0

                # 1. Фильтр по отрицательному фандингу
                if funding_rate_pct > FUNDING_THRESHOLD_PCT:
                    continue

                # 2. Фильтр по ПОЛОЖИТЕЛЬНОМУ суточному тренду
                if not (DEFAULT_MIN_24H_TREND <= trend_24h_pct <= DEFAULT_MAX_24H_TREND):
                    continue

                # Получаем текущий Open Interest в долларах/монетах
                oi_data = open_interests.get(sym, {})
                open_interest = oi_data.get('openInterestValue') or oi_data.get('openInterestAmount', 0) * price

                if open_interest <= 0:
                    continue

                # Инициализация истории для монеты
                if base_sym not in historical_data:
                    historical_data[base_sym] = {'oi': []}

                data = historical_data[base_sym]
                data['oi'].append({'value': open_interest, 'timestamp': timestamp})

                # Очищаем данные за пределами 5-минутного окна
                data['oi'] = [x for x in data['oi'] if timestamp - x['timestamp'] <= TIME_WINDOW]

                if len(data['oi']) > 1:
                    # Расчет РОСТА ОИ от минимального значения за 5м (положительный прирост)
                    min_oi = min(x['value'] for x in data['oi'])
                    current_oi = data['oi'][-1]['value']
                    oi_growth_pct = calculate_change(min_oi, current_oi)

                    # 3. Условие ПОЛОЖИТЕЛЬНОГО роста ОИ за 5м
                    if oi_growth_pct >= DEFAULT_MIN_OI_GROWTH:
                        if filter_duplicate_alert(base_sym) and LAST_CHAT_ID:
                            mins_left = get_minutes_to_next_funding(ticker)
                            msg = (
                                f"🚀 **{base_sym}**: Рост ОИ + Положительный тренд!\n\n"
                                f"🔹 Биржа: **Bybit**\n"
                                f"📊 Рост ОИ (5м): `+{oi_growth_pct:.2f}%`\n"
                                f"📈 Тренд 24h: `+{trend_24h_pct:.2f}%`\n"
                                f"💸 Ставка фандинга: `{funding_rate_pct:.4f}%`\n"
                                f"⏳ До выплаты: ~`{mins_left:.0f} мин`"
                            )
                            try:
                                await application.bot.send_message(chat_id=LAST_CHAT_ID, text=msg, parse_mode="Markdown")
                            except Exception as e:
                                logger.error(f"Не удалось отправить уведомление в Telegram: {e}")

        except Exception as e:
            logger.error(f"Ошибка в фоновом сканере Bybit: {e}")

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LAST_CHAT_ID
    LAST_CHAT_ID = update.effective_chat.id
    await update.message.reply_text(
        "🤖 **Бот мониторинга Bybit (Рост ОИ + Положительный тренд + Фандинг) запущен!**\n\n"
        f"Текущий порог фандинга: **≤ {FUNDING_THRESHOLD_PCT}%**\n"
        f"Мин. рост ОИ (5м): **≥ +{DEFAULT_MIN_OI_GROWTH}%**\n"
        f"Тренд 24h: от **+{DEFAULT_MIN_24H_TREND}%** до **+{DEFAULT_MAX_24H_TREND}%**\n\n"
        "💡 Для изменения порога фандинга используйте команду:\n"
        "`/set -0.8`",
        parse_mode="Markdown"
    )


async def set_funding_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global FUNDING_THRESHOLD_PCT
    if not context.args:
        await update.message.reply_text(
            f"⚠️ Укажите значение порога.\nТекущий порог: **{FUNDING_THRESHOLD_PCT}%**\nПример: `/set -0.8`",
            parse_mode="Markdown"
        )
        return

    try:
        raw_val = context.args[0].replace(',', '.')
        new_val = float(raw_val)
        if new_val > 0:
            new_val = -new_val

        FUNDING_THRESHOLD_PCT = new_val
        sent_alerts.clear()
        
        await update.message.reply_text(
            f"✅ Порог фандинга изменён на **≤ {FUNDING_THRESHOLD_PCT}%**",
            parse_mode="Markdown"
        )
    except ValueError:
        await update.message.reply_text("❌ Ошибка: укажите число. Пример: `/set -0.5`", parse_mode="Markdown")


async def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("set", set_funding_command))

    asyncio.create_task(background_scanner(app))

    print("Бот запущен...")
    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        
        stop_signal = asyncio.Event()
        await stop_signal.wait()
    finally:
        await exchange.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен.")
