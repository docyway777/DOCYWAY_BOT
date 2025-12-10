import logging
import os
import io
import csv
import re
from dataclasses import dataclass
from typing import Dict, Optional, List, Tuple
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

# États conversation paie
(PAY_PROVINCE, PAY_GROSS, PAY_FREQUENCY) = range(3)

# États relevé bancaire
STMT_WAIT_TEXT = 10

# États gestion fichiers
(FILE_ACTION, FILE_UPLOAD, FILE_SELECT, CSV_EDIT_SELECT, CSV_EDIT_ROW, CSV_EDIT_VALUE) = range(20, 26)

# États calculateur horaire
(HOURLY_PROVINCE, HOURLY_RATE, HOURLY_HOURS, HOURLY_OVERTIME) = range(30, 34)

# États rapport T4/Relevé 1
(T4_NAME, T4_SIN, T4_EMPLOYER, T4_INCOME, T4_TAX_FED, T4_TAX_PROV, T4_EI, T4_CPP) = range(40, 48)


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
    if not DATABASE_URL:
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        return conn
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        return None


def init_database():
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
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_files_user_id ON user_files(user_id)")
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Database init error: {e}")


def save_file_to_db(user_id: int, file_name: str, file_type: str, content: bytes) -> bool:
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO user_files (user_id, file_name, file_type, file_content) VALUES (%s, %s, %s, %s)",
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
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, file_name, file_type, created_at FROM user_files WHERE user_id = %s ORDER BY created_at DESC",
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
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, file_name, file_type, file_content FROM user_files WHERE id = %s AND user_id = %s",
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
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE user_files SET file_content = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s AND user_id = %s",
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
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM user_files WHERE id = %s AND user_id = %s", (file_id, user_id))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Delete file error: {e}")
        return False


# ==============================
# VRAIS PALIERS D'IMPOSITION 2024
# ==============================

# Paliers fédéraux 2024
FEDERAL_BRACKETS: List[Tuple[float, float]] = [
    (55867, 0.15),
    (111733, 0.205),
    (173205, 0.26),
    (246752, 0.29),
    (float('inf'), 0.33),
]

# Paliers provinciaux 2024
PROVINCIAL_BRACKETS: Dict[str, List[Tuple[float, float]]] = {
    "QC": [
        (51780, 0.14),
        (103545, 0.19),
        (126000, 0.24),
        (float('inf'), 0.2575),
    ],
    "ON": [
        (51446, 0.0505),
        (102894, 0.0915),
        (150000, 0.1116),
        (220000, 0.1216),
        (float('inf'), 0.1316),
    ],
    "BC": [
        (47937, 0.0506),
        (95875, 0.077),
        (110076, 0.105),
        (133664, 0.1229),
        (181232, 0.147),
        (252752, 0.168),
        (float('inf'), 0.205),
    ],
    "AB": [
        (148269, 0.10),
        (177922, 0.12),
        (237230, 0.13),
        (355845, 0.14),
        (float('inf'), 0.15),
    ],
    "MB": [
        (47000, 0.108),
        (100000, 0.1275),
        (float('inf'), 0.174),
    ],
    "SK": [
        (52057, 0.105),
        (148734, 0.125),
        (float('inf'), 0.145),
    ],
    "NB": [
        (47715, 0.094),
        (95431, 0.14),
        (176756, 0.16),
        (float('inf'), 0.195),
    ],
    "NS": [
        (29590, 0.0879),
        (59180, 0.1495),
        (93000, 0.1667),
        (150000, 0.175),
        (float('inf'), 0.21),
    ],
    "NL": [
        (43198, 0.087),
        (86395, 0.145),
        (154244, 0.158),
        (215943, 0.178),
        (275870, 0.198),
        (551739, 0.208),
        (1103478, 0.213),
        (float('inf'), 0.218),
    ],
    "PE": [
        (32656, 0.098),
        (64313, 0.138),
        (105000, 0.167),
        (140000, 0.1765),
        (float('inf'), 0.19),
    ],
}

# Taux AE et RPC/RRQ 2024
EI_RATE = 0.0166
EI_MAX = 1049.12
CPP_RATE = 0.0595
CPP_MAX = 3867.50
CPP_EXEMPTION = 3500
QPP_RATE = 0.064
QPP_MAX = 4160.00


