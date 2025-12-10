import logging
import os
import io
import json
from dataclasses import dataclass, asdict, field
from typing import Dict, Optional, List, Any
from datetime import datetime
from decimal import Decimal
import psycopg2
from psycopg2.extras import RealDictCursor
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =========================================================
# CONVERSATION STATES
# =========================================================

(
    MAIN_MENU,
    SELECT_TEMPLATE,
    # Basic form fields
    FORM_FIRST_NAME,
    FORM_LAST_NAME,
    FORM_ADDRESS,
    FORM_CITY,
    FORM_POSTAL_CODE,
    FORM_UNIT,
    FORM_PHONE,
    # Payroll specific
    PAYROLL_EMPLOYER,
    PAYROLL_SALARY,
    PAYROLL_PERIOD,
    PAYROLL_PROVINCE,
    PAYROLL_HOURS,
    PAYROLL_RATE,
    # Bank specific
    BANK_NAME,
    BANK_ACCOUNT,
    BANK_BALANCE,
    BANK_TRANSACTIONS,
    # Bill specific
    BILL_COMPANY,
    BILL_AMOUNT,
    BILL_DUE_DATE,
    BILL_SERVICE,
    # T4/T4A specific
    T4_EMPLOYER_NAME,
    T4_EMPLOYER_BN,
    T4_EMPLOYMENT_INCOME,
    T4_CPP_CONTRIBUTION,
    T4_EI_PREMIUM,
    T4_TAX_DEDUCTED,
    T4_YEAR,
    T4_PROVINCE,
    T4_OTHER_INCOME,
    # Employment Letter specific
    LETTER_EMPLOYER_NAME,
    LETTER_EMPLOYER_ADDRESS,
    LETTER_JOB_TITLE,
    LETTER_START_DATE,
    LETTER_SALARY,
    LETTER_EMPLOYMENT_TYPE,
    LETTER_PURPOSE,
    LETTER_END_DATE,
    # Confirmation
    CONFIRM,
) = range(42)

# =========================================================
# TEMPLATES
# =========================================================

TEMPLATES = {
    "payroll": {
        "name": "PAYROLL",
        "description": "Talons de paie et documents de remuneration",
        "templates": [
            {"id": "pay_standard", "name": "Talon de paie standard", "desc": "Format classique avec toutes les deductions"},
            {"id": "pay_detailed", "name": "Talon de paie detaille", "desc": "Inclut heures, overtime, bonus"},
            {"id": "pay_simple", "name": "Talon de paie simplifie", "desc": "Format minimaliste"},
            {"id": "pay_biweekly", "name": "Talon bi-hebdomadaire", "desc": "Format aux 2 semaines"},
        ]
    },
    "bank": {
        "name": "BANK STATEMENT",
        "description": "Releves bancaires et documents financiers",
        "templates": [
            {"id": "bank_monthly", "name": "Releve mensuel", "desc": "Releve de compte standard"},
            {"id": "bank_detailed", "name": "Releve detaille", "desc": "Avec categorisation des depenses"},
            {"id": "bank_summary", "name": "Sommaire financier", "desc": "Vue d'ensemble des finances"},
            {"id": "bank_proof", "name": "Preuve de fonds", "desc": "Attestation de solde"},
        ]
    },
    "bill": {
        "name": "BILL STATEMENT",
        "description": "Factures et releves de paiement",
        "templates": [
            {"id": "bill_utility", "name": "Facture services publics", "desc": "Hydro, gaz, eau"},
            {"id": "bill_telecom", "name": "Facture telecom", "desc": "Telephone, internet, cable"},
            {"id": "bill_rent", "name": "Recu de loyer", "desc": "Confirmation de paiement loyer"},
            {"id": "bill_invoice", "name": "Facture commerciale", "desc": "Facture professionnelle"},
        ]
    },
    "t4": {
        "name": "T4 / T1",
        "description": "Releves fiscaux canadiens",
        "templates": [
            {"id": "t4_standard", "name": "T4 - Revenus d'emploi", "desc": "Releve d'impot standard employe"},
            {"id": "t1_general", "name": "T1 - Declaration de revenus", "desc": "Declaration annuelle generale"},
            {"id": "rl1_quebec", "name": "RL-1 Quebec", "desc": "Releve 1 pour residents du Quebec"},
            {"id": "t4_summary", "name": "Sommaire T4", "desc": "Resume annuel des revenus"},
        ]
    },
    "employment_letter": {
        "name": "EMPLOYMENT LETTER",
        "description": "Lettres d'emploi et attestations",
        "templates": [
            {"id": "letter_confirmation", "name": "Confirmation d'emploi", "desc": "Lettre confirmant l'emploi actuel"},
            {"id": "letter_reference", "name": "Lettre de reference", "desc": "Recommandation professionnelle"},
            {"id": "letter_income", "name": "Attestation de revenus", "desc": "Confirmation du salaire"},
            {"id": "letter_termination", "name": "Lettre de fin d'emploi", "desc": "Confirmation de depart"},
        ]
    },
}

# =========================================================
# FORM DATA CLASS
# =========================================================

@dataclass
class FormData:
    # User info
    user_id: int = 0
    username: str = ""

    # Category and template
    category: str = ""
    template_id: str = ""

    # Basic fields
    first_name: str = ""
    last_name: str = ""
    address: str = ""
    city: str = ""
    postal_code: str = ""
    unit: str = ""
    phone: str = ""

    # Payroll fields
    employer_name: str = ""
    salary: str = ""
    pay_period: str = ""
    province: str = ""
    hours: str = ""
    hourly_rate: str = ""

    # Bank fields
    bank_name: str = ""
    account_number: str = ""
    balance: str = ""
    transactions: str = ""

    # Bill fields
    company_name: str = ""
    amount: str = ""
    due_date: str = ""
    service_type: str = ""

    # T4/T4A fields
    t4_employer_name: str = ""
    t4_employer_bn: str = ""
    employment_income: str = ""
    cpp_contribution: str = ""
    ei_premium: str = ""
    tax_deducted: str = ""
    tax_year: str = ""
    t4_province: str = ""
    other_income: str = ""

    # Employment Letter fields
    letter_employer_name: str = ""
    letter_employer_address: str = ""
    job_title: str = ""
    start_date: str = ""
    letter_salary: str = ""
    employment_type: str = ""
    letter_purpose: str = ""
    end_date: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)

