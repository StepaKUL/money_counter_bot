import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
    PicklePersistence,
)

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Timezone
TZ = ZoneInfo("Asia/Yekaterinburg")

# Stages
START_ROUTES, GET_AMOUNT, GET_DESCRIPTION = range(3)
# Callback data
ADD_INCOME, ADD_EXPENSE = 'add_income', 'add_expense'

def get_user_stats(user_data):
    """Calculates and returns financial statistics for the user for today."""
    today_str = datetime.now(TZ).strftime("%Y-%m-%d")
    
    # Initialize data for new user or new day
    if 'last_update' not in user_data or user_data.get('last_update') != today_str:
        # Calculate previous day's end balance
        end_of_yesterday_balance = user_data.get('balance_end_day', 0.0)
        user_data['balance_start_day'] = end_of_yesterday_balance
        user_data['transactions_today'] = []
        user_data['last_update'] = today_str

    transactions = user_data.get('transactions_today', [])
    
    total_income = sum(t['amount'] for t in transactions if t['type'] == 'income')
    total_expense = sum(t['amount'] for t in transactions if t['type'] == 'expense')
    
    balance_start = user_data.get('balance_start_day', 0.0)
    balance_end = balance_start + total_income - total_expense
    user_data['balance_end_day'] = balance_end # Save for tomorrow

    return {
        "date": datetime.now(TZ).strftime("%d %B %Y"),
        "balance_start": balance_start,
        "total_income": total_income,
        "total_expense": total_expense,
        "balance_end": balance_end,
        "transactions": transactions
    }

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Display main menu and handle daily balance updates."""
    user = update.effective_user
    stats = get_user_stats(context.user_data)

    text = (
        f"Привет, {user.mention_html()}!\n\n"
        f"📅 **Сегодня: {stats['date']}**\n"
        f"💰 **На начало дня:** {stats['balance_start']:.2f}\n"
        f"📈 **Доходы:** +{stats['total_income']:.2f}\n"
        f"📉 **Расходы:** -{stats['total_expense']:.2f}\n"
        f"🏦 **На конец дня:** {stats['balance_end']:.2f}\n\n"
        f"**Последние операции:**\n"
    )

    if not stats['transactions']:
        text += "_Пока нет операций за сегодня._\n"
    else:
        for t in stats['transactions'][-5:]:
            op_type = "✅" if t['type'] == 'income' else '🔻'
            text += f"{op_type} {t['amount']:.2f} - {t.get('description', 'без описания')}\n"

    keyboard = [
        [InlineKeyboardButton("➕ Добавить доход", callback_data=ADD_INCOME)],
        [InlineKeyboardButton("➖ Добавить расход", callback_data=ADD_EXPENSE)],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    message_id = context.user_data.get('message_id')
    if update.callback_query or not message_id:
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                text=text, reply_markup=reply_markup, parse_mode='HTML'
            )
        else: # This branch is for initial /start or our custom call
            if update.message:
                message = await update.message.reply_html(text, reply_markup=reply_markup)
            else: # Fallback for calls without a message to reply to (e.g., after saving a transaction)
                message = await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode='HTML'
                )
            context.user_data['message_id'] = message.message_id
    else: # Edit existing message on /start command
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        except Exception:
            message = await update.message.reply_html(text, reply_markup=reply_markup)
            context.user_data['message_id'] = message.message_id

    return START_ROUTES


async def ask_for_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Asks for the transaction amount."""
    query = update.callback_query
    await query.answer()
    
    transaction_type = 'income' if query.data == ADD_INCOME else 'expense'
    context.user_data['current_transaction'] = {'type': transaction_type}
    
    type_text = 'дохода' if transaction_type == 'income' else 'расхода'
    await query.edit_message_text(text=f"Введите сумму {type_text}:")
    
    return GET_AMOUNT

async def ask_for_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores the amount and asks for a description."""
    try:
        amount = float(update.message.text)
        context.user_data['current_transaction']['amount'] = amount
    except (ValueError, TypeError):
        await update.message.reply_text("Это не похоже на число. Пожалуйста, введите сумму еще раз.")
        return GET_AMOUNT

    await update.message.reply_text("Теперь введите краткое описание:")
    return GET_DESCRIPTION

async def save_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Saves the complete transaction, cleans up messages, and shows a new main menu."""
    description = update.message.text
    transaction = context.user_data['current_transaction']
    transaction['description'] = description
    transaction['date'] = datetime.now(TZ).isoformat()

    if 'transactions_today' not in context.user_data:
        context.user_data['transactions_today'] = []
        
    context.user_data['transactions_today'].append(transaction)
    
    # --- Clean up --- 
    # Delete old main menu
    old_menu_id = context.user_data.pop('message_id', None)
    if old_menu_id:
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=old_menu_id)
        except Exception as e:
            logger.info(f"Could not delete old menu message: {e}")

    # Delete user's amount message, bot's description prompt, user's description message
    # This is a bit tricky as we don't store all these IDs. A simpler approach for now
    # is to just send a new menu and not worry about deleting user messages.

    context.user_data.pop('current_transaction', None)

    # --- Send new menu --- 
    # We need to create a new update object for start() because the current one is a text message
    # and we want start() to send a new message, not edit one.
    class MockUpdate:
        def __init__(self, effective_chat, effective_user):
            self.effective_chat = effective_chat
            self.effective_user = effective_user
            self.callback_query = None
            self.message = None # This will force start() to send a new message

    mock_update = MockUpdate(update.effective_chat, update.effective_user)
    return await start(mock_update, context)



def main() -> None:
    """Run the bot."""
    # Create the Application and pass it your bot's token.
    # Read token from environment variable or local file
    token = os.getenv('BOT_TOKEN')
    if not token:
        try:
            with open('bot_token.txt', 'r') as f:
                token = f.read().strip()
        except FileNotFoundError:
            print("Ошибка: Не найден токен. Создайте переменную окружения BOT_TOKEN или файл bot_token.txt.")
            return

    # Set up persistence
    persistence = PicklePersistence(filepath="bot_data.pickle")

    application = Application.builder().token(token).persistence(persistence).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            START_ROUTES: [
                CallbackQueryHandler(ask_for_amount, pattern=f"^({ADD_INCOME}|{ADD_EXPENSE})$"),
            ],
            GET_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_for_description)
            ],
            GET_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_transaction)
            ],
        },
        fallbacks=[CommandHandler("start", start)],
    )

    application.add_handler(conv_handler)

    # Run the bot until the user presses Ctrl-C
    application.run_polling()


if __name__ == "__main__":
    main()
