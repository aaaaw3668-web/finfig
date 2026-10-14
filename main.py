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
FUNDING_THRESHOLD_PCT = -0.5      # Значение фандинга по умолчанию (%)
MIN_VOLUME_24H = 1_000_000        # Минимальный объем торгов за 24 часа в USDT (по умолчанию 1 млн)
CHECK_INTERVAL_SECONDS = 30       # Интервал проверки (каждые полминуты)
ALERT_COOLDOWN_SECONDS = 3600     # Кулдаун на повторное уведомление (1 час)

LAST_CHAT_ID = None

# Кэш отправленных уведомлений: exchange_symbol -> timestamp
sent_alerts = {}

# Инициализация CCXT клиента для Bybit
exchanges = {
    'Bybit': ccxt.bybit({'enableRateLimit': True})
}


def parse_symbol(symbol_str: str) -> str:
    """Приводит тикер к виду без слэша (например, BTCUSDT)"""
    if not symbol_str:
        return ""
    clean_sym = symbol_str.split(':')[0]
    return clean_sym.replace('/', '')


def get_minutes_to_next_funding(ticker_data: dict) -> float:
    """Возвращает количество минут, оставшееся до следующей выплаты фандинга."""
    next_time = ticker_data.get('fundingTimestamp') or ticker_data.get('nextFundingTime')
    if not next_time:
        return 0.0
    
    now_ms = time.time() * 1000
    diff_ms = next_time - now_ms
    return max(0.0, diff_ms / (1000 * 60))


async def fetch_exchange_funding(exchange_name: str, exchange_client):
    """Сканирует биржу на предмет фандинга и объема торгов"""
    try:
        # Загружаем фандинги и общие тикеры параллельно
        funding_rates, tickers = await asyncio.gather(
            exchange_client.fetch_funding_rates(),
            exchange_client.fetch_tickers()
        )
        
        extreme_funding_list = []
        
        for sym, data in funding_rates.items():
            if 'USDT' in sym:
                base_sym = parse_symbol(sym)
                rate = data.get('fundingRate')
                
                if base_sym.endswith('USDT') and rate is not None:
                    rate_pct = float(rate) * 100
                    
                    # Фильтр по динамическому порогу фандинга
                    if rate_pct <= FUNDING_THRESHOLD_PCT:
                        # Получаем объем торгов за 24 часа из общих тикеров
                        ticker_info = tickers.get(sym) or tickers.get(base_sym) or tickers.get(f"{base_sym}:USDT")
                        volume_24h = 0.0
                        
                        if ticker_info:
                            # quoteVolume — это объем в котируемой валюте (USDT)
                            volume_24h = float(ticker_info.get('quoteVolume') or 0.0)
                            # Защита на случай, если quoteVolume отсутствует, а есть baseVolume и цена
                            if volume_24h == 0.0 and ticker_info.get('baseVolume') and ticker_info.get('close'):
                                volume_24h = float(ticker_info['baseVolume']) * float(ticker_info['close'])

                        # Проверяем фильтр по минимальному объему
                        if volume_24h >= MIN_VOLUME_24H:
                            mins_left = get_minutes_to_next_funding(data)
                            extreme_funding_list.append({
                                'exchange': exchange_name,
                                'symbol': base_sym,
                                'rate': rate_pct,
                                'mins_left': mins_left,
                                'volume': volume_24h
                            })
                    
        return extreme_funding_list
    except Exception as e:
        logger.error(f"Ошибка при запросе данных с {exchange_name}: {e}")
        return []


def filter_duplicate_alerts(alerts):
    """Фильтрация повторных одинаковых уведомлений с учетом кулдауна"""
    now = time.time()
    fresh_alerts = []

    expired_keys = [k for k, timestamp in sent_alerts.items() if now - timestamp > ALERT_COOLDOWN_SECONDS]
    for k in expired_keys:
        del sent_alerts[k]

    for alert in alerts:
        alert_key = f"{alert['exchange']}_{alert['symbol']}"
        if alert_key not in sent_alerts:
            sent_alerts[alert_key] = now
            fresh_alerts.append(alert)

    return fresh_alerts