# =========================================================
# DATABASE FUNCTIONS
# =========================================================

def get_db_connection():
    """Get database connection."""
    if not DATABASE_URL:
        logger.warning("DATABASE_URL not set")
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
        return

    try:
        with conn.cursor() as cur:
            # Documents table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS documents (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    username VARCHAR(255),
                    category VARCHAR(50) NOT NULL,
                    template_id VARCHAR(50) NOT NULL,
                    form_data JSONB NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Users table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT UNIQUE NOT NULL,
                    username VARCHAR(255),
                    first_name VARCHAR(255),
                    last_name VARCHAR(255),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    document_count INT DEFAULT 0
                )
            """)

            # Create indexes
            cur.execute("CREATE INDEX IF NOT EXISTS idx_documents_user_id ON documents(user_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_users_user_id ON users(user_id)")

            conn.commit()
            logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Database init error: {e}")
        conn.rollback()
    finally:
        conn.close()

def save_user(user_id: int, username: str, first_name: str, last_name: str):
    """Save or update user in database."""
    conn = get_db_connection()
    if not conn:
        return

    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_id, username, first_name, last_name, last_active)
                VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    last_active = CURRENT_TIMESTAMP
            """, (user_id, username, first_name, last_name))
            conn.commit()
    except Exception as e:
        logger.error(f"Save user error: {e}")
        conn.rollback()
    finally:
        conn.close()

def save_document(form_data: FormData) -> Optional[int]:
    """Save document to database."""
    conn = get_db_connection()
    if not conn:
        return None

    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO documents (user_id, username, category, template_id, form_data)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (
                form_data.user_id,
                form_data.username,
                form_data.category,
                form_data.template_id,
                json.dumps(form_data.to_dict())
            ))
            doc_id = cur.fetchone()['id']

            # Update user document count
            cur.execute("""
                UPDATE users SET document_count = document_count + 1
                WHERE user_id = %s
            """, (form_data.user_id,))

            conn.commit()
            logger.info(f"Document saved with ID: {doc_id}")
            return doc_id
    except Exception as e:
        logger.error(f"Save document error: {e}")
        conn.rollback()
        return None
    finally:
        conn.close()

