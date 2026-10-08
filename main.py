import os
import asyncio
import logging
import time
from itertools import combinations
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

# Глобальные настройки
MIN_SPREAD_PCT = 0.5              # Минимальный валовый спред %
MAX_MINUTES_TO_FUNDING = 10       # До фандинга должно оставаться не более 30 минут!
CHECK_INTERVAL_SECONDS = 60       # Проверка каждую минуту (чтобы не пропустить 30-минутное окно)
ALERT_COOLDOWN_SECONDS = 1800    # Кулдаун на повторные уведомления (30 минут)

# Настройки комиссий и проскальзывания (в процентах)
# Для арбитража берем Taker-комиссию (обычно ~0.04% - 0.06% за ордер)
# Вход + выход на 2 биржах = 4 ордера (например, 4 * 0.05% = 0.20%)
ESTIMATED_TAKER_FEE_PER_ORDER = 0.05   # 0.05% за 1 ордер
ESTIMATED_SLIPPAGE_PCT = 0.05          # 0.05% суммарное проскальзывание

LAST_CHAT_ID = None

# Кэш отправленных alerts: key = (symbol, exch1, exch2), value = timestamp
sent_alerts = {}

# Инициализация асинхронных клиентов бирж
exchanges = {
    'bybit': ccxt.bybit({'enableRateLimit': True}),
    'bingx': ccxt.bingx({'enableRateLimit': True}),
    'binance': ccxt.binanceusdm({'enableRateLimit': True}),
    'okx': ccxt.okx({'enableRateLimit': True}),
    'bitget': ccxt.bitget({'enableRateLimit': True})
}


def parse_symbol(symbol_str: str) -> str:
    """Приводит тикер к стандартизированному виду (например, BTC/USDT)"""
    if not symbol_str:
        return ""
    return symbol_str.split(':')[0]