async def background_scanner(application):
    """Фоновый цикл проверки экстремального фандинга на Bybit"""
    await asyncio.sleep(3)
    
    while True:
        logger.info(f"Сканирование фандинга (<= {FUNDING_THRESHOLD_PCT}%, объём >= {MIN_VOLUME_24H:,.0f} USDT)...")
        
        all_alerts = []
        for name, client in exchanges.items():
            exchange_alerts = await fetch_exchange_funding(name, client)
            all_alerts.extend(exchange_alerts)
        
        if all_alerts:
            all_alerts.sort(key=lambda x: x['rate'])
            new_alerts = filter_duplicate_alerts(all_alerts)

            if new_alerts and LAST_CHAT_ID:
                msg = f"🚨 **Отрицательный фандинг обнаружен!**\n(≤ {FUNDING_THRESHOLD_PCT}%, Объем ≥ {MIN_VOLUME_24H:,.0f}$)\n\n"
                
                for item in new_alerts:
                    msg += (
                        f"🔹 Биржа: **{item['exchange']}**\n"
                        f"🪙 Монета: `{item['symbol']}`\n"
                        f"📉 Ставка: `{item['rate']:.4f}%`\n"
                        f"📊 Объем (24ч): `{item['volume']:,.0f} USDT`\n"
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
        "🌐 Биржа: **Bybit**\n"
        f"• Порог фандинга: **≤ {FUNDING_THRESHOLD_PCT}%**\n"
        f"• Мин. объем (24ч): **{MIN_VOLUME_24H:,.0f} USDT**\n\n"
        "💡 **Команды управления:**\n"
        "▫️ Изменить порог фандинга: `/set -0.8`\n"
        "▫️ Изменить мин. объем: `/setvol 2000000` (для 2 млн)",
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

        if new_val > 0:
            new_val = -new_val

        FUNDING_THRESHOLD_PCT = new_val
        sent_alerts.clear()
        
        await update.message.reply_text(
            f"✅ Порог фандинга изменён на **≤ {FUNDING_THRESHOLD_PCT}%**\n"
            f"Кэш уведомлений сброшен.",
            parse_mode="Markdown"
        )
    except ValueError:
        await update.message.reply_text(
            "❌ Ошибка: Укажите корректное число. Пример: `/set -0.5`",
            parse_mode="Markdown"
        )


async def set_volume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для установки минимального объема за 24 часа."""
    global MIN_VOLUME_24H
    
    if not context.args:
        await update.message.reply_text(
            f"⚠️ Укажите минимальный объем в USDT.\n\n"
            f"Текущий порог: **{MIN_VOLUME_24H:,.0f} USDT**\n"
            f"Пример использования: `/setvol 1000000` (для 1 млн) или `/setvol 500000` (для 500 тыс.)",
            parse_mode="Markdown"
        )
        return

    try:
        raw_val = context.args[0].replace(',', '').replace('_', '')
        new_val = float(raw_val)

        if new_val < 0:
            await update.message.reply_text("❌ Объем не может быть отрицательным.")
            return

        MIN_VOLUME_24H = new_val
        sent_alerts.clear()
        
        await update.message.reply_text(
            f"✅ Минимальный объем за 24ч изменён на **{MIN_VOLUME_24H:,.0f} USDT**\n"
            f"Кэш уведомлений сброшен.",
            parse_mode="Markdown"
        )
    except ValueError:
        await update.message.reply_text(
            "❌ Ошибка: Укажите корректное число. Пример: `/setvol 1500000`",
            parse_mode="Markdown"
        )


async def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Регистрация команд
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("set", set_funding_command))
    app.add_handler(CommandHandler("setvol", set_volume_command))

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
        for client in exchanges.values():
            await client.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен.")