def get_user_documents(user_id: int, limit: int = 10) -> List[Dict]:
    """Get user's recent documents."""
    conn = get_db_connection()
    if not conn:
        return []

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, category, template_id, created_at
                FROM documents
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT %s
            """, (user_id, limit))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"Get documents error: {e}")
        return []
    finally:
        conn.close()

# =========================================================
# PDF GENERATION FUNCTIONS
# =========================================================

def generate_payroll_pdf(data: FormData) -> io.BytesIO:
    """Generate payroll PDF."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, topMargin=0.5*inch, bottomMargin=0.5*inch)
    styles = getSampleStyleSheet()
    elements = []

    # Title
    title_style = ParagraphStyle('Title', parent=styles['Heading1'], fontSize=18, alignment=1, spaceAfter=20)
    elements.append(Paragraph("PAY STUB / TALON DE PAIE", title_style))
    elements.append(Spacer(1, 20))

    # Employer info
    employer_data = [
        ["Employer / Employeur:", data.employer_name],
        ["Pay Period / Periode:", data.pay_period],
        ["Province:", data.province],
    ]
    employer_table = Table(employer_data, colWidths=[2.5*inch, 4*inch])
    employer_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    elements.append(employer_table)
    elements.append(Spacer(1, 20))

    # Employee info
    employee_data = [
        ["Employee / Employe:", f"{data.first_name} {data.last_name}"],
        ["Address / Adresse:", f"{data.address}, {data.unit}" if data.unit else data.address],
        ["City / Ville:", f"{data.city}, {data.postal_code}"],
        ["Phone / Tel:", data.phone],
    ]
    employee_table = Table(employee_data, colWidths=[2.5*inch, 4*inch])
    employee_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    elements.append(employee_table)
    elements.append(Spacer(1, 20))

    # Earnings
    try:
        gross = float(data.salary.replace(',', '').replace('$', ''))
    except:
        gross = 0

    cpp = round(gross * 0.0595, 2)
    ei = round(gross * 0.0163, 2)
    tax = round(gross * 0.15, 2)
    net = round(gross - cpp - ei - tax, 2)

    earnings_data = [
        ["Description", "Earnings / Gains", "Deductions"],
        ["Gross Pay / Salaire brut", f"${gross:,.2f}", ""],
        ["CPP/RPC", "", f"${cpp:,.2f}"],
        ["EI/AE", "", f"${ei:,.2f}"],
        ["Income Tax / Impot", "", f"${tax:,.2f}"],
        ["", "", ""],
        ["NET PAY / SALAIRE NET", f"${net:,.2f}", ""],
    ]

    earnings_table = Table(earnings_data, colWidths=[3*inch, 2*inch, 1.5*inch])
    earnings_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
    ]))
    elements.append(earnings_table)

    # Footer
    elements.append(Spacer(1, 30))
    footer_style = ParagraphStyle('Footer', parent=styles['Normal'], fontSize=8, alignment=1, textColor=colors.grey)
    elements.append(Paragraph(f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M')}", footer_style))

    doc.build(elements)
    buffer.seek(0)
    return buffer

def generate_bank_statement_pdf(data: FormData) -> io.BytesIO:
    """Generate bank statement PDF."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, topMargin=0.5*inch, bottomMargin=0.5*inch)
    styles = getSampleStyleSheet()
    elements = []

    # Header
    title_style = ParagraphStyle('Title', parent=styles['Heading1'], fontSize=18, alignment=1, spaceAfter=20)
    elements.append(Paragraph("BANK STATEMENT / RELEVE BANCAIRE", title_style))
    elements.append(Spacer(1, 10))

    # Bank info
    bank_info = [
        [data.bank_name.upper()],
        [f"Account / Compte: {data.account_number}"],
        [f"Statement Date: {datetime.now().strftime('%B %Y')}"],
    ]
    bank_table = Table(bank_info, colWidths=[6.5*inch])
    bank_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (0, 0), 14),
        ('FONTSIZE', (0, 1), (-1, -1), 10),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    elements.append(bank_table)
    elements.append(Spacer(1, 20))

    # Account holder info
    holder_data = [
        ["Account Holder / Titulaire:", f"{data.first_name} {data.last_name}"],
        ["Address / Adresse:", data.address],
        ["City / Ville:", f"{data.city}, {data.postal_code}"],
    ]
    holder_table = Table(holder_data, colWidths=[2.5*inch, 4*inch])
    holder_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    elements.append(holder_table)
    elements.append(Spacer(1, 20))

    # Balance
    try:
        balance = float(data.balance.replace(',', '').replace('$', ''))
    except:
        balance = 0

    balance_data = [
        ["Current Balance / Solde actuel", f"${balance:,.2f}"],
    ]
    balance_table = Table(balance_data, colWidths=[4*inch, 2.5*inch])
    balance_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#27ae60')),
        ('TEXTCOLOR', (0, 0), (-1, -1), colors.white),
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 14),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 15),
        ('TOPPADDING', (0, 0), (-1, -1), 15),
    ]))
    elements.append(balance_table)

    # Footer
    elements.append(Spacer(1, 30))
    footer_style = ParagraphStyle('Footer', parent=styles['Normal'], fontSize=8, alignment=1, textColor=colors.grey)
    elements.append(Paragraph(f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M')}", footer_style))

    doc.build(elements)
    buffer.seek(0)
    return buffer

def generate_bill_pdf(data: FormData) -> io.BytesIO:
    """Generate bill/invoice PDF."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, topMargin=0.5*inch, bottomMargin=0.5*inch)
    styles = getSampleStyleSheet()
    elements = []

    # Header
    title_style = ParagraphStyle('Title', parent=styles['Heading1'], fontSize=18, alignment=1, spaceAfter=20)
    elements.append(Paragraph("INVOICE / FACTURE", title_style))
    elements.append(Spacer(1, 10))

    # Company info
    company_style = ParagraphStyle('Company', parent=styles['Heading2'], fontSize=14, alignment=1)
    elements.append(Paragraph(data.company_name.upper(), company_style))
    elements.append(Spacer(1, 20))

    # Bill to
    bill_to_data = [
        ["Bill To / Facturer a:", ""],
        [f"{data.first_name} {data.last_name}", ""],
        [data.address, ""],
        [f"{data.city}, {data.postal_code}", ""],
    ]
    bill_table = Table(bill_to_data, colWidths=[4*inch, 2.5*inch])
    bill_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    elements.append(bill_table)
    elements.append(Spacer(1, 20))

    # Invoice details
    try:
        amount = float(data.amount.replace(',', '').replace('$', ''))
    except:
        amount = 0

    tax = round(amount * 0.15, 2)
    total = round(amount + tax, 2)

    invoice_data = [
        ["Description", "Amount / Montant"],
        [data.service_type or "Service", f"${amount:,.2f}"],
        ["Tax / Taxes (15%)", f"${tax:,.2f}"],
        ["TOTAL", f"${total:,.2f}"],
    ]

    invoice_table = Table(invoice_data, colWidths=[4.5*inch, 2*inch])
    invoice_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3498db')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
    ]))
    elements.append(invoice_table)
    elements.append(Spacer(1, 20))

    # Due date
    due_style = ParagraphStyle('Due', parent=styles['Normal'], fontSize=12, alignment=1)
    elements.append(Paragraph(f"<b>Due Date / Echeance:</b> {data.due_date}", due_style))

    # Footer
    elements.append(Spacer(1, 30))
    footer_style = ParagraphStyle('Footer', parent=styles['Normal'], fontSize=8, alignment=1, textColor=colors.grey)
    elements.append(Paragraph(f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M')}", footer_style))

    doc.build(elements)
    buffer.seek(0)
    return buffer

def generate_t4_pdf(data: FormData) -> io.BytesIO:
    """Generate T4/T4A PDF."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, topMargin=0.5*inch, bottomMargin=0.5*inch)
    styles = getSampleStyleSheet()
    elements = []

    # Header
    title_style = ParagraphStyle('Title', parent=styles['Heading1'], fontSize=16, alignment=1, spaceAfter=10)

    if data.template_id == "t1_general":
        elements.append(Paragraph("T1 GENERAL - INCOME TAX AND BENEFIT RETURN", title_style))
        elements.append(Paragraph("DECLARATION DE REVENUS ET DE PRESTATIONS", title_style))
    elif data.template_id == "rl1_quebec":
        elements.append(Paragraph("RL-1 - RELEVE 1", title_style))
        elements.append(Paragraph("REVENUS D'EMPLOI ET REVENUS DIVERS", title_style))
    else:
        elements.append(Paragraph("T4 - STATEMENT OF REMUNERATION PAID", title_style))
        elements.append(Paragraph("ETAT DE LA REMUNERATION PAYEE", title_style))

    elements.append(Spacer(1, 10))

    # Year
    year_style = ParagraphStyle('Year', parent=styles['Heading2'], fontSize=14, alignment=1)
    elements.append(Paragraph(f"Tax Year / Annee d'imposition: {data.tax_year}", year_style))
    elements.append(Spacer(1, 20))

    # Employer info
    employer_data = [
        ["Employer's name / Nom de l'employeur:", data.t4_employer_name],
        ["Business Number / Numero d'entreprise:", data.t4_employer_bn],
    ]
    employer_table = Table(employer_data, colWidths=[3*inch, 3.5*inch])
    employer_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('BOX', (0, 0), (-1, -1), 1, colors.black),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.grey),
    ]))
    elements.append(employer_table)
    elements.append(Spacer(1, 15))

    # Employee info
    employee_data = [
        ["Employee's name / Nom de l'employe:", f"{data.first_name} {data.last_name}"],
        ["Address / Adresse:", f"{data.address}, {data.city}, {data.postal_code}"],
        ["Province:", data.t4_province],
    ]
    employee_table = Table(employee_data, colWidths=[3*inch, 3.5*inch])
    employee_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('BOX', (0, 0), (-1, -1), 1, colors.black),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.grey),
    ]))
    elements.append(employee_table)
    elements.append(Spacer(1, 20))

    # Income boxes
    try:
        income = float(data.employment_income.replace(',', '').replace('$', ''))
        cpp = float(data.cpp_contribution.replace(',', '').replace('$', '')) if data.cpp_contribution else 0
        ei = float(data.ei_premium.replace(',', '').replace('$', '')) if data.ei_premium else 0
        tax = float(data.tax_deducted.replace(',', '').replace('$', '')) if data.tax_deducted else 0
    except:
        income = cpp = ei = tax = 0

    boxes_data = [
        ["Box / Case", "Description", "Amount / Montant"],
        ["14", "Employment income / Revenus d'emploi", f"${income:,.2f}"],
        ["16", "Employee's CPP contributions / Cotisations RPC", f"${cpp:,.2f}"],
        ["18", "Employee's EI premiums / Cotisations AE", f"${ei:,.2f}"],
        ["22", "Income tax deducted / Impot retenu", f"${tax:,.2f}"],
    ]

    boxes_table = Table(boxes_data, colWidths=[1*inch, 3.5*inch, 2*inch])
    boxes_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#c0392b')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('ALIGN', (0, 0), (0, -1), 'CENTER'),
        ('ALIGN', (2, 0), (2, -1), 'RIGHT'),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
    ]))
    elements.append(boxes_table)

    # Footer
    elements.append(Spacer(1, 30))
    footer_style = ParagraphStyle('Footer', parent=styles['Normal'], fontSize=8, alignment=1, textColor=colors.grey)
    elements.append(Paragraph("This is a copy for the employee / Copie de l'employe", footer_style))
    elements.append(Paragraph(f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M')}", footer_style))

    doc.build(elements)
    buffer.seek(0)
    return buffer

def generate_employment_letter_pdf(data: FormData) -> io.BytesIO:
    """Generate employment letter PDF."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, topMargin=1*inch, bottomMargin=1*inch)
    styles = getSampleStyleSheet()
    elements = []

    # Letterhead
    header_style = ParagraphStyle('Header', parent=styles['Heading1'], fontSize=14, alignment=0, spaceAfter=5)
    elements.append(Paragraph(data.letter_employer_name.upper(), header_style))

    address_style = ParagraphStyle('Address', parent=styles['Normal'], fontSize=10, spaceAfter=20)
    elements.append(Paragraph(data.letter_employer_address, address_style))
    elements.append(Spacer(1, 20))

    # Date
    date_style = ParagraphStyle('Date', parent=styles['Normal'], fontSize=11, spaceAfter=20)
    elements.append(Paragraph(datetime.now().strftime("%B %d, %Y"), date_style))
    elements.append(Spacer(1, 10))

    # Title based on template
    title_style = ParagraphStyle('Title', parent=styles['Heading2'], fontSize=12, alignment=1, spaceAfter=20)

    if data.template_id == "letter_confirmation":
        elements.append(Paragraph("EMPLOYMENT CONFIRMATION LETTER", title_style))
        elements.append(Paragraph("LETTRE DE CONFIRMATION D'EMPLOI", title_style))
    elif data.template_id == "letter_reference":
        elements.append(Paragraph("LETTER OF REFERENCE", title_style))
        elements.append(Paragraph("LETTRE DE REFERENCE", title_style))
    elif data.template_id == "letter_income":
        elements.append(Paragraph("INCOME VERIFICATION LETTER", title_style))
        elements.append(Paragraph("ATTESTATION DE REVENUS", title_style))
    else:
        elements.append(Paragraph("EMPLOYMENT TERMINATION LETTER", title_style))
        elements.append(Paragraph("LETTRE DE FIN D'EMPLOI", title_style))

    elements.append(Spacer(1, 20))

    # Body
    body_style = ParagraphStyle('Body', parent=styles['Normal'], fontSize=11, leading=16, spaceAfter=15)

    elements.append(Paragraph("To Whom It May Concern / A qui de droit,", body_style))
    elements.append(Spacer(1, 10))

    if data.template_id == "letter_confirmation":
        body_text = f"""
        This letter confirms that <b>{data.first_name} {data.last_name}</b> is currently employed
        at <b>{data.letter_employer_name}</b> as a <b>{data.job_title}</b>.
        <br/><br/>
        Employment start date / Date de debut: <b>{data.start_date}</b><br/>
        Employment type / Type d'emploi: <b>{data.employment_type}</b><br/>
        Annual salary / Salaire annuel: <b>${data.letter_salary}</b>
        <br/><br/>
        Cette lettre confirme que <b>{data.first_name} {data.last_name}</b> est presentement employe(e)
        chez <b>{data.letter_employer_name}</b> en tant que <b>{data.job_title}</b>.
        """
    elif data.template_id == "letter_income":
        body_text = f"""
        This letter certifies that <b>{data.first_name} {data.last_name}</b> is employed at our company
        and earns an annual salary of <b>${data.letter_salary}</b>.
        <br/><br/>
        Position / Poste: <b>{data.job_title}</b><br/>
        Start date / Date de debut: <b>{data.start_date}</b><br/>
        Employment type / Type d'emploi: <b>{data.employment_type}</b>
        <br/><br/>
        Cette lettre certifie que <b>{data.first_name} {data.last_name}</b> est employe(e) dans notre
        entreprise et gagne un salaire annuel de <b>${data.letter_salary}</b>.
        """
    elif data.template_id == "letter_termination":
        body_text = f"""
        This letter confirms that <b>{data.first_name} {data.last_name}</b> was employed at
        <b>{data.letter_employer_name}</b> from <b>{data.start_date}</b> to <b>{data.end_date}</b>.
        <br/><br/>
        Position held / Poste occupe: <b>{data.job_title}</b><br/>
        Final salary / Dernier salaire: <b>${data.letter_salary}</b>
        <br/><br/>
        Cette lettre confirme que <b>{data.first_name} {data.last_name}</b> etait employe(e) chez
        <b>{data.letter_employer_name}</b> du <b>{data.start_date}</b> au <b>{data.end_date}</b>.
        """
    else:  # letter_reference
        body_text = f"""
        I am pleased to recommend <b>{data.first_name} {data.last_name}</b> who worked at
        <b>{data.letter_employer_name}</b> as a <b>{data.job_title}</b>.
        <br/><br/>
        Employment period / Periode d'emploi: <b>{data.start_date}</b> - Present<br/>
        <br/>
        {data.first_name} has demonstrated excellent skills and dedication during their time with us.
        <br/><br/>
        Je recommande avec plaisir <b>{data.first_name} {data.last_name}</b> qui a travaille chez
        <b>{data.letter_employer_name}</b> en tant que <b>{data.job_title}</b>.
        """

    elements.append(Paragraph(body_text, body_style))
    elements.append(Spacer(1, 30))

    # Purpose if provided
    if data.letter_purpose:
        purpose_text = f"<b>Purpose / Objet:</b> {data.letter_purpose}"
        elements.append(Paragraph(purpose_text, body_style))
        elements.append(Spacer(1, 20))

    # Signature
    elements.append(Paragraph("Sincerely / Cordialement,", body_style))
    elements.append(Spacer(1, 30))
    elements.append(Paragraph("_________________________", body_style))
    elements.append(Paragraph("Authorized Signature / Signature autorisee", address_style))
    elements.append(Paragraph(data.letter_employer_name, address_style))

    # Footer
    elements.append(Spacer(1, 30))
    footer_style = ParagraphStyle('Footer', parent=styles['Normal'], fontSize=8, alignment=1, textColor=colors.grey)
    elements.append(Paragraph(f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M')}", footer_style))

    doc.build(elements)
    buffer.seek(0)
    return buffer

def generate_pdf(data: FormData) -> io.BytesIO:
    """Route to appropriate PDF generator."""
    if data.category == "payroll":
        return generate_payroll_pdf(data)
    elif data.category == "bank":
        return generate_bank_statement_pdf(data)
    elif data.category == "bill":
        return generate_bill_pdf(data)
    elif data.category == "t4":
        return generate_t4_pdf(data)
    elif data.category == "employment_letter":
        return generate_employment_letter_pdf(data)
    else:
        return generate_payroll_pdf(data)

# =========================================================
# KEYBOARD BUILDERS
# =========================================================

def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Build main category selection keyboard."""
    buttons = []
    for cat_key, cat_data in TEMPLATES.items():
        buttons.append([InlineKeyboardButton(cat_data["name"], callback_data=f"CAT_{cat_key}")])
    buttons.append([InlineKeyboardButton("Mes documents", callback_data="MY_DOCS")])
    return InlineKeyboardMarkup(buttons)

def build_template_keyboard(category: str) -> InlineKeyboardMarkup:
    """Build template selection keyboard."""
    buttons = []
    templates = TEMPLATES.get(category, {}).get("templates", [])
    for tpl in templates:
        buttons.append([InlineKeyboardButton(tpl["name"], callback_data=f"TPL_{category}_{tpl['id']}")])
    buttons.append([InlineKeyboardButton("< Retour", callback_data="BACK_MAIN")])
    return InlineKeyboardMarkup(buttons)

def build_skip_keyboard() -> InlineKeyboardMarkup:
    """Build skip button keyboard."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("Skip / Passer", callback_data="SKIP")]])

def build_confirm_keyboard() -> InlineKeyboardMarkup:
    """Build confirmation keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Confirmer / Confirm", callback_data="CONFIRM_YES")],
        [InlineKeyboardButton("Modifier / Edit", callback_data="CONFIRM_EDIT")],
        [InlineKeyboardButton("Annuler / Cancel", callback_data="CONFIRM_CANCEL")],
    ])

def build_province_keyboard() -> InlineKeyboardMarkup:
    """Build province selection keyboard."""
    provinces = ["ON", "QC", "BC", "AB", "MB", "SK", "NS", "NB", "NL", "PE", "NT", "YT", "NU"]
    buttons = []
    row = []
    for i, prov in enumerate(provinces):
        row.append(InlineKeyboardButton(prov, callback_data=f"PROV_{prov}"))
        if (i + 1) % 4 == 0:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)

def build_employment_type_keyboard() -> InlineKeyboardMarkup:
    """Build employment type keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Full-time / Temps plein", callback_data="EMPTYPE_Full-time")],
        [InlineKeyboardButton("Part-time / Temps partiel", callback_data="EMPTYPE_Part-time")],
        [InlineKeyboardButton("Contract / Contrat", callback_data="EMPTYPE_Contract")],
    ])

# =========================================================
# HANDLERS
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /start command."""
    user = update.effective_user

    # Save user to database
    save_user(user.id, user.username or "", user.first_name or "", user.last_name or "")

    # Initialize form data
    context.user_data["form"] = FormData(user_id=user.id, username=user.username or "")

    text = (
        f"Salut {user.first_name or 'ami'}!\n\n"
        "Bienvenue sur DOCYWAY BOT\n"
        "Generateur de documents professionnels\n\n"
        "Choisis une categorie:\n"
        "- PAYROLL - Talons de paie\n"
        "- BANK STATEMENT - Releves bancaires\n"
        "- BILL STATEMENT - Factures\n"
        "- T4 / T1 - Releves fiscaux\n"
        "- EMPLOYMENT LETTER - Lettres d'emploi"
    )

    if update.message:
        await update.message.reply_text(text, reply_markup=build_main_menu_keyboard())
    else:
        await update.callback_query.edit_message_text(text, reply_markup=build_main_menu_keyboard())

    return MAIN_MENU

async def handle_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle category selection."""
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "BACK_MAIN":
        return await start(update, context)

    if data == "MY_DOCS":
        return await show_my_documents(update, context)

    if not data.startswith("CAT_"):
        return MAIN_MENU

    category = data.replace("CAT_", "")
    form: FormData = context.user_data.get("form", FormData())
    form.category = category
    context.user_data["form"] = form

    cat_info = TEMPLATES.get(category, {})
    text = (
        f"Categorie: {cat_info.get('name', category)}\n"
        f"{cat_info.get('description', '')}\n\n"
        "Choisis un template:"
    )

    await query.edit_message_text(text, reply_markup=build_template_keyboard(category))
    return SELECT_TEMPLATE

async def handle_template(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle template selection."""
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "BACK_MAIN":
        return await start(update, context)

    if not data.startswith("TPL_"):
        return SELECT_TEMPLATE

    parts = data.split("_", 2)
    if len(parts) < 3:
        return SELECT_TEMPLATE

    category = parts[1]
    template_id = parts[2]

    form: FormData = context.user_data.get("form", FormData())
    form.category = category
    form.template_id = template_id
    context.user_data["form"] = form

    # Find template name
    template_name = template_id
    for tpl in TEMPLATES.get(category, {}).get("templates", []):
        if tpl["id"] == template_id:
            template_name = tpl["name"]
            break

    await query.edit_message_text(
        f"Template: {template_name}\n\n"
        "Commençons! / Let's start!\n\n"
        "First name / Prenom:"
    )
    return FORM_FIRST_NAME

async def show_my_documents(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show user's documents."""
    query = update.callback_query
    user_id = update.effective_user.id

    docs = get_user_documents(user_id)

    if not docs:
        text = "Tu n'as pas encore de documents.\n\nUtilise /start pour en creer un!"
    else:
        text = "Tes documents recents:\n\n"
        for doc in docs:
            cat_name = TEMPLATES.get(doc['category'], {}).get('name', doc['category'])
            date_str = doc['created_at'].strftime('%Y-%m-%d %H:%M')
            text += f"- {cat_name} - {doc['template_id']} ({date_str})\n"

    buttons = [[InlineKeyboardButton("< Retour", callback_data="BACK_MAIN")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    return MAIN_MENU

# Basic form handlers
async def handle_first_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())
    form.first_name = update.message.text.strip()
    context.user_data["form"] = form
    await update.message.reply_text("Last name / Nom de famille:")
    return FORM_LAST_NAME

async def handle_last_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())
    form.last_name = update.message.text.strip()
    context.user_data["form"] = form
    await update.message.reply_text("Address / Adresse:")
    return FORM_ADDRESS

async def handle_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())
    form.address = update.message.text.strip()
    context.user_data["form"] = form
    await update.message.reply_text("City / Ville:")
    return FORM_CITY

async def handle_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())
    form.city = update.message.text.strip()
    context.user_data["form"] = form
    await update.message.reply_text("Postal code / Code postal:")
    return FORM_POSTAL_CODE

async def handle_postal_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())
    form.postal_code = update.message.text.strip()
    context.user_data["form"] = form
    await update.message.reply_text("Unit / Appartement (optional):", reply_markup=build_skip_keyboard())
    return FORM_UNIT

