import logging
import os
import io
import csv
import re
from dataclasses import dataclass
from typing import Dict, Optional
from datetime import datetime

import psycopg2
from psycopg2.extras import RealDictCursor

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

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

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

# États pour la conversation de paie
(
    PAY_PROVINCE,
    PAY_GROSS,
    PAY_FREQUENCY,
) = range(3)

# États pour relevé bancaire
STMT_WAIT_TEXT = 10

# États pour gestion de fichiers
(
    FILE_ACTION,
    FILE_UPLOAD,
    FILE_SELECT,
    CSV_EDIT_SELECT,
    CSV_EDIT_ROW,
    CSV_EDIT_VALUE,
) = range(20, 26)


# ==============================
# LOGGING
# ==============================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ==============================
# DATABASE
# ==============================

def get_db_connection():
    """Get database connection."""
    if not DATABASE_URL:
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        return conn
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        return None


def init_database():
    """Initialize database tables."""
    conn = get_db_connection()
    if not conn:
        logger.warning("No database connection - file storage disabled")
        return

    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_files (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                file_name VARCHAR(255) NOT NULL,
                file_type VARCHAR(50) NOT NULL,
                file_content BYTEA NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_files_user_id ON user_files(user_id)
        """)
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Database init error: {e}")


def save_file_to_db(user_id: int, file_name: str, file_type: str, content: bytes) -> bool:
    """Save a file to database."""
    conn = get_db_connection()
    if not conn:
        return False

    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO user_files (user_id, file_name, file_type, file_content)
            VALUES (%s, %s, %s, %s)
            """,
            (user_id, file_name, file_type, content)
        )
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Save file error: {e}")
        return False


def get_user_files(user_id: int) -> list:
    """Get all files for a user."""
    conn = get_db_connection()
    if not conn:
        return []

    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, file_name, file_type, created_at
            FROM user_files
            WHERE user_id = %s
            ORDER BY created_at DESC
            """,
            (user_id,)
        )
        files = cur.fetchall()
        cur.close()
        conn.close()
        return files
    except Exception as e:
        logger.error(f"Get files error: {e}")
        return []


def get_file_by_id(file_id: int, user_id: int) -> Optional[dict]:
    """Get a specific file by ID."""
    conn = get_db_connection()
    if not conn:
        return None

    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, file_name, file_type, file_content
            FROM user_files
            WHERE id = %s AND user_id = %s
            """,
            (file_id, user_id)
        )
        file = cur.fetchone()
        cur.close()
        conn.close()
        return file
    except Exception as e:
        logger.error(f"Get file error: {e}")
        return None


def update_file_content(file_id: int, user_id: int, content: bytes) -> bool:
    """Update file content."""
    conn = get_db_connection()
    if not conn:
        return False

    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE user_files
            SET file_content = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND user_id = %s
            """,
            (content, file_id, user_id)
        )
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Update file error: {e}")
        return False


def delete_file_from_db(file_id: int, user_id: int) -> bool:
    """Delete a file from database."""
    conn = get_db_connection()
    if not conn:
        return False

    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM user_files WHERE id = %s AND user_id = %s",
            (file_id, user_id)
        )
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Delete file error: {e}")
        return False


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


FEDERAL_TAX_RATE = 0.15
PROVINCIAL_TAX_RATES: Dict[str, float] = {
    "QC": 0.15, "ON": 0.0505, "BC": 0.05, "AB": 0.10,
    "MB": 0.108, "NB": 0.0968, "NS": 0.0879, "SK": 0.1075,
    "NL": 0.087, "PE": 0.098,
}
EI_RATE = 0.0166
CPP_RATE = 0.0595
QPP_RATE = 0.064


def calculate_payroll(province: str, gross: float, frequency: str) -> PayrollResult:
    prov = province.upper()
    prov_rate = PROVINCIAL_TAX_RATES.get(prov, 0.07)
    federal_tax = gross * FEDERAL_TAX_RATE
    provincial_tax = gross * prov_rate
    ei = gross * EI_RATE
    pension = gross * QPP_RATE if prov == "QC" else gross * CPP_RATE
    net = gross - (federal_tax + provincial_tax + ei + pension)

    return PayrollResult(
        province=prov, gross=gross, frequency=frequency,
        federal_tax=federal_tax, provincial_tax=provincial_tax,
        ei=ei, pension=pension, net=net,
    )


def generate_payroll_pdf(result: PayrollResult, employee_name: str = "Employé") -> bytes:
    """Generate a PDF pay stub."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    elements = []

    # Title
    title_style = ParagraphStyle(
        'CustomTitle', parent=styles['Heading1'],
        fontSize=18, spaceAfter=30, alignment=1
    )
    elements.append(Paragraph("TALON DE PAIE", title_style))
    elements.append(Spacer(1, 20))

    # Info
    freq_label = {"weekly": "Hebdomadaire", "biweekly": "Aux 2 semaines", "monthly": "Mensuel"}.get(result.frequency, result.frequency)

    info_data = [
        ["Employé:", employee_name],
        ["Province:", result.province],
        ["Période:", freq_label],
        ["Date:", datetime.now().strftime("%Y-%m-%d")],
    ]
    info_table = Table(info_data, colWidths=[150, 300])
    info_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 30))

    # Earnings and deductions
    data = [
        ["Description", "Montant"],
        ["Salaire brut", f"{result.gross:,.2f} $"],
        ["", ""],
        ["DÉDUCTIONS", ""],
        ["Impôt fédéral", f"-{result.federal_tax:,.2f} $"],
        ["Impôt provincial", f"-{result.provincial_tax:,.2f} $"],
        ["Assurance emploi (AE)", f"-{result.ei:,.2f} $"],
        ["Régime de pension", f"-{result.pension:,.2f} $"],
        ["", ""],
        ["NET À PAYER", f"{result.net:,.2f} $"],
    ]

    table = Table(data, colWidths=[300, 150])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTNAME', (0, 3), (0, 3), 'Helvetica-Bold'),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('BACKGROUND', (0, -1), (-1, -1), colors.lightgrey),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
    ]))
    elements.append(table)
    elements.append(Spacer(1, 30))

    # Disclaimer
    disclaimer = Paragraph(
        "<i>Ce document est généré à titre informatif seulement. Les montants sont approximatifs.</i>",
        styles['Normal']
    )
    elements.append(disclaimer)

    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()