def calculate_bracket_tax(income: float, brackets: List[Tuple[float, float]]) -> float:
    """Calcule l'impôt selon les paliers progressifs."""
    tax = 0.0
    prev_limit = 0.0

    for limit, rate in brackets:
        if income <= prev_limit:
            break
        taxable = min(income, limit) - prev_limit
        tax += taxable * rate
        prev_limit = limit

    return tax


def annualize_income(gross: float, frequency: str) -> float:
    """Convertit le salaire de la période en salaire annuel."""
    multipliers = {"weekly": 52, "biweekly": 26, "monthly": 12}
    return gross * multipliers.get(frequency, 26)


def deannualize(annual: float, frequency: str) -> float:
    """Convertit un montant annuel en montant par période."""
    multipliers = {"weekly": 52, "biweekly": 26, "monthly": 12}
    return annual / multipliers.get(frequency, 26)


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
    annual_gross: float
    annual_federal: float
    annual_provincial: float


def calculate_payroll(province: str, gross: float, frequency: str) -> PayrollResult:
    """Calcul de paie avec vrais paliers d'imposition."""
    prov = province.upper()
    annual_gross = annualize_income(gross, frequency)

    # Impôt fédéral (paliers progressifs)
    annual_federal = calculate_bracket_tax(annual_gross, FEDERAL_BRACKETS)

    # Crédit personnel de base fédéral 2024
    federal_basic_credit = 15705 * 0.15
    annual_federal = max(0, annual_federal - federal_basic_credit)

    # Impôt provincial (paliers progressifs)
    prov_brackets = PROVINCIAL_BRACKETS.get(prov, [(float('inf'), 0.10)])
    annual_provincial = calculate_bracket_tax(annual_gross, prov_brackets)

    # Crédit personnel de base provincial (approximatif)
    prov_basic_credits = {
        "QC": 18056 * 0.14, "ON": 12399 * 0.0505, "BC": 12580 * 0.0506,
        "AB": 21003 * 0.10, "MB": 15780 * 0.108, "SK": 17661 * 0.105,
        "NB": 13044 * 0.094, "NS": 8481 * 0.0879, "NL": 10818 * 0.087,
        "PE": 13500 * 0.098,
    }
    annual_provincial = max(0, annual_provincial - prov_basic_credits.get(prov, 0))

    # Convertir en montant par période
    federal_tax = deannualize(annual_federal, frequency)
    provincial_tax = deannualize(annual_provincial, frequency)

    # AE (avec maximum annuel)
    annual_ei = min(annual_gross * EI_RATE, EI_MAX)
    ei = deannualize(annual_ei, frequency)

    # CPP/QPP (avec exemption et maximum)
    pensionable = max(0, annual_gross - CPP_EXEMPTION)
    if prov == "QC":
        annual_pension = min(pensionable * QPP_RATE, QPP_MAX)
    else:
        annual_pension = min(pensionable * CPP_RATE, CPP_MAX)
    pension = deannualize(annual_pension, frequency)

    net = gross - (federal_tax + provincial_tax + ei + pension)

    return PayrollResult(
        province=prov, gross=gross, frequency=frequency,
        federal_tax=federal_tax, provincial_tax=provincial_tax,
        ei=ei, pension=pension, net=net,
        annual_gross=annual_gross, annual_federal=annual_federal,
        annual_provincial=annual_provincial,
    )