async def handle_unit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())

    if update.callback_query:
        await update.callback_query.answer()
        form.unit = ""
    else:
        form.unit = update.message.text.strip()

    context.user_data["form"] = form

    msg = update.callback_query.message if update.callback_query else update.message
    await msg.reply_text("Phone / Telephone (optional):", reply_markup=build_skip_keyboard())
    return FORM_PHONE

async def handle_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())

    if update.callback_query:
        await update.callback_query.answer()
        form.phone = ""
        msg = update.callback_query.message
    else:
        form.phone = update.message.text.strip()
        msg = update.message

    context.user_data["form"] = form

    # Route to category-specific questions
    if form.category == "payroll":
        await msg.reply_text("Employer name / Nom de l'employeur:")
        return PAYROLL_EMPLOYER
    elif form.category == "bank":
        await msg.reply_text("Bank name / Nom de la banque:")
        return BANK_NAME
    elif form.category == "bill":
        await msg.reply_text("Company name / Nom de la compagnie:")
        return BILL_COMPANY
    elif form.category == "t4":
        await msg.reply_text("Employer name / Nom de l'employeur:")
        return T4_EMPLOYER_NAME
    elif form.category == "employment_letter":
        await msg.reply_text("Employer name / Nom de l'employeur:")
        return LETTER_EMPLOYER_NAME
    else:
        return await show_confirmation(update, context)