def extract_funding_interval_hours(ticker_data: dict, market_data: dict) -> int:
    """
    Извлекает ТОЧНЫЙ фандинг-интервал из метаданных CCXT / рынка биржи.
    Возвращает интервал в часах (1, 4, 8) или None, если не удалось определить.
    """
    # 1. Из тикера CCXT
    interval = ticker_data.get('fundingInterval')
    
    # 2. Из метаданных рынка (marketinfo)
    if interval is None and market_data:
        info = market_data.get('info', {})
        # Каждая биржа может хранить интервал под разными ключами в info
        interval = (
            info.get('fundingInterval') or 
            info.get('fundingRateInterval') or 
            info.get('fundingIntervalHours') or
            info.get('fundingFeeCycle')
        )

    if interval is not None:
        try:
            val = float(interval)
            if 1 <= val <= 24:          # Указано сразу в часах (например, 8, 4, 1)
                return int(val)
            elif 60 <= val <= 1440:     # Указано в минутах (например, 480, 240, 60)
                return int(val // 60)
            elif val > 1440:            # Указано в миллисекундах
                return int(val // (1000 * 3600))
        except (ValueError, TypeError):
            pass

    # Если явного поля нет, стандартный интервал для большинства деривативов — 8 часов
    return 8


def get_minutes_to_next_funding(ticker_data: dict) -> float:
    """Возвращает количество минут, оставшееся до следующей выплаты фандинга."""
    next_time = ticker_data.get('fundingTimestamp') or ticker_data.get('nextFundingTime')
    if not next_time:
        return 999.0  # Неизвестно
    
    now_ms = time.time() * 1000
    diff_ms = next_time - now_ms
    return max(0.0, diff_ms / (1000 * 60))


async def fetch_exchange_rates(ex_name: str, ex_instance):
    """Безопасно загружает фандинги и данные рынков с конкретной биржи"""
    try:
        markets = await ex_instance.load_markets()
        tickers = await ex_instance.fetch_funding_rates()
        result = {}
        
        for sym, data in tickers.items():
            base_sym = parse_symbol(sym)
            if base_sym.endswith('/USDT') and data.get('fundingRate') is not None:
                market_info = markets.get(sym, {})
                rate_pct = float(data['fundingRate']) * 100
                interval_h = extract_funding_interval_hours(data, market_info)
                mins_left = get_minutes_to_next_funding(data)

                result[base_sym] = {
                    'rate': rate_pct,
                    'interval': interval_h,
                    'mins_left': mins_left,
                    'raw': data
                }
        return ex_name, result
    except Exception as e:
        logger.error(f"Ошибка при запросе данных с биржи {ex_name}: {e}")
        return ex_name, {}


async def fetch_all_funding_rates():
    """Собирает фандинг-рейты со всех бирж и рассчитывает Net Yield"""
    tasks = [fetch_exchange_rates(name, ex) for name, ex in exchanges.items()]
    results = await asyncio.gather(*tasks)
    
    data_by_exchange = {name: data for name, data in results}
    opportunities = []

    # Расчет суммарной комиссии: Вход (2 ордера) + Выход (2 ордера) = 4 ордера Taker
    total_trading_fees = ESTIMATED_TAKER_FEE_PER_ORDER * 4  # e.g., 0.20%

    exchange_names = list(exchanges.keys())
    for ex1, ex2 in combinations(exchange_names, 2):
        data1 = data_by_exchange.get(ex1, {})
        data2 = data_by_exchange.get(ex2, {})

        common_symbols = set(data1.keys()) & set(data2.keys())

        for symbol in common_symbols:
            item1 = data1[symbol]
            item2 = data2[symbol]

            # 1. ПРОВЕРКА РЕАЛЬНОГО ИНТЕРВАЛА ФАНДИНГА (должны совпадать!)
            if item1['interval'] != item2['interval']:
                continue

            # 2. ПРОВЕРКА ВРЕМЕНИ ДО СПИСАНИЯ:
            # До списания на ОБЕИХ биржах должно оставаться <= MAX_MINUTES_TO_FUNDING (например, 30 минут)
            if item1['mins_left'] > MAX_MINUTES_TO_FUNDING or item2['mins_left'] > MAX_MINUTES_TO_FUNDING:
                continue

            rate1 = item1['rate']
            rate2 = item2['rate']
            gross_spread = abs(rate1 - rate2)

            if gross_spread >= MIN_SPREAD_PCT:
                # 3. НАПРАВЛЕНИЕ ТОРГОВЛИ:
                # LONG там, где ставка ниже / более отрицательна
                # SHORT там, где ставка выше / более положительна
                if rate1 > rate2:
                    short_ex, short_rate = ex1, rate1
                    long_ex, long_rate = ex2, rate2
                else:
                    short_ex, short_rate = ex2, rate2
                    long_ex, long_rate = ex1, rate1

                # 4. РАСЧЕТ NET YIELD (Чистой доходности)
                estimated_net = gross_spread - total_trading_fees - ESTIMATED_SLIPPAGE_PCT

                # Фильтруем убыточные или нулевые связки после комиссий
                if estimated_net <= 0:
                    continue

                opportunities.append({
                    'symbol': symbol,
                    'long_ex': long_ex.upper(),
                    'long_rate': long_rate,
                    'short_ex': short_ex.upper(),
                    'short_rate': short_rate,
                    'gross_spread': gross_spread,
                    'trading_fees': total_trading_fees,
                    'slippage': ESTIMATED_SLIPPAGE_PCT,
                    'estimated_net': estimated_net,
                    'interval': item1['interval'],
                    'mins_left_1': item1['mins_left'],
                    'mins_left_2': item2['mins_left']
                })

    return opportunities


def filter_duplicate_alerts(opportunities):
    """Фильтрация повторных одинаковых уведомлений"""
    now = time.time()
    fresh_opportunities = []

    expired_keys = [k for k, timestamp in sent_alerts.items() if now - timestamp > ALERT_COOLDOWN_SECONDS]
    for k in expired_keys:
        del sent_alerts[k]

    for opp in opportunities:
        alert_key = (opp['symbol'], opp['long_ex'], opp['short_ex'])

        if alert_key not in sent_alerts:
            sent_alerts[alert_key] = now
            fresh_opportunities.append(opp)

    return fresh_opportunities


async def background_scanner(application):
    """Фоновый цикл проверки спреда фандинга"""
    await asyncio.sleep(5)
    
    while True:
        logger.info("Сканирование фандинга (филигранно по окну ≤ 30 мин)...")
        opportunities = await fetch_all_funding_rates()
        
        if opportunities:
            # Сортируем по чистой доходности (Net Yield)
            opportunities.sort(key=lambda x: x['estimated_net'], reverse=True)
            
            new_opportunities = filter_duplicate_alerts(opportunities)

            if new_opportunities and LAST_CHAT_ID:
                msg = f"🚨 **Арбитражные связки фандинга!** (Окно: ≤{MAX_MINUTES_TO_FUNDING} мин до списания)\n\n"
                
                for opp in new_opportunities[:5]:
                    min_left_str = f"{min(opp['mins_left_1'], opp['mins_left_2']):.0f} мин"
                    msg += (
                        f"🔹 **{opp['symbol']}** | Интервал: `{opp['interval']}ч` | До выплаты: ~`{min_left_str}`\n"
                        f"🟢 **LONG:** {opp['long_ex']} (`{opp['long_rate']:.4f}%`)\n"
                        f"🔴 **SHORT:** {opp['short_ex']} (`{opp['short_rate']:.4f}%`)\n"
                        f"```\n"
                        f"Gross funding: +{opp['gross_spread']:.4f}%\n"
                        f"Trading fees:  -{opp['trading_fees']:.2f}%\n"
                        f"Slippage:      -{opp['slippage']:.2f}%\n"
                        f"----------------------------\n"
                        f"Estimated net: +{opp['estimated_net']:.4f}%\n"
                        f"```\n"
                    )

                try:
                    await application.bot.send_message(chat_id=LAST_CHAT_ID, text=msg, parse_mode="Markdown")
                except Exception as e:
                    logger.error(f"Не удалось отправить уведомление: {e}")

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LAST_CHAT_ID
    LAST_CHAT_ID = update.effective_chat.id
    ex_list = ", ".join([e.upper() for e in exchanges.keys()])
    await update.message.reply_text(
        f"🤖 **Фандинг-скринер с расчетом Net Yield запущен!**\n\n"
        f"• Мин. Валовый спред: `{MIN_SPREAD_PCT}%`\n"
        f"• Время до списания: **≤ {MAX_MINUTES_TO_FUNDING} минут** на обеих биржах\n"
        f"• Отслеживаемые биржи: **{ex_list}**\n\n"
        f"Изменить минимальный спред:\n`/set 0.6`",
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
        await update.message.reply_text(f"✅ Минимальный валовый спред изменен на: `{MIN_SPREAD_PCT}%`", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Неверный формат числа. Пример: `/set 0.6`", parse_mode="Markdown")


async def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("set", set_command))

    asyncio.create_task(background_scanner(app))

    print("Бот запущен...")
    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        
        stop_signal = asyncio.Event()
        await stop_signal.wait()
    finally:
        for name, ex in exchanges.items():
            await ex.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен.")