def generate_payroll_pdf(result: PayrollResult, employee_name: str = "Employé") -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    elements = []

    title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'], fontSize=18, spaceAfter=30, alignment=1)
    elements.append(Paragraph("TALON DE PAIE", title_style))
    elements.append(Spacer(1, 20))

    freq_label = {"weekly": "Hebdomadaire", "biweekly": "Aux 2 semaines", "monthly": "Mensuel"}.get(result.frequency, result.frequency)

    info_data = [
        ["Employé:", employee_name],
        ["Province:", result.province],
        ["Période:", freq_label],
        ["Date:", datetime.now().strftime("%Y-%m-%d")],
        ["Salaire annuel:", f"{result.annual_gross:,.2f} $"],
    ]
    info_table = Table(info_data, colWidths=[150, 300])
    info_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 30))

    data = [
        ["Description", "Période", "Annuel"],
        ["Salaire brut", f"{result.gross:,.2f} $", f"{result.annual_gross:,.2f} $"],
        ["", "", ""],
        ["DÉDUCTIONS", "", ""],
        ["Impôt fédéral", f"-{result.federal_tax:,.2f} $", f"-{result.annual_federal:,.2f} $"],
        ["Impôt provincial", f"-{result.provincial_tax:,.2f} $", f"-{result.annual_provincial:,.2f} $"],
        ["Assurance emploi (AE)", f"-{result.ei:,.2f} $", ""],
        ["Régime de pension (RPC/RRQ)", f"-{result.pension:,.2f} $", ""],
        ["", "", ""],
        ["NET À PAYER", f"{result.net:,.2f} $", ""],
    ]

    table = Table(data, colWidths=[220, 120, 120])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
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

    disclaimer = Paragraph(
        "<i>Calculs basés sur les paliers d'imposition 2024. Les montants peuvent varier selon votre situation.</i>",
        styles['Normal']
    )
    elements.append(disclaimer)

    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()