# Payroll handlers
async def handle_payroll_employer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())
    form.employer_name = update.message.text.strip()
    context.user_data["form"] = form
    await update.message.reply_text("Gross salary / Salaire brut (ex: 2500):")
    return PAYROLL_SALARY

async def handle_payroll_salary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())
    form.salary = update.message.text.strip()
    context.user_data["form"] = form
    await update.message.reply_text("Pay period / Periode de paie (ex: 2025-01-01 to 2025-01-15):")
    return PAYROLL_PERIOD

async def handle_payroll_period(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())
    form.pay_period = update.message.text.strip()
    context.user_data["form"] = form
    await update.message.reply_text("Province:", reply_markup=build_province_keyboard())
    return PAYROLL_PROVINCE

async def handle_payroll_province(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())

    if update.callback_query:
        await update.callback_query.answer()
        form.province = update.callback_query.data.replace("PROV_", "")
    else:
        form.province = update.message.text.strip()

    context.user_data["form"] = form
    return await show_confirmation(update, context)

# Bank handlers
async def handle_bank_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())
    form.bank_name = update.message.text.strip()
    context.user_data["form"] = form
    await update.message.reply_text("Account number / Numero de compte (last 4 digits):")
    return BANK_ACCOUNT

async def handle_bank_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())
    form.account_number = update.message.text.strip()
    context.user_data["form"] = form
    await update.message.reply_text("Current balance / Solde actuel (ex: 5000):")
    return BANK_BALANCE