def analyze_bank_statement_from_text(text: str) -> Dict[str, float]:
    income = 0.0
    expenses = 0.0
    count_income = 0
    count_expense = 0

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
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

    return {
        "income": income, "expenses": expenses,
        "count_income": count_income, "count_expense": count_expense,
        "balance_change": income + expenses,
    }


# ==============================
# HANDLERS COMMANDES
# ==============================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (
        f"Salut {user.first_name or ''} 👋\n\n"
        "Je suis ton bot de **calcul de paie** et **gestion de fichiers**.\n\n"
        "📌 Commandes principales :\n"
        "• /payroll – calculer un talon de paie (avec PDF)\n"
        "• /statement – analyser un relevé bancaire\n"
        "• /files – gérer tes fichiers (upload, download, edit)\n"
        "• /help – voir toutes les options\n\n"
        "⚠️ *Attention : les calculs sont approximatifs.*"
    )
    await update.message.reply_markdown(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🧾 **Fonctions du bot**\n\n"
        "1️⃣ /payroll – Calcul de paie\n"
        "   • Choisis province, salaire, fréquence\n"
        "   • Reçois un PDF du talon de paie\n\n"
        "2️⃣ /statement – Analyse de relevé\n"
        "   • Colle du texte ou envoie un fichier\n"
        "   • Reçois un résumé des transactions\n\n"
        "3️⃣ /files – Gestion de fichiers\n"
        "   • Upload des fichiers CSV/PDF\n"
        "   • Télécharge tes fichiers sauvegardés\n"
        "   • Édite tes fichiers CSV\n"
        "   • Supprime des fichiers\n\n"
        "⚠️ *Ce bot ne remplace pas un comptable.*"
    )
    await update.message.reply_markdown(text)


# ==============================
# CONVERSATION – PAYROLL
# ==============================

