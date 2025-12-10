import logging
import os
from dataclasses import dataclass
from typing import Dict

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# ==============================
# CONFIG
# ==============================

BOT_TOKEN = os.environ.get("BOT_TOKEN")  # Token from environment variable

# États pour la conversation de paie
(
    PAY_PROVINCE,
    PAY_GROSS,
    PAY_FREQUENCY,
) = range(3)

# États pour relevé bancaire
(
    STMT_WAIT_TEXT,
) = range(10, 11)


# ==============================
# LOGGING
# ==============================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ==============================
# MODÈLES & LOGIQUE DE CALCUL
# ==============================

@dataclass
class PayrollResult:
    province: str
    gross: float
    frequency: str
    federal_tax: float
    provincial_tax: float
    ei: float
    pension: float
    net: float


# Taux très simplifiés, juste pour exemple – à ajuster avec un vrai barème si tu veux
FEDERAL_TAX_RATE = 0.15  # 15% simplifié

PROVINCIAL_TAX_RATES: Dict[str, float] = {
    "QC": 0.15,
    "ON": 0.0505,
    "BC": 0.05,
    "AB": 0.10,
    "MB": 0.108,
    "NB": 0.0968,
    "NS": 0.0879,
    "SK": 0.1075,
    "NL": 0.087,
    "PE": 0.098,
}

EI_RATE = 0.0166  # Assurance emploi (approx)
CPP_RATE = 0.0595  # Régime de pensions du Canada (hors QC)
QPP_RATE = 0.064  # Régime de rentes du Québec (approx)


def calculate_payroll(province: str, gross: float, frequency: str) -> PayrollResult:
    """
    Calcul simplifié paie par période.
    province: QC, ON, etc.
    gross: salaire brut pour la période (ex: 2000$ aux deux semaines)
    frequency: weekly, biweekly, monthly (utilisé seulement pour l'affichage ici)
    """

    prov = province.upper()
    prov_rate = PROVINCIAL_TAX_RATES.get(prov, 0.07)  # défaut 7%

    # Impôts
    federal_tax = gross * FEDERAL_TAX_RATE
    provincial_tax = gross * prov_rate

    # AE et Régime de pension (CPP ou QPP)
    ei = gross * EI_RATE
    if prov == "QC":
        pension = gross * QPP_RATE
    else:
        pension = gross * CPP_RATE

    total_deductions = federal_tax + provincial_tax + ei + pension
    net = gross - total_deductions

    return PayrollResult(
        province=prov,
        gross=gross,
        frequency=frequency,
        federal_tax=federal_tax,
        provincial_tax=provincial_tax,
        ei=ei,
        pension=pension,
        net=net,
    )


def analyze_bank_statement_from_text(text: str) -> Dict[str, float]:
    """
    Analyse très simple d'un relevé bancaire en texte.
    Format attendu (souple) : une ligne par transaction contenant un montant.
    Exemple de lignes :
        2025-12-01, DEP, +1500.25, Salaire
        2025-12-02, -45.80, Restaurant
    On cherche le premier nombre dans chaque ligne, avec + ou -.
    """
    import re

    income = 0.0
    expenses = 0.0
    count_income = 0
    count_expense = 0

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        # Regex pour trouver un nombre avec éventuellement + ou - et décimales
        match = re.search(r"([-+]?\d+(?:[.,]\d+)?)", line)
        if not match:
            continue

        raw = match.group(1).replace(",", ".")
        try:
            amount = float(raw)
        except ValueError:
            continue

        if amount > 0:
            income += amount
            count_income += 1
        else:
            expenses += amount
            count_expense += 1

    balance_change = income + expenses

    return {
        "income": income,
        "expenses": expenses,
        "count_income": count_income,
        "count_expense": count_expense,
        "balance_change": balance_change,
    }