async def handle_bank_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())
    form.balance = update.message.text.strip()
    context.user_data["form"] = form
    return await show_confirmation(update, context)

# Bill handlers
async def handle_bill_company(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())
    form.company_name = update.message.text.strip()
    context.user_data["form"] = form
    await update.message.reply_text("Service type / Type de service (ex: Electricity, Internet):")
    return BILL_SERVICE

async def handle_bill_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())
    form.service_type = update.message.text.strip()
    context.user_data["form"] = form
    await update.message.reply_text("Amount due / Montant du (ex: 150):")
    return BILL_AMOUNT

async def handle_bill_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())
    form.amount = update.message.text.strip()
    context.user_data["form"] = form
    await update.message.reply_text("Due date / Date d'echeance (ex: 2025-01-31):")
    return BILL_DUE_DATE

async def handle_bill_due_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())
    form.due_date = update.message.text.strip()
    context.user_data["form"] = form
    return await show_confirmation(update, context)

# T4/T4A handlers
async def handle_t4_employer_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())
    form.t4_employer_name = update.message.text.strip()
    context.user_data["form"] = form
    await update.message.reply_text("Employer Business Number / Numero d'entreprise (ex: 123456789RC0001):")
    return T4_EMPLOYER_BN

async def handle_t4_employer_bn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())
    form.t4_employer_bn = update.message.text.strip()
    context.user_data["form"] = form
    await update.message.reply_text("Tax year / Annee d'imposition (ex: 2024):")
    return T4_YEAR