async def payroll_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("QC", callback_data="prov_QC"),
         InlineKeyboardButton("ON", callback_data="prov_ON"),
         InlineKeyboardButton("BC", callback_data="prov_BC")],
        [InlineKeyboardButton("AB", callback_data="prov_AB"),
         InlineKeyboardButton("MB", callback_data="prov_MB"),
         InlineKeyboardButton("NB", callback_data="prov_NB")],
        [InlineKeyboardButton("NS", callback_data="prov_NS"),
         InlineKeyboardButton("SK", callback_data="prov_SK")],
        [InlineKeyboardButton("NL", callback_data="prov_NL"),
         InlineKeyboardButton("PE", callback_data="prov_PE")],
    ]
    await update.message.reply_text(
        "🧾 *Calcul de paie*\n\nChoisis la province :",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    return PAY_PROVINCE


async def payroll_set_province(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    province = query.data.replace("prov_", "")
    context.user_data["pay_province"] = province
    await query.edit_message_text(
        f"Province : *{province}*\n\nEntre le *salaire brut* pour la période :",
        parse_mode="Markdown",
    )
    return PAY_GROSS


async def payroll_set_gross(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        gross = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("❌ Montant invalide. Entre un nombre.")
        return PAY_GROSS
    if gross <= 0:
        await update.message.reply_text("❌ Le montant doit être positif.")
        return PAY_GROSS

    context.user_data["pay_gross"] = gross
    keyboard = [
        [InlineKeyboardButton("Hebdomadaire", callback_data="freq_weekly"),
         InlineKeyboardButton("Aux 2 sem.", callback_data="freq_biweekly")],
        [InlineKeyboardButton("Mensuel", callback_data="freq_monthly")],
    ]
    await update.message.reply_text(
        "Quelle *fréquence de paie* ?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    return PAY_FREQUENCY


async def payroll_set_frequency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    frequency = query.data.replace("freq_", "")

    province = context.user_data["pay_province"]
    gross = context.user_data["pay_gross"]
    result = calculate_payroll(province, gross, frequency)

    freq_label = {"weekly": "Hebdomadaire", "biweekly": "Aux 2 semaines", "monthly": "Mensuel"}.get(frequency, frequency)

    text = (
        "🧾 *Talon de paie*\n\n"
        f"Province : *{result.province}*\n"
        f"Fréquence : *{freq_label}*\n"
        f"Brut : *{result.gross:,.2f} $*\n\n"
        f"Impôt fédéral : *{result.federal_tax:,.2f} $*\n"
        f"Impôt provincial : *{result.provincial_tax:,.2f} $*\n"
        f"AE : *{result.ei:,.2f} $*\n"
        f"Pension : *{result.pension:,.2f} $*\n"
        "─────────────────────\n"
        f"*NET : {result.net:,.2f} $*"
    )
    await query.edit_message_text(text, parse_mode="Markdown")

    # Generate and send PDF
    pdf_bytes = generate_payroll_pdf(result)
    await query.message.reply_document(
        document=io.BytesIO(pdf_bytes),
        filename=f"talon_paie_{datetime.now().strftime('%Y%m%d')}.pdf",
        caption="📄 Voici ton talon de paie en PDF"
    )

    # Save to database if available
    user_id = update.effective_user.id
    if save_file_to_db(user_id, f"talon_paie_{datetime.now().strftime('%Y%m%d')}.pdf", "pdf", pdf_bytes):
        await query.message.reply_text("✅ PDF sauvegardé dans tes fichiers (/files)")

    return ConversationHandler.END


async def payroll_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Annulé. /payroll pour recommencer.")
    return ConversationHandler.END


# ==============================
# CONVERSATION – BANK STATEMENT
# ==============================

async def statement_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🏦 *Analyse de relevé bancaire*\n\n"
        "Envoie :\n"
        "• Du texte (une transaction par ligne)\n"
        "• Un fichier `.txt` ou `.csv`\n\n"
        "Format : `+1500.25` (revenu) ou `-45.80` (dépense)"
    )
    await update.message.reply_markdown(text)
    return STMT_WAIT_TEXT


async def statement_process_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    stats = analyze_bank_statement_from_text(text)
    reply = (
        "📊 *Résultat*\n\n"
        f"Revenus : *{stats['count_income']}* transactions = *{stats['income']:,.2f} $*\n"
        f"Dépenses : *{stats['count_expense']}* transactions = *{stats['expenses']:,.2f} $*\n\n"
        f"Variation : *{stats['balance_change']:,.2f} $*"
    )
    await update.message.reply_markdown(reply)
    return ConversationHandler.END


async def statement_process_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc.file_name.lower().endswith((".txt", ".csv")):
        await update.message.reply_text("❌ Fichiers `.txt` ou `.csv` seulement.")
        return STMT_WAIT_TEXT

    file = await doc.get_file()
    file_bytes = await file.download_as_bytearray()
    content = file_bytes.decode("utf-8", errors="ignore")
    stats = analyze_bank_statement_from_text(content)

    reply = (
        f"📊 *Résultat – {doc.file_name}*\n\n"
        f"Revenus : *{stats['count_income']}* = *{stats['income']:,.2f} $*\n"
        f"Dépenses : *{stats['count_expense']}* = *{stats['expenses']:,.2f} $*\n\n"
        f"Variation : *{stats['balance_change']:,.2f} $*"
    )
    await update.message.reply_markdown(reply)
    return ConversationHandler.END


async def statement_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Annulé. /statement pour recommencer.")
    return ConversationHandler.END


# ==============================
# CONVERSATION – FILE MANAGEMENT
# ==============================

async def files_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📤 Upload un fichier", callback_data="file_upload")],
        [InlineKeyboardButton("📥 Mes fichiers", callback_data="file_list")],
        [InlineKeyboardButton("✏️ Éditer un CSV", callback_data="file_edit")],
        [InlineKeyboardButton("🗑️ Supprimer", callback_data="file_delete")],
    ]
    await update.message.reply_text(
        "📁 *Gestion de fichiers*\n\nQue veux-tu faire ?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    return FILE_ACTION


async def files_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data

    if action == "file_upload":
        await query.edit_message_text(
            "📤 Envoie-moi un fichier CSV ou PDF à sauvegarder."
        )
        return FILE_UPLOAD

    elif action == "file_list":
        user_id = update.effective_user.id
        files = get_user_files(user_id)

        if not files:
            await query.edit_message_text("📂 Tu n'as aucun fichier sauvegardé.")
            return ConversationHandler.END

        keyboard = []
        for f in files[:10]:  # Max 10 files
            label = f"{f['file_name']} ({f['file_type'].upper()})"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"dl_{f['id']}")])
        keyboard.append([InlineKeyboardButton("❌ Fermer", callback_data="file_close")])

        await query.edit_message_text(
            "📂 *Tes fichiers* (clique pour télécharger) :",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return FILE_SELECT

    elif action == "file_edit":
        user_id = update.effective_user.id
        files = [f for f in get_user_files(user_id) if f['file_type'] == 'csv']

        if not files:
            await query.edit_message_text("📂 Tu n'as aucun fichier CSV à éditer.")
            return ConversationHandler.END

        keyboard = []
        for f in files[:10]:
            keyboard.append([InlineKeyboardButton(f['file_name'], callback_data=f"edit_{f['id']}")])
        keyboard.append([InlineKeyboardButton("❌ Annuler", callback_data="file_close")])

        await query.edit_message_text(
            "✏️ *Quel CSV veux-tu éditer ?*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return CSV_EDIT_SELECT

    elif action == "file_delete":
        user_id = update.effective_user.id
        files = get_user_files(user_id)

        if not files:
            await query.edit_message_text("📂 Tu n'as aucun fichier à supprimer.")
            return ConversationHandler.END

        keyboard = []
        for f in files[:10]:
            label = f"🗑️ {f['file_name']}"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"del_{f['id']}")])
        keyboard.append([InlineKeyboardButton("❌ Annuler", callback_data="file_close")])

        await query.edit_message_text(
            "🗑️ *Quel fichier supprimer ?*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return FILE_SELECT

    return ConversationHandler.END


async def files_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        await update.message.reply_text("❌ Envoie un fichier (pas une image).")
        return FILE_UPLOAD

    if not doc.file_name.lower().endswith((".csv", ".pdf", ".txt")):
        await update.message.reply_text("❌ Fichiers CSV, PDF ou TXT seulement.")
        return FILE_UPLOAD

    file = await doc.get_file()
    file_bytes = await file.download_as_bytearray()

    file_type = doc.file_name.split(".")[-1].lower()
    user_id = update.effective_user.id

    if save_file_to_db(user_id, doc.file_name, file_type, bytes(file_bytes)):
        await update.message.reply_text(f"✅ Fichier *{doc.file_name}* sauvegardé!", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Erreur de sauvegarde. Base de données non configurée?")

    return ConversationHandler.END


async def files_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "file_close":
        await query.edit_message_text("👍 OK!")
        return ConversationHandler.END

    user_id = update.effective_user.id

    if data.startswith("dl_"):
        file_id = int(data.replace("dl_", ""))
        file = get_file_by_id(file_id, user_id)

        if file:
            await query.message.reply_document(
                document=io.BytesIO(bytes(file['file_content'])),
                filename=file['file_name'],
                caption=f"📄 {file['file_name']}"
            )
        else:
            await query.edit_message_text("❌ Fichier non trouvé.")
        return ConversationHandler.END

    elif data.startswith("del_"):
        file_id = int(data.replace("del_", ""))
        if delete_file_from_db(file_id, user_id):
            await query.edit_message_text("✅ Fichier supprimé!")
        else:
            await query.edit_message_text("❌ Erreur de suppression.")
        return ConversationHandler.END

    return ConversationHandler.END


async def csv_edit_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "file_close":
        await query.edit_message_text("👍 OK!")
        return ConversationHandler.END

    if data.startswith("edit_"):
        file_id = int(data.replace("edit_", ""))
        user_id = update.effective_user.id
        file = get_file_by_id(file_id, user_id)

        if not file:
            await query.edit_message_text("❌ Fichier non trouvé.")
            return ConversationHandler.END

        context.user_data["edit_file_id"] = file_id
        content = bytes(file['file_content']).decode('utf-8', errors='ignore')

        # Parse CSV and show first rows
        reader = csv.reader(io.StringIO(content))
        rows = list(reader)

        if len(rows) < 2:
            await query.edit_message_text("❌ Le CSV est vide ou n'a qu'un en-tête.")
            return ConversationHandler.END

        context.user_data["csv_rows"] = rows

        # Show preview
        preview = "📄 *Aperçu du CSV :*\n\n"
        for i, row in enumerate(rows[:5]):
            preview += f"`{i}: {', '.join(row[:4])}{'...' if len(row) > 4 else ''}`\n"
        if len(rows) > 5:
            preview += f"_... et {len(rows) - 5} autres lignes_\n"

        preview += "\n*Entre le numéro de ligne à modifier :*"

        await query.edit_message_text(preview, parse_mode="Markdown")
        return CSV_EDIT_ROW

    return ConversationHandler.END


async def csv_edit_row(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        row_num = int(update.message.text)
    except ValueError:
        await update.message.reply_text("❌ Entre un numéro valide.")
        return CSV_EDIT_ROW

    rows = context.user_data.get("csv_rows", [])
    if row_num < 0 or row_num >= len(rows):
        await update.message.reply_text(f"❌ Numéro invalide (0-{len(rows)-1}).")
        return CSV_EDIT_ROW

    context.user_data["edit_row_num"] = row_num
    row = rows[row_num]

    text = f"📝 *Ligne {row_num} actuelle :*\n`{', '.join(row)}`\n\n"
    text += "*Entre la nouvelle ligne (valeurs séparées par des virgules) :*"

    await update.message.reply_markdown(text)
    return CSV_EDIT_VALUE


async def csv_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_values = update.message.text.split(",")
    new_values = [v.strip() for v in new_values]

    row_num = context.user_data["edit_row_num"]
    rows = context.user_data["csv_rows"]
    file_id = context.user_data["edit_file_id"]
    user_id = update.effective_user.id

    # Update the row
    rows[row_num] = new_values

    # Convert back to CSV
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerows(rows)
    new_content = output.getvalue().encode('utf-8')

    if update_file_content(file_id, user_id, new_content):
        await update.message.reply_text(f"✅ Ligne {row_num} modifiée avec succès!")
    else:
        await update.message.reply_text("❌ Erreur lors de la sauvegarde.")

    return ConversationHandler.END


async def files_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Annulé. /files pour recommencer.")
    return ConversationHandler.END


# ==============================
# MAIN
# ==============================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set!")

    # Initialize database
    init_database()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))

    # Payroll conversation
    payroll_conv = ConversationHandler(
        entry_points=[CommandHandler("payroll", payroll_start)],
        states={
            PAY_PROVINCE: [CallbackQueryHandler(payroll_set_province, pattern="^prov_")],
            PAY_GROSS: [MessageHandler(filters.TEXT & ~filters.COMMAND, payroll_set_gross)],
            PAY_FREQUENCY: [CallbackQueryHandler(payroll_set_frequency, pattern="^freq_")],
        },
        fallbacks=[CommandHandler("cancel", payroll_cancel)],
    )
    app.add_handler(payroll_conv)

    # Statement conversation
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

    # Files conversation
    files_conv = ConversationHandler(
        entry_points=[CommandHandler("files", files_start)],
        states={
            FILE_ACTION: [CallbackQueryHandler(files_action)],
            FILE_UPLOAD: [MessageHandler(filters.Document.ALL, files_upload)],
            FILE_SELECT: [CallbackQueryHandler(files_select)],
            CSV_EDIT_SELECT: [CallbackQueryHandler(csv_edit_select)],
            CSV_EDIT_ROW: [MessageHandler(filters.TEXT & ~filters.COMMAND, csv_edit_row)],
            CSV_EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, csv_edit_value)],
        },
        fallbacks=[CommandHandler("cancel", files_cancel)],
    )
    app.add_handler(files_conv)

    logger.info("Bot démarré...")
    app.run_polling()


if __name__ == "__main__":
    main()
