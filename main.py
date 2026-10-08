import os
import asyncio
import logging
import ccxt.async_support as ccxt
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

==================== НАСТРОЙКИ (ПО УМОЛЧАНИЮ) ====================
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
if not TELEGRAM_BOT_TOKEN:
    print("✗ Ошибка: TELEGRAM_BOT_TOKEN не найден в переменных окружения!")
    exit(1)

# Глобальная переменная для минимального спреда (в процентах), которую можно менять через /set
MIN_SPREAD_PCT = 0.5  
CHECK_INTERVAL_SECONDS = 300  # Проверка каждые 5 минут

# Инициализация асинхронных клиентов бирж
bybit = ccxt.bybit({'enableRateLimit': True})
bingx = ccxt.bingx({'enableRateLimit': True})


async def fetch_funding_rates():
    """Собирает фандинг-рейты с Bybit и BingX и находит пересекающиеся пары"""
    try:
        await bybit.load_markets()
        await bingx.load_markets()

        bybit_tickers = await bybit.fetch_funding_rates()
        bingx_tickers = await bingx.fetch_funding_rates()

        opportunities = []

        # Анализируем фьючерсы (ищем монеты с суффиксом USDT или общим названием)
        for symbol, b_data in bybit_tickers.items():
            if not symbol.endswith('USDT'):
                continue
            
            # Приводим названия к единому виду (например, BTC/USDT:USDT -> BTC/USDT)
            base_symbol = symbol.split(':')[0]
            
            # Ищем соответствующий символ на BingX
            bingx_match = None
            for bx_sym, bx_data in bingx_tickers.items():
                if bx_sym.split(':')[0] == base_symbol:
                    bingx_match = bx_data
                    break
            
            if bingx_match and b_data.get('fundingRate') is not None and bingx_match.get('fundingRate') is not None:
                rate_bybit = float(b_data['fundingRate']) * 100   выражаем в %
                rate_bingx = float(bingx_match['fundingRate']) * 100

                spread = abs(rate_bybit - rate_bingx)

                if spread >= MIN_SPREAD_PCT:
                    # Определяем, где платить будут нам (где фандинг более отрицательный или меньше положительного)
                    opportunities.append({
                        'symbol': base_symbol,
                        'bybit': rate_bybit,
                        'bingx': rate_bingx,
                        'spread': spread
                    })

        return opportunities
    except Exception as e:
        logger.error(f"Ошибка при запросе фандинга: {e}")
        return []


async def background_scanner(application):
    """Фоновый цикл проверки спреда фандинга"""
    # Ждем пару секунд после старта бота
    await asyncio.sleep(5)
    
    while True:
        logger.info("Запуск сканирования фандинга...")
        opportunities = await fetch_funding_rates()
        
        if opportunities:
            # Сортируем по убыванию спреда
            opportunities.sort(key=lambda x: x['spread'], reverse=True)
            
            msg = f"🚨 **Найдены арбитражные связки по фандингу!** (Спред >= {MIN_SPREAD_PCT}%)\n\n"
            for opp in opportunities[:5]:  # Отправляем топ-5
                msg += (
                    f"🔹 **{opp['symbol']}**\n"
                    f"• Bybit: `{opp['bybit']:.4f}%`\n"
                    f"• BingX: `{opp['bingx']:.4f}%`\n"
                    f"• **Спред:** `+{opp['spread']:.4f}%`\n\n"
                )
            
            # Рассылаем всем админам или в чаты, где запущен бот (берем из контекста или дефолтный чат)
            # В данном примере бот рассылает в чаты, откуда запрашивали или сохраняли chat_id. 
            # Для простоты скринера можно сохранить ID последнего чата, куда писали боту.
            global LAST_CHAT_ID
            if 'LAST_CHAT_ID' in globals() and LAST_CHAT_ID:
                try:
                    await application.bot.send_message(chat_id=LAST_CHAT_ID, text=msg, parse_mode="Markdown")
                except Exception as e:
                    logger.error(f"Не удалось отправить уведомление: {e}")

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


# Переменная для сохранения чата для рассылки
LAST_CHAT_ID = None

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LAST_CHAT_ID
    LAST_CHAT_ID = update.effective_chat.id
    await update.message.reply_text(
        f"🤖 **Фандинг-скринер запущен!**\n\n"
        f"Текущий порог спреда: `{MIN_SPREAD_PCT}%`\n"
        f"Биржи: Bybit ↔ BingX\n\n"
        f"Чтобы изменить порог, используйте команду:\n`/set 0.8` (где 0.8 — новый % спреда)",
        parse_mode="Markdown"
    )


async def set_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MIN_SPREAD_PCT
    global LAST_CHAT_ID
    LAST_CHAT_ID = update.effective_chat.id

    if not context.args:
        await update.message.reply_text(f"Текущий порог спреда: `{MIN_SPREAD_PCT}%`\nИспользование: `/set 0.5`", parse_mode="Markdown")
        return

    try:
        new_val = float(context.args[0].replace(',', '.'))
        if new_val <= 0:
            await update.message.reply_text("❌ Значение должно быть больше 0.")
            return
        
        MIN_SPREAD_PCT = new_val
        await update.message.reply_text(f"✅ Успешно! Минимальный спред изменен на: `{MIN_SPREAD_PCT}%`", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Неверный формат числа. Пример: `/set 0.6`", parse_mode="Markdown")


async def main():
    # Закрываем биржи корректно при выходе
    try:
        app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

        app.add_handler(CommandHandler("start", start_command))
        app.add_handler(CommandHandler("set", set_command))

        # Запускаем фоновую задачу сканирования параллельно с ботом
        asyncio.create_task(background_scanner(app))

        print("Бот успешно запущен и ожидает команд...")
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        
        # Бесконечный цикл для работы бота
        stop_signal = asyncio.Event()
        await stop_signal.wait()
    finally:
        await bybit.close()
        await bingx.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен.")