def generate_t4_pdf(data: dict) -> bytes:
    """Génère un formulaire T4/Relevé 1 simplifié."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    elements = []

    is_quebec = data.get('province', '').upper() == 'QC'
    title = "RELEVÉ 1 - Revenus d'emploi" if is_quebec else "T4 - État de la rémunération payée"

    title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'], fontSize=16, spaceAfter=20, alignment=1)
    elements.append(Paragraph(title, title_style))
    elements.append(Paragraph(f"<b>Année d'imposition: {datetime.now().year}</b>", styles['Normal']))
    elements.append(Spacer(1, 20))

    # Info employé
    emp_data = [
        ["Nom de l'employé:", data.get('name', 'N/A')],
        ["NAS:", data.get('sin', 'XXX-XXX-XXX')],
        ["Employeur:", data.get('employer', 'N/A')],
    ]
    emp_table = Table(emp_data, colWidths=[150, 300])
    emp_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    elements.append(emp_table)
    elements.append(Spacer(1, 20))

    if is_quebec:
        # Relevé 1 (Québec)
        box_data = [
            ["Case", "Description", "Montant"],
            ["A", "Revenus d'emploi", f"{data.get('income', 0):,.2f} $"],
            ["B", "Cotisation au RRQ", f"{data.get('cpp', 0):,.2f} $"],
            ["C", "Cotisation à l'assurance emploi", f"{data.get('ei', 0):,.2f} $"],
            ["E", "Impôt du Québec retenu", f"{data.get('tax_prov', 0):,.2f} $"],
            ["P", "Impôt fédéral retenu", f"{data.get('tax_fed', 0):,.2f} $"],
        ]
    else:
        # T4 (autres provinces)
        box_data = [
            ["Case", "Description", "Montant"],
            ["14", "Revenus d'emploi", f"{data.get('income', 0):,.2f} $"],
            ["16", "Cotisations de l'employé au RPC", f"{data.get('cpp', 0):,.2f} $"],
            ["18", "Cotisations de l'employé à l'AE", f"{data.get('ei', 0):,.2f} $"],
            ["22", "Impôt sur le revenu retenu", f"{data.get('tax_fed', 0) + data.get('tax_prov', 0):,.2f} $"],
        ]

    table = Table(box_data, colWidths=[60, 280, 120])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.darkblue),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (0, -1), 'CENTER'),
        ('ALIGN', (2, 0), (2, -1), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
    ]))
    elements.append(table)
    elements.append(Spacer(1, 30))

    disclaimer = Paragraph(
        "<i>Ce document est généré à titre informatif seulement et ne remplace pas les formulaires officiels de l'ARC ou de Revenu Québec.</i>",
        styles['Normal']
    )
    elements.append(disclaimer)

    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()


def analyze_bank_statement_from_text(text: str) -> Dict[str, float]:
    income, expenses, count_income, count_expense = 0.0, 0.0, 0, 0
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
    return {"income": income, "expenses": expenses, "count_income": count_income, "count_expense": count_expense, "balance_change": income + expenses}


# ==============================
# HANDLERS
# ==============================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (
        f"Salut {user.first_name or ''} 👋\n\n"
        "Je suis ton bot de **calcul de paie** avec les *vrais paliers d'imposition 2024*.\n\n"
        "📌 Commandes :\n"
        "• /payroll – Talon de paie (PDF)\n"
        "• /hourly – Calculateur horaire\n"
        "• /t4 – Générer un T4/Relevé 1\n"
        "• /statement – Analyser un relevé bancaire\n"
        "• /files – Gérer tes fichiers\n"
        "• /help – Aide\n\n"
        "✅ *Paliers d'imposition 2024 inclus!*"
    )
    await update.message.reply_markdown(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🧾 **Fonctions du bot**\n\n"
        "1️⃣ /payroll – Calcul de paie\n"
        "   • Vrais paliers d'imposition 2024\n"
        "   • Crédits personnels de base\n"
        "   • PDF téléchargeable\n\n"
        "2️⃣ /hourly – Calculateur horaire\n"
        "   • Taux horaire × heures\n"
        "   • Temps supplémentaire (1.5x)\n\n"
        "3️⃣ /t4 – Rapport T4/Relevé 1\n"
        "   • Génère un formulaire fiscal\n"
        "   • Format PDF officiel\n\n"
        "4️⃣ /statement – Relevé bancaire\n"
        "5️⃣ /files – Gestion fichiers\n\n"
        "⚠️ *À titre informatif seulement.*"
    )
    await update.message.reply_markdown(text)


# ==============================
# PAYROLL CONVERSATION
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
        "🧾 *Calcul de paie (Paliers 2024)*\n\nChoisis la province :",
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
        gross = float(update.message.text.replace(",", ".").replace(" ", "").replace("$", ""))
    except ValueError:
        await update.message.reply_text("❌ Montant invalide.")
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
        "🧾 *Talon de paie (Paliers 2024)*\n\n"
        f"Province : *{result.province}*\n"
        f"Fréquence : *{freq_label}*\n"
        f"Brut : *{result.gross:,.2f} $* (Annuel: {result.annual_gross:,.2f} $)\n\n"
        f"Impôt fédéral : *{result.federal_tax:,.2f} $*\n"
        f"Impôt provincial : *{result.provincial_tax:,.2f} $*\n"
        f"AE : *{result.ei:,.2f} $*\n"
        f"RPC/RRQ : *{result.pension:,.2f} $*\n"
        "─────────────────────\n"
        f"*NET : {result.net:,.2f} $*"
    )
    await query.edit_message_text(text, parse_mode="Markdown")

    pdf_bytes = generate_payroll_pdf(result)
    await query.message.reply_document(
        document=io.BytesIO(pdf_bytes),
        filename=f"talon_paie_{datetime.now().strftime('%Y%m%d')}.pdf",
        caption="📄 Talon de paie (vrais paliers 2024)"
    )
    user_id = update.effective_user.id
    save_file_to_db(user_id, f"talon_paie_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf", "pdf", pdf_bytes)
    return ConversationHandler.END


async def payroll_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Annulé.")
    return ConversationHandler.END


# ==============================
# HOURLY CALCULATOR
# ==============================

async def hourly_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("QC", callback_data="hprov_QC"),
         InlineKeyboardButton("ON", callback_data="hprov_ON"),
         InlineKeyboardButton("BC", callback_data="hprov_BC")],
        [InlineKeyboardButton("AB", callback_data="hprov_AB"),
         InlineKeyboardButton("MB", callback_data="hprov_MB"),
         InlineKeyboardButton("Autre", callback_data="hprov_OTHER")],
    ]
    await update.message.reply_text(
        "⏰ *Calculateur horaire*\n\nChoisis la province :",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    return HOURLY_PROVINCE


async def hourly_set_province(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    province = query.data.replace("hprov_", "")
    context.user_data["hourly_province"] = province if province != "OTHER" else "ON"
    await query.edit_message_text(
        f"Province : *{province}*\n\nEntre ton *taux horaire* (ex: 25.50) :",
        parse_mode="Markdown",
    )
    return HOURLY_RATE


async def hourly_set_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rate = float(update.message.text.replace(",", ".").replace("$", "").replace(" ", ""))
    except ValueError:
        await update.message.reply_text("❌ Taux invalide.")
        return HOURLY_RATE
    if rate <= 0:
        await update.message.reply_text("❌ Le taux doit être positif.")
        return HOURLY_RATE
    context.user_data["hourly_rate"] = rate
    await update.message.reply_text(
        f"Taux : *{rate:.2f} $/h*\n\nCombien d'*heures régulières* cette période ?",
        parse_mode="Markdown",
    )
    return HOURLY_HOURS


async def hourly_set_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        hours = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("❌ Nombre invalide.")
        return HOURLY_HOURS
    if hours < 0:
        await update.message.reply_text("❌ Les heures doivent être positives.")
        return HOURLY_HOURS
    context.user_data["hourly_hours"] = hours
    keyboard = [
        [InlineKeyboardButton("0h", callback_data="ot_0"),
         InlineKeyboardButton("5h", callback_data="ot_5"),
         InlineKeyboardButton("10h", callback_data="ot_10")],
        [InlineKeyboardButton("15h", callback_data="ot_15"),
         InlineKeyboardButton("20h", callback_data="ot_20"),
         InlineKeyboardButton("Autre", callback_data="ot_other")],
    ]
    await update.message.reply_text(
        "Combien d'*heures supplémentaires* (temps et demi) ?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    return HOURLY_OVERTIME


async def hourly_set_overtime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ot_data = query.data.replace("ot_", "")

    if ot_data == "other":
        await query.edit_message_text("Entre le nombre d'heures supplémentaires :")
        return HOURLY_OVERTIME

    overtime = float(ot_data)
    return await calculate_hourly_result(query, context, overtime)


async def hourly_set_overtime_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        overtime = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("❌ Nombre invalide.")
        return HOURLY_OVERTIME

    # Create a fake query object for the function
    class FakeQuery:
        message = update.message
        async def edit_message_text(self, text, parse_mode=None):
            await self.message.reply_text(text, parse_mode=parse_mode)

    return await calculate_hourly_result(FakeQuery(), context, overtime)


async def calculate_hourly_result(query, context, overtime: float):
    province = context.user_data["hourly_province"]
    rate = context.user_data["hourly_rate"]
    hours = context.user_data["hourly_hours"]

    regular_pay = hours * rate
    overtime_pay = overtime * rate * 1.5
    gross = regular_pay + overtime_pay

    # Calculer sur base hebdomadaire pour la paie
    result = calculate_payroll(province, gross, "weekly")

    text = (
        "⏰ *Résultat - Calculateur horaire*\n\n"
        f"Taux horaire : *{rate:.2f} $/h*\n"
        f"Heures régulières : *{hours:.1f}h* = {regular_pay:,.2f} $\n"
        f"Heures supp. (1.5x) : *{overtime:.1f}h* = {overtime_pay:,.2f} $\n"
        "─────────────────────\n"
        f"Brut total : *{gross:,.2f} $*\n\n"
        f"Impôt fédéral : -{result.federal_tax:,.2f} $\n"
        f"Impôt provincial : -{result.provincial_tax:,.2f} $\n"
        f"AE : -{result.ei:,.2f} $\n"
        f"RPC/RRQ : -{result.pension:,.2f} $\n"
        "─────────────────────\n"
        f"*NET : {result.net:,.2f} $*"
    )
    await query.message.reply_text(text, parse_mode="Markdown")
    return ConversationHandler.END


async def hourly_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Annulé.")
    return ConversationHandler.END


# ==============================
# T4 / RELEVÉ 1 GENERATOR
# ==============================

async def t4_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Québec (Relevé 1)", callback_data="t4prov_QC")],
        [InlineKeyboardButton("Autre province (T4)", callback_data="t4prov_OTHER")],
    ]
    await update.message.reply_text(
        "📋 *Générateur T4 / Relevé 1*\n\nChoisis le type de formulaire :",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    return T4_NAME


async def t4_set_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    province = "QC" if query.data == "t4prov_QC" else "ON"
    context.user_data["t4_province"] = province
    await query.edit_message_text(
        "Entre le *nom complet de l'employé* :",
        parse_mode="Markdown",
    )
    return T4_NAME


async def t4_set_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "t4_province" not in context.user_data:
        context.user_data["t4_province"] = "ON"
    context.user_data["t4_name"] = update.message.text
    await update.message.reply_text(
        "Entre le *NAS* (ou XXX-XXX-XXX si tu préfères ne pas l'inclure) :",
        parse_mode="Markdown",
    )
    return T4_SIN


async def t4_set_sin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["t4_sin"] = update.message.text
    await update.message.reply_text(
        "Entre le *nom de l'employeur* :",
        parse_mode="Markdown",
    )
    return T4_EMPLOYER


async def t4_set_employer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["t4_employer"] = update.message.text
    await update.message.reply_text(
        "Entre le *revenu d'emploi total* (annuel) :",
        parse_mode="Markdown",
    )
    return T4_INCOME


async def t4_set_income(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        income = float(update.message.text.replace(",", ".").replace(" ", "").replace("$", ""))
    except ValueError:
        await update.message.reply_text("❌ Montant invalide.")
        return T4_INCOME
    context.user_data["t4_income"] = income
    await update.message.reply_text(
        "Entre l'*impôt fédéral retenu* (annuel) :",
        parse_mode="Markdown",
    )
    return T4_TAX_FED


async def t4_set_tax_fed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tax = float(update.message.text.replace(",", ".").replace(" ", "").replace("$", ""))
    except ValueError:
        await update.message.reply_text("❌ Montant invalide.")
        return T4_TAX_FED
    context.user_data["t4_tax_fed"] = tax
    await update.message.reply_text(
        "Entre l'*impôt provincial retenu* (annuel) :",
        parse_mode="Markdown",
    )
    return T4_TAX_PROV


async def t4_set_tax_prov(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tax = float(update.message.text.replace(",", ".").replace(" ", "").replace("$", ""))
    except ValueError:
        await update.message.reply_text("❌ Montant invalide.")
        return T4_TAX_PROV
    context.user_data["t4_tax_prov"] = tax
    await update.message.reply_text(
        "Entre les *cotisations AE* (annuel) :",
        parse_mode="Markdown",
    )
    return T4_EI


async def t4_set_ei(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        ei = float(update.message.text.replace(",", ".").replace(" ", "").replace("$", ""))
    except ValueError:
        await update.message.reply_text("❌ Montant invalide.")
        return T4_EI
    context.user_data["t4_ei"] = ei
    await update.message.reply_text(
        "Entre les *cotisations RPC/RRQ* (annuel) :",
        parse_mode="Markdown",
    )
    return T4_CPP


async def t4_set_cpp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cpp = float(update.message.text.replace(",", ".").replace(" ", "").replace("$", ""))
    except ValueError:
        await update.message.reply_text("❌ Montant invalide.")
        return T4_CPP
    context.user_data["t4_cpp"] = cpp

    # Générer le PDF
    data = {
        "province": context.user_data["t4_province"],
        "name": context.user_data["t4_name"],
        "sin": context.user_data["t4_sin"],
        "employer": context.user_data["t4_employer"],
        "income": context.user_data["t4_income"],
        "tax_fed": context.user_data["t4_tax_fed"],
        "tax_prov": context.user_data["t4_tax_prov"],
        "ei": context.user_data["t4_ei"],
        "cpp": cpp,
    }

    pdf_bytes = generate_t4_pdf(data)
    doc_type = "releve1" if data["province"] == "QC" else "t4"
    filename = f"{doc_type}_{datetime.now().strftime('%Y')}.pdf"

    await update.message.reply_document(
        document=io.BytesIO(pdf_bytes),
        filename=filename,
        caption=f"📋 {'Relevé 1' if data['province'] == 'QC' else 'T4'} généré!"
    )

    user_id = update.effective_user.id
    save_file_to_db(user_id, filename, "pdf", pdf_bytes)

    return ConversationHandler.END


async def t4_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Annulé.")
    return ConversationHandler.END


# ==============================
# STATEMENT CONVERSATION
# ==============================

async def statement_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_markdown(
        "🏦 *Analyse de relevé bancaire*\n\n"
        "Envoie du texte ou un fichier `.txt` / `.csv`\n"
        "Format : `+1500.25` (revenu) ou `-45.80` (dépense)"
    )
    return STMT_WAIT_TEXT


async def statement_process_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = analyze_bank_statement_from_text(update.message.text)
    await update.message.reply_markdown(
        f"📊 *Résultat*\n\n"
        f"Revenus : *{stats['count_income']}* = *{stats['income']:,.2f} $*\n"
        f"Dépenses : *{stats['count_expense']}* = *{stats['expenses']:,.2f} $*\n\n"
        f"Variation : *{stats['balance_change']:,.2f} $*"
    )
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
    await update.message.reply_markdown(
        f"📊 *{doc.file_name}*\n\n"
        f"Revenus : *{stats['count_income']}* = *{stats['income']:,.2f} $*\n"
        f"Dépenses : *{stats['count_expense']}* = *{stats['expenses']:,.2f} $*\n\n"
        f"Variation : *{stats['balance_change']:,.2f} $*"
    )
    return ConversationHandler.END


async def statement_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Annulé.")
    return ConversationHandler.END


# ==============================
# FILES CONVERSATION
# ==============================

async def files_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📤 Upload", callback_data="file_upload")],
        [InlineKeyboardButton("📥 Mes fichiers", callback_data="file_list")],
        [InlineKeyboardButton("✏️ Éditer CSV", callback_data="file_edit")],
        [InlineKeyboardButton("🗑️ Supprimer", callback_data="file_delete")],
    ]
    await update.message.reply_text(
        "📁 *Gestion de fichiers*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    return FILE_ACTION


async def files_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data
    user_id = update.effective_user.id

    if action == "file_upload":
        await query.edit_message_text("📤 Envoie un fichier CSV, PDF ou TXT.")
        return FILE_UPLOAD

    files = get_user_files(user_id)
    if not files:
        await query.edit_message_text("📂 Aucun fichier.")
        return ConversationHandler.END

    if action == "file_edit":
        files = [f for f in files if f['file_type'] == 'csv']
        if not files:
            await query.edit_message_text("📂 Aucun CSV.")
            return ConversationHandler.END

    keyboard = []
    prefix = "edit_" if action == "file_edit" else "del_" if action == "file_delete" else "dl_"
    for f in files[:10]:
        keyboard.append([InlineKeyboardButton(f['file_name'], callback_data=f"{prefix}{f['id']}")])
    keyboard.append([InlineKeyboardButton("❌ Fermer", callback_data="file_close")])

    titles = {"file_list": "📂 Tes fichiers:", "file_edit": "✏️ Éditer:", "file_delete": "🗑️ Supprimer:"}
    await query.edit_message_text(titles.get(action, "📂"), reply_markup=InlineKeyboardMarkup(keyboard))
    return FILE_SELECT if action != "file_edit" else CSV_EDIT_SELECT


async def files_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc or not doc.file_name.lower().endswith((".csv", ".pdf", ".txt")):
        await update.message.reply_text("❌ CSV, PDF ou TXT seulement.")
        return FILE_UPLOAD
    file = await doc.get_file()
    file_bytes = await file.download_as_bytearray()
    if save_file_to_db(update.effective_user.id, doc.file_name, doc.file_name.split(".")[-1].lower(), bytes(file_bytes)):
        await update.message.reply_text(f"✅ *{doc.file_name}* sauvegardé!", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Erreur.")
    return ConversationHandler.END


async def files_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data == "file_close":
        await query.edit_message_text("👍")
        return ConversationHandler.END

    if data.startswith("dl_"):
        file = get_file_by_id(int(data[3:]), user_id)
        if file:
            await query.message.reply_document(document=io.BytesIO(bytes(file['file_content'])), filename=file['file_name'])
        return ConversationHandler.END

    if data.startswith("del_"):
        delete_file_from_db(int(data[4:]), user_id)
        await query.edit_message_text("✅ Supprimé!")
        return ConversationHandler.END

    return ConversationHandler.END


async def csv_edit_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "file_close":
        await query.edit_message_text("👍")
        return ConversationHandler.END

    if data.startswith("edit_"):
        file = get_file_by_id(int(data[5:]), update.effective_user.id)
        if not file:
            await query.edit_message_text("❌ Non trouvé.")
            return ConversationHandler.END
        context.user_data["edit_file_id"] = int(data[5:])
        content = bytes(file['file_content']).decode('utf-8', errors='ignore')
        rows = list(csv.reader(io.StringIO(content)))
        if len(rows) < 1:
            await query.edit_message_text("❌ CSV vide.")
            return ConversationHandler.END
        context.user_data["csv_rows"] = rows
        preview = "📄 *CSV:*\n\n"
        for i, row in enumerate(rows[:5]):
            preview += f"`{i}: {', '.join(row[:3])}{'...' if len(row) > 3 else ''}`\n"
        preview += "\n*Numéro de ligne à modifier:*"
        await query.edit_message_text(preview, parse_mode="Markdown")
        return CSV_EDIT_ROW

    return ConversationHandler.END


async def csv_edit_row(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        row_num = int(update.message.text)
    except ValueError:
        await update.message.reply_text("❌ Numéro invalide.")
        return CSV_EDIT_ROW
    rows = context.user_data.get("csv_rows", [])
    if row_num < 0 or row_num >= len(rows):
        await update.message.reply_text(f"❌ 0-{len(rows)-1} seulement.")
        return CSV_EDIT_ROW
    context.user_data["edit_row_num"] = row_num
    await update.message.reply_markdown(f"📝 Ligne {row_num}: `{', '.join(rows[row_num])}`\n\n*Nouvelle ligne (virgules):*")
    return CSV_EDIT_VALUE


async def csv_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_values = [v.strip() for v in update.message.text.split(",")]
    rows = context.user_data["csv_rows"]
    rows[context.user_data["edit_row_num"]] = new_values
    output = io.StringIO()
    csv.writer(output).writerows(rows)
    if update_file_content(context.user_data["edit_file_id"], update.effective_user.id, output.getvalue().encode('utf-8')):
        await update.message.reply_text("✅ Modifié!")
    else:
        await update.message.reply_text("❌ Erreur.")
    return ConversationHandler.END


async def files_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Annulé.")
    return ConversationHandler.END


# ==============================
# MAIN
# ==============================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set!")

    init_database()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))

    # Payroll
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("payroll", payroll_start)],
        states={
            PAY_PROVINCE: [CallbackQueryHandler(payroll_set_province, pattern="^prov_")],
            PAY_GROSS: [MessageHandler(filters.TEXT & ~filters.COMMAND, payroll_set_gross)],
            PAY_FREQUENCY: [CallbackQueryHandler(payroll_set_frequency, pattern="^freq_")],
        },
        fallbacks=[CommandHandler("cancel", payroll_cancel)],
    ))

    # Hourly
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("hourly", hourly_start)],
        states={
            HOURLY_PROVINCE: [CallbackQueryHandler(hourly_set_province, pattern="^hprov_")],
            HOURLY_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, hourly_set_rate)],
            HOURLY_HOURS: [MessageHandler(filters.TEXT & ~filters.COMMAND, hourly_set_hours)],
            HOURLY_OVERTIME: [
                CallbackQueryHandler(hourly_set_overtime, pattern="^ot_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, hourly_set_overtime_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", hourly_cancel)],
    ))

    # T4
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("t4", t4_start)],
        states={
            T4_NAME: [
                CallbackQueryHandler(t4_set_type, pattern="^t4prov_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, t4_set_name),
            ],
            T4_SIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, t4_set_sin)],
            T4_EMPLOYER: [MessageHandler(filters.TEXT & ~filters.COMMAND, t4_set_employer)],
            T4_INCOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, t4_set_income)],
            T4_TAX_FED: [MessageHandler(filters.TEXT & ~filters.COMMAND, t4_set_tax_fed)],
            T4_TAX_PROV: [MessageHandler(filters.TEXT & ~filters.COMMAND, t4_set_tax_prov)],
            T4_EI: [MessageHandler(filters.TEXT & ~filters.COMMAND, t4_set_ei)],
            T4_CPP: [MessageHandler(filters.TEXT & ~filters.COMMAND, t4_set_cpp)],
        },
        fallbacks=[CommandHandler("cancel", t4_cancel)],
    ))

    # Statement
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("statement", statement_start)],
        states={
            STMT_WAIT_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, statement_process_text),
                MessageHandler(filters.Document.ALL, statement_process_document),
            ],
        },
        fallbacks=[CommandHandler("cancel", statement_cancel)],
    ))

    # Files
    app.add_handler(ConversationHandler(
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
    ))

    logger.info("Bot démarré...")
    app.run_polling()


if __name__ == "__main__":
    main()