async def handle_t4_year(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())
    form.tax_year = update.message.text.strip()
    context.user_data["form"] = form
    await update.message.reply_text("Province:", reply_markup=build_province_keyboard())
    return T4_PROVINCE

async def handle_t4_province(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())

    if update.callback_query:
        await update.callback_query.answer()
        form.t4_province = update.callback_query.data.replace("PROV_", "")
        msg = update.callback_query.message
    else:
        form.t4_province = update.message.text.strip()
        msg = update.message

    context.user_data["form"] = form
    await msg.reply_text("Employment income / Revenus d'emploi (Box 14):")
    return T4_EMPLOYMENT_INCOME

async def handle_t4_employment_income(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())
    form.employment_income = update.message.text.strip()
    context.user_data["form"] = form
    await update.message.reply_text("CPP contributions / Cotisations RPC (Box 16):", reply_markup=build_skip_keyboard())
    return T4_CPP_CONTRIBUTION

async def handle_t4_cpp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())

    if update.callback_query:
        await update.callback_query.answer()
        form.cpp_contribution = ""
        msg = update.callback_query.message
    else:
        form.cpp_contribution = update.message.text.strip()
        msg = update.message

    context.user_data["form"] = form
    await msg.reply_text("EI premiums / Cotisations AE (Box 18):", reply_markup=build_skip_keyboard())
    return T4_EI_PREMIUM

async def handle_t4_ei(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())

    if update.callback_query:
        await update.callback_query.answer()
        form.ei_premium = ""
        msg = update.callback_query.message
    else:
        form.ei_premium = update.message.text.strip()
        msg = update.message

    context.user_data["form"] = form
    await msg.reply_text("Income tax deducted / Impot retenu (Box 22):", reply_markup=build_skip_keyboard())
    return T4_TAX_DEDUCTED

async def handle_t4_tax(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())

    if update.callback_query:
        await update.callback_query.answer()
        form.tax_deducted = ""
    else:
        form.tax_deducted = update.message.text.strip()

    context.user_data["form"] = form
    return await show_confirmation(update, context)

# Employment Letter handlers
async def handle_letter_employer_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())
    form.letter_employer_name = update.message.text.strip()
    context.user_data["form"] = form
    await update.message.reply_text("Employer address / Adresse de l'employeur:")
    return LETTER_EMPLOYER_ADDRESS

async def handle_letter_employer_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())
    form.letter_employer_address = update.message.text.strip()
    context.user_data["form"] = form
    await update.message.reply_text("Job title / Titre du poste:")
    return LETTER_JOB_TITLE

async def handle_letter_job_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())
    form.job_title = update.message.text.strip()
    context.user_data["form"] = form
    await update.message.reply_text("Start date / Date de debut (ex: 2023-01-15):")
    return LETTER_START_DATE

async def handle_letter_start_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())
    form.start_date = update.message.text.strip()
    context.user_data["form"] = form
    await update.message.reply_text("Annual salary / Salaire annuel:")
    return LETTER_SALARY

async def handle_letter_salary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())
    form.letter_salary = update.message.text.strip()
    context.user_data["form"] = form
    await update.message.reply_text("Employment type / Type d'emploi:", reply_markup=build_employment_type_keyboard())
    return LETTER_EMPLOYMENT_TYPE

async def handle_letter_employment_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())

    if update.callback_query:
        await update.callback_query.answer()
        form.employment_type = update.callback_query.data.replace("EMPTYPE_", "")
        msg = update.callback_query.message
    else:
        form.employment_type = update.message.text.strip()
        msg = update.message

    context.user_data["form"] = form

    # If termination letter, ask for end date
    if form.template_id == "letter_termination":
        await msg.reply_text("End date / Date de fin (ex: 2025-01-15):")
        return LETTER_END_DATE

    await msg.reply_text("Purpose / Objet de la lettre (optional):", reply_markup=build_skip_keyboard())
    return LETTER_PURPOSE

async def handle_letter_end_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())
    form.end_date = update.message.text.strip()
    context.user_data["form"] = form
    await update.message.reply_text("Purpose / Objet de la lettre (optional):", reply_markup=build_skip_keyboard())
    return LETTER_PURPOSE

async def handle_letter_purpose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    form: FormData = context.user_data.get("form", FormData())

    if update.callback_query:
        await update.callback_query.answer()
        form.letter_purpose = ""
    else:
        form.letter_purpose = update.message.text.strip()

    context.user_data["form"] = form
    return await show_confirmation(update, context)