# ==============================
# HANDLERS COMMANDES
# ==============================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (
        f"Salut {user.first_name or ''} 👋\n\n"
        "Je suis ton bot de **calcul de paie** et **analyse de relevé bancaire**.\n\n"
        "📌 Commandes principales :\n"
        "• /payroll – calculer un talon de paie simplifié par province\n"
        "• /statement – analyser un relevé bancaire (texte ou CSV simple)\n"
        "• /help – voir toutes les options\n\n"
        "⚠️ *Attention : les calculs sont approximatifs et à titre informatif seulement.*"
    )
    await update.message.reply_markdown(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🧾 **Fonctions du bot**\n\n"
        "1️⃣ /payroll – Calcul de paie simplifié\n"
        "   - Choisis la province\n"
        "   - Entres le salaire brut pour la période\n"
        "   - Le bot calcule impôts, AE, pension et net\n\n"
        "2️⃣ /statement – Analyse de relevé bancaire\n"
        "   - Colle du texte (une transaction par ligne) OU envoie un fichier .txt / .csv\n"
        "   - Le bot totalise revenus, dépenses et variation de solde\n\n"
        "⚠️ *Ce bot ne remplace pas un comptable ni un logiciel de paie officiel.*"
    )
    await update.message.reply_markdown(text)


# ==============================
# CONVERSATION – PAYROLL
# ==============================

