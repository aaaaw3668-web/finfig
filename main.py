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

# Константы и глобальное состояние
FUNDING_THRESHOLD_PCT = -0.5      # Значение по умолчанию
CHECK_INTERVAL_SECONDS = 30       # Интервал проверки (каждые полминуты)
ALERT_COOLDOWN_SECONDS = 3600     # Кулдаун на повторное уведомление (1 час)

LAST_CHAT_ID = None

# Кэш отправленных уведомлений: symbol -> timestamp
sent_alerts = {}

# Инициализация CCXT клиента Bybit
bybit = ccxt.bybit({'enableRateLimit': True})


def parse_symbol(symbol_str: str) -> str:
    """Приводит тикер к стандартизированному виду (например, BTC/USDT)"""
    if not symbol_str:
        return ""
    return symbol_str.split(':')[0]


def get_minutes_to_next_funding(ticker_data: dict) -> float:
    """Возвращает количество минут, оставшееся до следующей выплаты фандинга."""
    next_time = ticker_data.get('fundingTimestamp') or ticker_data.get('nextFundingTime')
    if not next_time:
        return 0.0
    
    now_ms = time.time() * 1000
    diff_ms = next_time - now_ms
    return max(0.0, diff_ms / (1000 * 60))


async def fetch_extreme_funding():
    """Сканирует Bybit на предмет фандинга <= FUNDING_THRESHOLD_PCT"""
    try:
        tickers = await bybit.fetch_funding_rates()
        extreme_funding_list = []
        
        for sym, data in tickers.items():
            base_sym = parse_symbol(sym)
            rate = data.get('fundingRate')
            
            if base_sym.endswith('/USDT') and rate is not None:
                rate_pct = float(rate) * 100
                
                # Фильтр по динамическому порогу
                if rate_pct <= FUNDING_THRESHOLD_PCT:
                    mins_left = get_minutes_to_next_funding(data)
                    extreme_funding_list.append({
                        'symbol': base_sym,
                        'rate': rate_pct,
                        'mins_left': mins_left
                    })
                    
        return extreme_funding_list
    except Exception as e:
        logger.error(f"Ошибка при запросе данных с Bybit: {e}")
        return []


def filter_duplicate_alerts(alerts):
    """Фильтрация повторных одинаковых уведомлений с учетом кулдауна"""
    now = time.time()
    fresh_alerts = []

    # Очистка устаревших записей
    expired_keys = [k for k, timestamp in sent_alerts.items() if now - timestamp > ALERT_COOLDOWN_SECONDS]
    for k in expired_keys:
        del sent_alerts[k]

    for alert in alerts:
        symbol = alert['symbol']
        if symbol not in sent_alerts:
            sent_alerts[symbol] = now
            fresh_alerts.append(alert)

    return fresh_alerts


async def background_scanner(application):
    """Фоновый цикл проверки экстремального фандинга"""
    await asyncio.sleep(3)
    
    while True:
        logger.info(f"Сканирование фандинга Bybit (порог <= {FUNDING_THRESHOLD_PCT}%)...")
        alerts = await fetch_extreme_funding()
        
        if alerts:
            # Сортируем от самого низкого (самого отрицательного)
            alerts.sort(key=lambda x: x['rate'])
            
            new_alerts = filter_duplicate_alerts(alerts)

            if new_alerts and LAST_CHAT_ID:
                msg = f"🚨 **Отрицательный фандинг на Bybit!** (≤ {FUNDING_THRESHOLD_PCT}%)\n\n"
                
                for item in new_alerts:
                    msg += (
                        f"🔹 **{item['symbol']}**\n"
                        f"📉 Ставка: `{item['rate']:.4f}%`\n"
                        f"⏳ До выплаты: ~`{item['mins_left']:.0f} мин`\n\n"
                    )

                try:
                    await application.bot.send_message(chat_id=LAST_CHAT_ID, text=msg, parse_mode="Markdown")
                except Exception as e:
                    logger.error(f"Не удалось отправить уведомление в Telegram: {e}")

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LAST_CHAT_ID
    LAST_CHAT_ID = update.effective_chat.id
    await update.message.reply_text(
        "🤖 **Бот отслеживания фандинга Bybit запущен!**\n\n"
        f"Текущий порог фандинга: **≤ {FUNDING_THRESHOLD_PCT}%**\n\n"
        "💡 Для изменения порога используйте команду:\n"
        "`/set -0.8` или `/set -1`",
        parse_mode="Markdown"
    )


async def set_funding_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для установки нового порога фандинга."""
    global FUNDING_THRESHOLD_PCT
    
    if not context.args:
        await update.message.reply_text(
            f"⚠️ Пожалуйста, укажите значение порога в процентах.\n\n"
            f"Текущий порог: **{FUNDING_THRESHOLD_PCT}%**\n"
            f"Пример использования: `/set -0.8` или `/set -1.5`",
            parse_mode="Markdown"
        )
        return

    try:
        raw_val = context.args[0].replace(',', '.')
        new_val = float(raw_val)

        # Если пользователь ввел положительное число, конвертируем в отрицательное (или оставляем проверку на <=)
        if new_val > 0:
            new_val = -new_val

        FUNDING_THRESHOLD_PCT = new_val
        sent_alerts.clear()  # Сбрасываем кэш, чтобы заново прийти уведомления по новому порогу
        
        await update.message.reply_text(
            f"✅ Порог фандинга успешно изменён на **≤ {FUNDING_THRESHOLD_PCT}%**\n"
            f"Кэш ранее отправленных уведомлений сброшен.",
            parse_mode="Markdown"
        )
    except ValueError:
        await update.message.reply_text(
            "❌ Ошибка: Укажите корректное число. Пример: `/set -0.5`",
            parse_mode="Markdown"
        )


async def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Регистрация команд
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("set", set_funding_command))

    # Запуск фонового сканера
    asyncio.create_task(background_scanner(app))

    print("Бот запущен...")
    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        
        stop_signal = asyncio.Event()
        await stop_signal.wait()
    finally:
        await bybit.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен.")