# Confirmation
async def show_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show confirmation screen."""
    form: FormData = context.user_data.get("form", FormData())

    cat_name = TEMPLATES.get(form.category, {}).get("name", form.category)
    template_name = form.template_id
    for tpl in TEMPLATES.get(form.category, {}).get("templates", []):
        if tpl["id"] == form.template_id:
            template_name = tpl["name"]
            break

    text = (
        f"CONFIRMATION\n\n"
        f"Category: {cat_name}\n"
        f"Template: {template_name}\n\n"
        f"Name: {form.first_name} {form.last_name}\n"
        f"Address: {form.address}\n"
        f"City: {form.city}, {form.postal_code}\n"
    )

    if form.category == "payroll":
        text += f"\nEmployer: {form.employer_name}\nSalary: ${form.salary}\nPeriod: {form.pay_period}\n"
    elif form.category == "bank":
        text += f"\nBank: {form.bank_name}\nAccount: {form.account_number}\nBalance: ${form.balance}\n"
    elif form.category == "bill":
        text += f"\nCompany: {form.company_name}\nAmount: ${form.amount}\nDue: {form.due_date}\n"
    elif form.category == "t4":
        text += f"\nEmployer: {form.t4_employer_name}\nYear: {form.tax_year}\nIncome: ${form.employment_income}\n"
    elif form.category == "employment_letter":
        text += f"\nEmployer: {form.letter_employer_name}\nPosition: {form.job_title}\nSalary: ${form.letter_salary}\n"

    text += "\nGenerer le document?"

    msg = update.callback_query.message if update.callback_query else update.message
    await msg.reply_text(text, reply_markup=build_confirm_keyboard())
    return CONFIRM

async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle confirmation response."""
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "CONFIRM_CANCEL":
        await query.edit_message_text("Annule! Utilise /start pour recommencer.")
        return ConversationHandler.END

    if data == "CONFIRM_EDIT":
        await query.edit_message_text("Utilise /start pour recommencer avec de nouvelles informations.")
        return ConversationHandler.END

    if data == "CONFIRM_YES":
        form: FormData = context.user_data.get("form", FormData())

        await query.edit_message_text("Generation du PDF en cours...")

        try:
            # Generate PDF
            pdf_buffer = generate_pdf(form)

            # Save to database
            doc_id = save_document(form)

            # Get template name for filename
            template_name = form.template_id
            for tpl in TEMPLATES.get(form.category, {}).get("templates", []):
                if tpl["id"] == form.template_id:
                    template_name = tpl["name"].replace(" ", "_")
                    break

            filename = f"{template_name}_{form.last_name}_{datetime.now().strftime('%Y%m%d')}.pdf"

            # Send PDF
            await query.message.reply_document(
                document=pdf_buffer,
                filename=filename,
                caption=f"Document genere!\n\nID: {doc_id or 'N/A'}\n\nUtilise /start pour en creer un autre."
            )

        except Exception as e:
            logger.error(f"PDF generation error: {e}")
            await query.message.reply_text(f"Erreur lors de la generation: {str(e)}\n\nUtilise /start pour reessayer.")

        return ConversationHandler.END

    return CONFIRM

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /cancel command."""
    await update.message.reply_text("Annule! Utilise /start pour recommencer.")
    return ConversationHandler.END

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    text = (
        "DOCYWAY BOT - Aide\n\n"
        "Ce bot genere des documents professionnels:\n\n"
        "- PAYROLL - Talons de paie\n"
        "- BANK STATEMENT - Releves bancaires\n"
        "- BILL STATEMENT - Factures\n"
        "- T4 / T1 - Releves fiscaux\n"
        "- EMPLOYMENT LETTER - Lettres d'emploi\n\n"
        "Commandes:\n"
        "/start - Demarrer\n"
        "/cancel - Annuler\n"
        "/help - Aide"
    )
    await update.message.reply_text(text)

# =========================================================
# MAIN
# =========================================================

def main():
    """Run the bot."""
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("Set BOT_TOKEN environment variable!")

    # Initialize database
    init_database()

    # Build application
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MAIN_MENU: [
                CallbackQueryHandler(handle_category, pattern=r"^(CAT_|MY_DOCS|BACK_MAIN)")
            ],
            SELECT_TEMPLATE: [
                CallbackQueryHandler(handle_template, pattern=r"^(TPL_|BACK_MAIN)")
            ],
            FORM_FIRST_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_first_name)
            ],
            FORM_LAST_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_last_name)
            ],
            FORM_ADDRESS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_address)
            ],
            FORM_CITY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_city)
            ],
            FORM_POSTAL_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_postal_code)
            ],
            FORM_UNIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unit),
                CallbackQueryHandler(handle_unit, pattern=r"^SKIP$")
            ],
            FORM_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_phone),
                CallbackQueryHandler(handle_phone, pattern=r"^SKIP$")
            ],
            # Payroll states
            PAYROLL_EMPLOYER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_payroll_employer)
            ],
            PAYROLL_SALARY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_payroll_salary)
            ],
            PAYROLL_PERIOD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_payroll_period)
            ],
            PAYROLL_PROVINCE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_payroll_province),
                CallbackQueryHandler(handle_payroll_province, pattern=r"^PROV_")
            ],
            # Bank states
            BANK_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_bank_name)
            ],
            BANK_ACCOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_bank_account)
            ],
            BANK_BALANCE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_bank_balance)
            ],
            # Bill states
            BILL_COMPANY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_bill_company)
            ],
            BILL_SERVICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_bill_service)
            ],
            BILL_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_bill_amount)
            ],
            BILL_DUE_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_bill_due_date)
            ],
            # T4 states
            T4_EMPLOYER_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_t4_employer_name)
            ],
            T4_EMPLOYER_BN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_t4_employer_bn)
            ],
            T4_YEAR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_t4_year)
            ],
            T4_PROVINCE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_t4_province),
                CallbackQueryHandler(handle_t4_province, pattern=r"^PROV_")
            ],
            T4_EMPLOYMENT_INCOME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_t4_employment_income)
            ],
            T4_CPP_CONTRIBUTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_t4_cpp),
                CallbackQueryHandler(handle_t4_cpp, pattern=r"^SKIP$")
            ],
            T4_EI_PREMIUM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_t4_ei),
                CallbackQueryHandler(handle_t4_ei, pattern=r"^SKIP$")
            ],
            T4_TAX_DEDUCTED: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_t4_tax),
                CallbackQueryHandler(handle_t4_tax, pattern=r"^SKIP$")
            ],
            # Employment Letter states
            LETTER_EMPLOYER_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_letter_employer_name)
            ],
            LETTER_EMPLOYER_ADDRESS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_letter_employer_address)
            ],
            LETTER_JOB_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_letter_job_title)
            ],
            LETTER_START_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_letter_start_date)
            ],
            LETTER_SALARY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_letter_salary)
            ],
            LETTER_EMPLOYMENT_TYPE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_letter_employment_type),
                CallbackQueryHandler(handle_letter_employment_type, pattern=r"^EMPTYPE_")
            ],
            LETTER_END_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_letter_end_date)
            ],
            LETTER_PURPOSE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_letter_purpose),
                CallbackQueryHandler(handle_letter_purpose, pattern=r"^SKIP$")
            ],
            # Confirmation
            CONFIRM: [
                CallbackQueryHandler(handle_confirmation, pattern=r"^CONFIRM_")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("help", help_command))

    logger.info("Bot starting...")
    application.run_polling()

if __name__ == "__main__":
    main()