async def payroll_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("QC", callback_data="QC"),
            InlineKeyboardButton("ON", callback_data="ON"),
            InlineKeyboardButton("BC", callback_data="BC"),
        ],
        [
            InlineKeyboardButton("AB", callback_data="AB"),
            InlineKeyboardButton("MB", callback_data="MB"),
            InlineKeyboardButton("NB", callback_data="NB"),
        ],
        [
            InlineKeyboardButton("NS", callback_data="NS"),
            InlineKeyboardButton("SK", callback_data="SK"),
        ],
        [
            InlineKeyboardButton("NL", callback_data="NL"),
            InlineKeyboardButton("PE", callback_data="PE"),
        ],
    ]

    await update.message.reply_text(
        "🧾 *Calcul de paie*\n\n"
        "Choisis d'abord la province de travail :",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    return PAY_PROVINCE


async def payroll_set_province(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    province = query.data
    context.user_data["pay_province"] = province

    await query.edit_message_text(
        f"Province sélectionnée : *{province}*\n\n"
        "Maintenant, entre le *salaire brut pour la période*.\n"
        "Exemples :\n"
        "• 2000\n"
        "• 1450.75",
        parse_mode="Markdown",
    )

    return PAY_GROSS


async def payroll_set_gross(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        gross = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text(
            "❌ Montant invalide. Entre un nombre comme `2000` ou `1450.75`."
        )
        return PAY_GROSS

    if gross <= 0:
        await update.message.reply_text(
            "❌ Le salaire brut doit être un montant positif. Réessaie."
        )
        return PAY_GROSS

    context.user_data["pay_gross"] = gross

    keyboard = [
        [
            InlineKeyboardButton("Hebdomadaire", callback_data="weekly"),
            InlineKeyboardButton("Aux 2 semaines", callback_data="biweekly"),
        ],
        [
            InlineKeyboardButton("Mensuel", callback_data="monthly"),
        ],
    ]

    await update.message.reply_text(
        "Parfait 👍\n\n"
        "Quelle est la *fréquence de paie* ?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )

    return PAY_FREQUENCY


async def payroll_set_frequency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    frequency = query.data
    context.user_data["pay_frequency"] = frequency

    province = context.user_data["pay_province"]
    gross = context.user_data["pay_gross"]

    result = calculate_payroll(province, gross, frequency)

    # Texte de fréquence plus lisible
    freq_label = {
        "weekly": "Hebdomadaire",
        "biweekly": "Aux 2 semaines",
        "monthly": "Mensuel",
    }.get(frequency, frequency)

    text = (
        "🧾 *Résultat – Talon de paie simplifié*\n\n"
        f"Province : *{result.province}*\n"
        f"Fréquence : *{freq_label}*\n"
        f"Salaire brut : *{result.gross:,.2f} $*\n\n"
        f"Impôt fédéral : *{result.federal_tax:,.2f} $*\n"
        f"Impôt provincial : *{result.provincial_tax:,.2f} $*\n"
        f"Assurance emploi (AE) : *{result.ei:,.2f} $*\n"
        f"Régime de pension : *{result.pension:,.2f} $*\n"
        "--------------------------------------\n"
        f"Net payé (approx.) : *{result.net:,.2f} $*\n\n"
        "⚠️ *Les taux sont simplifiés et approximatifs. "
        "Pour de la paie réelle, utilise un logiciel certifié ou un comptable.*"
    )

    await query.edit_message_text(text, parse_mode="Markdown")

    # Fin de la conversation
    return ConversationHandler.END


async def payroll_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Conversation annulée. Tu peux recommencer avec /payroll.")
    return ConversationHandler.END


# ==============================
# CONVERSATION – BANK STATEMENT
# ==============================

async def statement_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🏦 *Analyse de relevé bancaire*\n\n"
        "Tu peux :\n"
        "1️⃣ Coller ci-dessous ton relevé en texte (une transaction par ligne)\n"
        "   Exemples de lignes :\n"
        "   `2025-12-01, DEP, +1500.25, Salaire`\n"
        "   `2025-12-02, -45.80, Restaurant`\n\n"
        "2️⃣ OU envoyer un fichier `.txt` ou `.csv` simple.\n\n"
        "Je vais totaliser les *revenus*, *dépenses* et la *variation de solde*."
    )
    await update.message.reply_markdown(text)
    return STMT_WAIT_TEXT


async def statement_process_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    stats = analyze_bank_statement_from_text(text)

    reply = (
        "📊 *Résultat – Relevé bancaire (texte)*\n\n"
        f"Nombre de revenus : *{stats['count_income']}*\n"
        f"Total revenus : *{stats['income']:,.2f} $*\n\n"
        f"Nombre de dépenses : *{stats['count_expense']}*\n"
        f"Total dépenses : *{stats['expenses']:,.2f} $*\n\n"
        f"Variation de solde (revenus + dépenses) : *{stats['balance_change']:,.2f} $*\n\n"
        "⚠️ Analyse basée uniquement sur les montants trouvés dans chaque ligne.\n"
        "   Formate bien ton relevé pour de meilleurs résultats."
    )
    await update.message.reply_markdown(reply)
    return ConversationHandler.END


async def statement_process_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc.file_name.lower().endswith((".txt", ".csv")):
        await update.message.reply_text(
            "❌ Pour l'instant, j'accepte seulement des fichiers `.txt` ou `.csv` simples."
        )
        return STMT_WAIT_TEXT

    file = await doc.get_file()
    # Téléchargement en mémoire
    file_bytes = await file.download_as_bytearray()
    content = file_bytes.decode("utf-8", errors="ignore")

    stats = analyze_bank_statement_from_text(content)

    reply = (
        "📊 *Résultat – Relevé bancaire (fichier)*\n\n"
        f"Fichier : `{doc.file_name}`\n\n"
        f"Nombre de revenus : *{stats['count_income']}*\n"
        f"Total revenus : *{stats['income']:,.2f} $*\n\n"
        f"Nombre de dépenses : *{stats['count_expense']}*\n"
        f"Total dépenses : *{stats['expenses']:,.2f} $*\n\n"
        f"Variation de solde (revenus + dépenses) : *{stats['balance_change']:,.2f} $*\n\n"
        "⚠️ Analyse basée seulement sur les montants trouvés. "
        "Assure-toi que ton relevé est bien formatté."
    )
    await update.message.reply_markdown(reply)
    return ConversationHandler.END


async def statement_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Analyse de relevé annulée. Tu peux recommencer avec /statement.")
    return ConversationHandler.END


# ==============================
# MAIN
# ==============================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("⚠️ BOT_TOKEN environment variable is not set!")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # /start et /help
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))

    # Conversation pour /payroll
    payroll_conv = ConversationHandler(
        entry_points=[CommandHandler("payroll", payroll_start)],
        states={
            PAY_PROVINCE: [CallbackQueryHandler(payroll_set_province)],
            PAY_GROSS: [MessageHandler(filters.TEXT & ~filters.COMMAND, payroll_set_gross)],
            PAY_FREQUENCY: [CallbackQueryHandler(payroll_set_frequency)],
        },
        fallbacks=[CommandHandler("cancel", payroll_cancel)],
    )
    app.add_handler(payroll_conv)

    # Conversation pour /statement
    statement_conv = ConversationHandler(
        entry_points=[CommandHandler("statement", statement_start)],
        states={
            STMT_WAIT_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, statement_process_text),
                MessageHandler(filters.Document.ALL, statement_process_document),
            ],
        },
        fallbacks=[CommandHandler("cancel", statement_cancel)],
    )
    app.add_handler(statement_conv)

    logger.info("Bot démarré...")
    app.run_polling()


if __name__ == "__main__":
    main()
