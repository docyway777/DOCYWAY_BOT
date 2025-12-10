"""
=============================================================================
BOT TELEGRAM PROFESSIONNEL - GÉNÉRATION DE DOCUMENTS
=============================================================================
Ce bot permet de générer des documents (Payroll, Bank Statement, Bill Statement)
à partir de templates prédéfinis et d'informations collectées via un formulaire.

Fonctionnalités:
- Menu principal avec 3 catégories
- Système de templates par catégorie
- Formulaire étape par étape
- Sauvegarde en base de données PostgreSQL
- Génération de documents PDF
- Gestion des fichiers utilisateur

Auteur: Bot Generator
Version: 2.0
=============================================================================
"""

import logging
import os
import io
import json
from dataclasses import dataclass, asdict
from typing import Dict, Optional, List
from datetime import datetime
from enum import Enum

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
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
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


# =============================================================================
# CONFIGURATION
# =============================================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

# Configuration du logging pour déboguer facilement
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# =============================================================================
# ÉTATS DE CONVERSATION
# États numérotés pour gérer le flux de la conversation
# =============================================================================

class States(Enum):
    """Énumération des états de conversation pour une meilleure lisibilité."""
    # Menu principal
    MAIN_MENU = 0
    SELECT_TEMPLATE = 1

    # Formulaire utilisateur (étapes)
    FORM_FIRST_NAME = 10
    FORM_LAST_NAME = 11
    FORM_ADDRESS = 12
    FORM_CITY = 13
    FORM_POSTAL_CODE = 14
    FORM_UNIT = 15
    FORM_PHONE = 16

    # Étapes spécifiques par catégorie
    PAYROLL_EMPLOYER = 20
    PAYROLL_SALARY = 21
    PAYROLL_PERIOD = 22
    PAYROLL_PROVINCE = 23

    BANK_ACCOUNT = 30
    BANK_TRANSACTIONS = 31

    BILL_COMPANY = 40
    BILL_AMOUNT = 41
    BILL_DUE_DATE = 42

    # Confirmation
    CONFIRM = 50


# Convertir enum en valeurs pour ConversationHandler
(
    MAIN_MENU, SELECT_TEMPLATE,
    FORM_FIRST_NAME, FORM_LAST_NAME, FORM_ADDRESS, FORM_CITY,
    FORM_POSTAL_CODE, FORM_UNIT, FORM_PHONE,
    PAYROLL_EMPLOYER, PAYROLL_SALARY, PAYROLL_PERIOD, PAYROLL_PROVINCE,
    BANK_ACCOUNT, BANK_TRANSACTIONS,
    BILL_COMPANY, BILL_AMOUNT, BILL_DUE_DATE,
    CONFIRM
) = range(19)


# =============================================================================
# DÉFINITION DES TEMPLATES
# Structure des templates disponibles par catégorie
# =============================================================================

TEMPLATES = {
    "payroll": {
        "name": "🧾 PAYROLL",
        "description": "Talons de paie et documents de rémunération",
        "templates": [
            {"id": "pay_standard", "name": "📄 Talon de paie standard", "desc": "Format classique avec toutes les déductions"},
            {"id": "pay_detailed", "name": "📊 Talon de paie détaillé", "desc": "Inclut heures, overtime, bonus"},
            {"id": "pay_simple", "name": "📝 Talon de paie simplifié", "desc": "Format minimaliste"},
            {"id": "pay_annual", "name": "📅 Relevé annuel (T4)", "desc": "Sommaire annuel des revenus"},
        ]
    },
    "bank": {
        "name": "🏦 BANK STATEMENT",
        "description": "Relevés bancaires et documents financiers",
        "templates": [
            {"id": "bank_monthly", "name": "📅 Relevé mensuel", "desc": "Relevé de compte standard"},
            {"id": "bank_detailed", "name": "📊 Relevé détaillé", "desc": "Avec catégorisation des dépenses"},
            {"id": "bank_summary", "name": "📈 Sommaire financier", "desc": "Vue d'ensemble des finances"},
            {"id": "bank_proof", "name": "✅ Preuve de fonds", "desc": "Attestation de solde"},
        ]
    },
    "bill": {
        "name": "📃 BILL STATEMENT",
        "description": "Factures et relevés de paiement",
        "templates": [
            {"id": "bill_utility", "name": "💡 Facture services publics", "desc": "Hydro, gaz, eau"},
            {"id": "bill_telecom", "name": "📱 Facture télécom", "desc": "Téléphone, internet, câble"},
            {"id": "bill_rent", "name": "🏠 Reçu de loyer", "desc": "Confirmation de paiement loyer"},
            {"id": "bill_invoice", "name": "🧾 Facture commerciale", "desc": "Facture professionnelle"},
        ]
    }
}


# =============================================================================
# CLASSE DE DONNÉES UTILISATEUR
# Stocke les informations collectées pendant le formulaire
# =============================================================================

@dataclass
class UserFormData:
    """Structure de données pour stocker les informations du formulaire."""
    # Informations de base
    first_name: str = ""
    last_name: str = ""
    address: str = ""
    city: str = ""
    postal_code: str = ""
    unit: str = ""
    phone: str = ""

    # Catégorie et template sélectionnés
    category: str = ""
    template_id: str = ""

    # Données spécifiques selon le template
    extra_data: Dict = None

    def __post_init__(self):
        if self.extra_data is None:
            self.extra_data = {}

    def to_dict(self) -> dict:
        """Convertit les données en dictionnaire pour la sauvegarde."""
        return asdict(self)

    def get_full_name(self) -> str:
        """Retourne le nom complet."""
        return f"{self.first_name} {self.last_name}".strip()

    def get_full_address(self) -> str:
        """Retourne l'adresse complète formatée."""
        parts = [self.address]
        if self.unit:
            parts.append(f"Unit {self.unit}")
        parts.append(f"{self.city}, {self.postal_code}")
        return "\n".join(parts)


# =============================================================================
# FONCTIONS DE BASE DE DONNÉES
# Gestion de la connexion et des opérations CRUD
# =============================================================================

def get_db_connection():
    """
    Établit une connexion à la base de données PostgreSQL.
    Retourne None si la connexion échoue ou si DATABASE_URL n'est pas défini.
    """
    if not DATABASE_URL:
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        return conn
    except Exception as e:
        logger.error(f"Erreur de connexion DB: {e}")
        return None


def init_database():
    """
    Initialise les tables de la base de données.
    Crée les tables si elles n'existent pas.
    """
    conn = get_db_connection()
    if not conn:
        logger.warning("Base de données non disponible - stockage désactivé")
        return

    try:
        cur = conn.cursor()

        # Table pour stocker les documents générés
        cur.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                user_name VARCHAR(255),
                category VARCHAR(50) NOT NULL,
                template_id VARCHAR(50) NOT NULL,
                form_data JSONB NOT NULL,
                file_content BYTEA,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Table pour stocker les fichiers utilisateur
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_files (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                file_name VARCHAR(255) NOT NULL,
                file_type VARCHAR(50) NOT NULL,
                file_content BYTEA NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Index pour améliorer les performances
        cur.execute("CREATE INDEX IF NOT EXISTS idx_documents_user_id ON documents(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_files_user_id ON user_files(user_id)")

        conn.commit()
        cur.close()
        conn.close()
        logger.info("Base de données initialisée avec succès")

    except Exception as e:
        logger.error(f"Erreur d'initialisation DB: {e}")


def save_document(user_id: int, user_name: str, form_data: UserFormData, pdf_content: bytes = None) -> bool:
    """
    Sauvegarde un document généré dans la base de données.

    Args:
        user_id: ID Telegram de l'utilisateur
        user_name: Nom d'utilisateur Telegram
        form_data: Données du formulaire
        pdf_content: Contenu PDF du document (optionnel)

    Returns:
        True si la sauvegarde réussit, False sinon
    """
    conn = get_db_connection()
    if not conn:
        return False

    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO documents (user_id, user_name, category, template_id, form_data, file_content)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (user_id, user_name, form_data.category, form_data.template_id,
             json.dumps(form_data.to_dict()), pdf_content)
        )
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Erreur de sauvegarde: {e}")
        return False


def get_user_documents(user_id: int, limit: int = 10) -> List[dict]:
    """
    Récupère les documents récents d'un utilisateur.

    Args:
        user_id: ID Telegram de l'utilisateur
        limit: Nombre maximum de documents à retourner

    Returns:
        Liste des documents
    """
    conn = get_db_connection()
    if not conn:
        return []

    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, category, template_id, created_at
            FROM documents
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (user_id, limit)
        )
        docs = cur.fetchall()
        cur.close()
        conn.close()
        return docs
    except Exception as e:
        logger.error(f"Erreur de récupération: {e}")
        return []


# =============================================================================
# FONCTIONS DE GÉNÉRATION PDF
# Création des documents PDF selon les templates
# =============================================================================

def generate_payroll_pdf(form_data: UserFormData) -> bytes:
    """
    Génère un talon de paie PDF basé sur les données du formulaire.

    Args:
        form_data: Données collectées via le formulaire

    Returns:
        Contenu PDF en bytes
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    elements = []

    # Style personnalisé pour le titre
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=20,
        spaceAfter=30,
        alignment=1,  # Centré
        textColor=colors.darkblue
    )

    # Titre du document
    template_name = next(
        (t["name"] for t in TEMPLATES["payroll"]["templates"] if t["id"] == form_data.template_id),
        "Talon de paie"
    )
    elements.append(Paragraph(template_name.replace("📄 ", "").replace("📊 ", "").replace("📝 ", "").replace("📅 ", ""), title_style))
    elements.append(Spacer(1, 20))

    # Informations de l'employé
    elements.append(Paragraph("<b>INFORMATIONS DE L'EMPLOYÉ</b>", styles['Heading2']))
    employee_data = [
        ["Nom complet:", form_data.get_full_name()],
        ["Adresse:", form_data.address],
        ["Ville:", f"{form_data.city}, {form_data.postal_code}"],
    ]
    if form_data.unit:
        employee_data.insert(2, ["Unité:", form_data.unit])
    if form_data.phone:
        employee_data.append(["Téléphone:", form_data.phone])

    emp_table = Table(employee_data, colWidths=[150, 350])
    emp_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
    ]))
    elements.append(emp_table)
    elements.append(Spacer(1, 20))

    # Informations de paie (exemple)
    elements.append(Paragraph("<b>DÉTAILS DE LA PAIE</b>", styles['Heading2']))

    extra = form_data.extra_data
    salary = float(extra.get('salary', 0))

    # Calculs de base (simplifié)
    federal_tax = salary * 0.15
    provincial_tax = salary * 0.10
    ei = min(salary * 0.0166, 40.34)
    cpp = min(salary * 0.0595, 148.75)
    net = salary - federal_tax - provincial_tax - ei - cpp

    pay_data = [
        ["Description", "Montant"],
        ["Salaire brut", f"{salary:,.2f} $"],
        ["", ""],
        ["DÉDUCTIONS", ""],
        ["Impôt fédéral", f"-{federal_tax:,.2f} $"],
        ["Impôt provincial", f"-{provincial_tax:,.2f} $"],
        ["Assurance emploi (AE)", f"-{ei:,.2f} $"],
        ["RPC/RRQ", f"-{cpp:,.2f} $"],
        ["", ""],
        ["NET À PAYER", f"{net:,.2f} $"],
    ]

    pay_table = Table(pay_data, colWidths=[350, 150])
    pay_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.darkblue),
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
    elements.append(pay_table)
    elements.append(Spacer(1, 30))

    # Informations employeur
    elements.append(Paragraph("<b>EMPLOYEUR</b>", styles['Heading2']))
    elements.append(Paragraph(extra.get('employer', 'N/A'), styles['Normal']))
    elements.append(Spacer(1, 10))
    elements.append(Paragraph(f"<i>Période: {extra.get('period', 'N/A')}</i>", styles['Normal']))
    elements.append(Paragraph(f"<i>Date: {datetime.now().strftime('%Y-%m-%d')}</i>", styles['Normal']))

    # Disclaimer
    elements.append(Spacer(1, 30))
    elements.append(Paragraph(
        "<i>Ce document est généré à titre informatif seulement.</i>",
        styles['Normal']
    ))

    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()


def generate_bank_statement_pdf(form_data: UserFormData) -> bytes:
    """Génère un relevé bancaire PDF."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    elements = []

    title_style = ParagraphStyle(
        'CustomTitle', parent=styles['Heading1'],
        fontSize=20, spaceAfter=30, alignment=1, textColor=colors.darkgreen
    )

    elements.append(Paragraph("RELEVÉ BANCAIRE", title_style))
    elements.append(Spacer(1, 20))

    # Informations du client
    elements.append(Paragraph("<b>INFORMATIONS DU TITULAIRE</b>", styles['Heading2']))
    client_data = [
        ["Nom:", form_data.get_full_name()],
        ["Adresse:", form_data.get_full_address().replace("\n", ", ")],
    ]
    if form_data.phone:
        client_data.append(["Téléphone:", form_data.phone])

    client_table = Table(client_data, colWidths=[150, 350])
    client_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    elements.append(client_table)
    elements.append(Spacer(1, 20))

    # Informations du compte
    extra = form_data.extra_data
    elements.append(Paragraph("<b>INFORMATIONS DU COMPTE</b>", styles['Heading2']))
    elements.append(Paragraph(f"Numéro de compte: {extra.get('account', 'XXXX-XXXX')}", styles['Normal']))
    elements.append(Paragraph(f"Date du relevé: {datetime.now().strftime('%Y-%m-%d')}", styles['Normal']))
    elements.append(Spacer(1, 20))

    # Sommaire
    elements.append(Paragraph("<b>SOMMAIRE</b>", styles['Heading2']))
    summary_data = [
        ["Description", "Montant"],
        ["Solde d'ouverture", "1,000.00 $"],
        ["Total des dépôts", "+2,500.00 $"],
        ["Total des retraits", "-1,200.00 $"],
        ["Solde de clôture", "2,300.00 $"],
    ]

    summary_table = Table(summary_data, colWidths=[350, 150])
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.darkgreen),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
    ]))
    elements.append(summary_table)

    elements.append(Spacer(1, 30))
    elements.append(Paragraph("<i>Ce document est généré à titre informatif seulement.</i>", styles['Normal']))

    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()


def generate_bill_pdf(form_data: UserFormData) -> bytes:
    """Génère une facture PDF."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    elements = []

    title_style = ParagraphStyle(
        'CustomTitle', parent=styles['Heading1'],
        fontSize=20, spaceAfter=30, alignment=1, textColor=colors.darkred
    )

    template_name = next(
        (t["name"] for t in TEMPLATES["bill"]["templates"] if t["id"] == form_data.template_id),
        "Facture"
    )
    elements.append(Paragraph(template_name.replace("💡 ", "").replace("📱 ", "").replace("🏠 ", "").replace("🧾 ", ""), title_style))
    elements.append(Spacer(1, 20))

    extra = form_data.extra_data

    # Informations du fournisseur
    elements.append(Paragraph(f"<b>{extra.get('company', 'Entreprise')}</b>", styles['Heading2']))
    elements.append(Spacer(1, 10))

    # Informations du client
    elements.append(Paragraph("<b>FACTURÉ À:</b>", styles['Heading3']))
    elements.append(Paragraph(form_data.get_full_name(), styles['Normal']))
    elements.append(Paragraph(form_data.get_full_address().replace("\n", "<br/>"), styles['Normal']))
    elements.append(Spacer(1, 20))

    # Détails de la facture
    amount = float(extra.get('amount', 0))
    tax = amount * 0.15
    total = amount + tax

    bill_data = [
        ["Description", "Montant"],
        ["Services", f"{amount:,.2f} $"],
        ["Taxes (TPS/TVQ)", f"{tax:,.2f} $"],
        ["TOTAL À PAYER", f"{total:,.2f} $"],
    ]

    bill_table = Table(bill_data, colWidths=[350, 150])
    bill_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.darkred),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('BACKGROUND', (0, -1), (-1, -1), colors.lightgrey),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
    ]))
    elements.append(bill_table)
    elements.append(Spacer(1, 20))

    # Date d'échéance
    elements.append(Paragraph(f"<b>Date d'échéance:</b> {extra.get('due_date', 'N/A')}", styles['Normal']))

    elements.append(Spacer(1, 30))
    elements.append(Paragraph("<i>Ce document est généré à titre informatif seulement.</i>", styles['Normal']))

    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()


def generate_document_pdf(form_data: UserFormData) -> bytes:
    """
    Fonction principale qui route vers le bon générateur selon la catégorie.

    Args:
        form_data: Données du formulaire

    Returns:
        Contenu PDF en bytes
    """
    if form_data.category == "payroll":
        return generate_payroll_pdf(form_data)
    elif form_data.category == "bank":
        return generate_bank_statement_pdf(form_data)
    elif form_data.category == "bill":
        return generate_bill_pdf(form_data)
    else:
        return generate_payroll_pdf(form_data)  # Défaut


# =============================================================================
# FONCTIONS UTILITAIRES
# Helpers pour construire les claviers et messages
# =============================================================================

def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    """
    Construit le clavier du menu principal avec les 3 catégories.

    Returns:
        InlineKeyboardMarkup avec les boutons de catégorie
    """
    keyboard = [
        [InlineKeyboardButton("🧾 PAYROLL", callback_data="cat_payroll")],
        [InlineKeyboardButton("🏦 BANK STATEMENT", callback_data="cat_bank")],
        [InlineKeyboardButton("📃 BILL STATEMENT", callback_data="cat_bill")],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_templates_keyboard(category: str) -> InlineKeyboardMarkup:
    """
    Construit le clavier avec les templates disponibles pour une catégorie.

    Args:
        category: Identifiant de la catégorie (payroll, bank, bill)

    Returns:
        InlineKeyboardMarkup avec les boutons de templates
    """
    templates = TEMPLATES.get(category, {}).get("templates", [])
    keyboard = []

    for template in templates:
        keyboard.append([
            InlineKeyboardButton(
                template["name"],
                callback_data=f"tpl_{template['id']}"
            )
        ])

    # Bouton retour
    keyboard.append([InlineKeyboardButton("⬅️ Retour", callback_data="back_main")])

    return InlineKeyboardMarkup(keyboard)


def build_skip_keyboard() -> InlineKeyboardMarkup:
    """Construit un clavier avec bouton 'Passer' pour les champs optionnels."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭️ Passer", callback_data="skip")]
    ])


def build_confirm_keyboard() -> InlineKeyboardMarkup:
    """Construit le clavier de confirmation finale."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirmer et générer", callback_data="confirm_yes")],
        [InlineKeyboardButton("✏️ Modifier", callback_data="confirm_edit")],
        [InlineKeyboardButton("❌ Annuler", callback_data="confirm_cancel")],
    ])


def build_province_keyboard() -> InlineKeyboardMarkup:
    """Construit le clavier de sélection de province."""
    keyboard = [
        [InlineKeyboardButton("QC", callback_data="prov_QC"),
         InlineKeyboardButton("ON", callback_data="prov_ON"),
         InlineKeyboardButton("BC", callback_data="prov_BC")],
        [InlineKeyboardButton("AB", callback_data="prov_AB"),
         InlineKeyboardButton("MB", callback_data="prov_MB"),
         InlineKeyboardButton("SK", callback_data="prov_SK")],
        [InlineKeyboardButton("NS", callback_data="prov_NS"),
         InlineKeyboardButton("NB", callback_data="prov_NB"),
         InlineKeyboardButton("Autre", callback_data="prov_OTHER")],
    ]
    return InlineKeyboardMarkup(keyboard)


def format_form_summary(form_data: UserFormData) -> str:
    """
    Formate un résumé des données du formulaire pour confirmation.

    Args:
        form_data: Données du formulaire

    Returns:
        Texte formaté en Markdown
    """
    template_name = "N/A"
    for cat_data in TEMPLATES.values():
        for tpl in cat_data.get("templates", []):
            if tpl["id"] == form_data.template_id:
                template_name = tpl["name"]
                break

    summary = f"""
📋 *RÉSUMÉ DE VOS INFORMATIONS*

*Template:* {template_name}

*Informations personnelles:*
• Prénom: {form_data.first_name}
• Nom: {form_data.last_name}
• Adresse: {form_data.address}
• Ville: {form_data.city}
• Code postal: {form_data.postal_code}
• Unité: {form_data.unit or 'N/A'}
• Téléphone: {form_data.phone or 'N/A'}
"""

    # Ajouter les données spécifiques selon la catégorie
    if form_data.category == "payroll" and form_data.extra_data:
        summary += f"""
*Informations de paie:*
• Employeur: {form_data.extra_data.get('employer', 'N/A')}
• Salaire: {form_data.extra_data.get('salary', 'N/A')} $
• Période: {form_data.extra_data.get('period', 'N/A')}
• Province: {form_data.extra_data.get('province', 'N/A')}
"""
    elif form_data.category == "bank" and form_data.extra_data:
        summary += f"""
*Informations bancaires:*
• Numéro de compte: {form_data.extra_data.get('account', 'N/A')}
"""
    elif form_data.category == "bill" and form_data.extra_data:
        summary += f"""
*Informations de facturation:*
• Entreprise: {form_data.extra_data.get('company', 'N/A')}
• Montant: {form_data.extra_data.get('amount', 'N/A')} $
• Date d'échéance: {form_data.extra_data.get('due_date', 'N/A')}
"""

    return summary


# =============================================================================
# HANDLERS - MENU PRINCIPAL
# Gestion du menu principal et de la sélection de catégorie
# =============================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Handler pour la commande /start.
    Affiche le menu principal avec les 3 catégories.
    """
    user = update.effective_user

    # Initialiser les données utilisateur
    context.user_data['form_data'] = UserFormData()

    welcome_text = f"""
👋 *Bienvenue {user.first_name or ''}!*

Je suis votre assistant pour générer des documents professionnels.

📌 *Choisissez une catégorie:*

🧾 *PAYROLL* - Talons de paie et documents de rémunération
🏦 *BANK STATEMENT* - Relevés bancaires
📃 *BILL STATEMENT* - Factures et relevés

Cliquez sur un bouton pour commencer:
"""

    await update.message.reply_text(
        welcome_text,
        reply_markup=build_main_menu_keyboard(),
        parse_mode="Markdown"
    )

    return MAIN_MENU


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler pour la commande /help."""
    help_text = """
📚 *GUIDE D'UTILISATION*

*Commandes disponibles:*
• /start - Démarrer et voir le menu principal
• /help - Afficher ce guide
• /history - Voir vos documents récents
• /cancel - Annuler l'opération en cours

*Comment ça marche:*
1️⃣ Choisissez une catégorie (Payroll, Bank, Bill)
2️⃣ Sélectionnez un template
3️⃣ Remplissez le formulaire étape par étape
4️⃣ Confirmez et recevez votre document PDF

*Conseils:*
• Les champs marqués (optionnel) peuvent être passés
• Utilisez /cancel pour annuler à tout moment
• Vos documents sont sauvegardés automatiquement

❓ *Questions?* Contactez le support.
"""
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler pour la commande /history - affiche les documents récents."""
    user_id = update.effective_user.id
    docs = get_user_documents(user_id)

    if not docs:
        await update.message.reply_text("📂 Vous n'avez aucun document sauvegardé.")
        return

    text = "📚 *VOS DOCUMENTS RÉCENTS:*\n\n"
    for i, doc in enumerate(docs, 1):
        cat_name = TEMPLATES.get(doc['category'], {}).get('name', doc['category'])
        text += f"{i}. {cat_name} - {doc['created_at'].strftime('%Y-%m-%d %H:%M')}\n"

    await update.message.reply_text(text, parse_mode="Markdown")


# =============================================================================
# HANDLERS - SÉLECTION DE CATÉGORIE ET TEMPLATE
# =============================================================================

async def handle_category_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Handler pour la sélection d'une catégorie.
    Affiche les templates disponibles pour la catégorie choisie.
    """
    query = update.callback_query
    await query.answer()

    # Extraire la catégorie du callback_data
    category = query.data.replace("cat_", "")

    # Sauvegarder la catégorie sélectionnée
    form_data: UserFormData = context.user_data.get('form_data', UserFormData())
    form_data.category = category
    context.user_data['form_data'] = form_data

    # Récupérer les infos de la catégorie
    cat_info = TEMPLATES.get(category, {})

    text = f"""
{cat_info.get('name', category.upper())}

{cat_info.get('description', '')}

📌 *Choisissez un template:*
"""

    await query.edit_message_text(
        text,
        reply_markup=build_templates_keyboard(category),
        parse_mode="Markdown"
    )

    return SELECT_TEMPLATE


async def handle_template_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Handler pour la sélection d'un template.
    Démarre le formulaire de collecte d'informations.
    """
    query = update.callback_query
    await query.answer()

    # Vérifier si c'est un retour au menu principal
    if query.data == "back_main":
        await query.edit_message_text(
            "📌 *Choisissez une catégorie:*",
            reply_markup=build_main_menu_keyboard(),
            parse_mode="Markdown"
        )
        return MAIN_MENU

    # Extraire l'ID du template
    template_id = query.data.replace("tpl_", "")

    # Sauvegarder le template sélectionné
    form_data: UserFormData = context.user_data.get('form_data', UserFormData())
    form_data.template_id = template_id
    context.user_data['form_data'] = form_data

    # Trouver le nom du template
    template_name = "Template"
    for cat_data in TEMPLATES.values():
        for tpl in cat_data.get("templates", []):
            if tpl["id"] == template_id:
                template_name = tpl["name"]
                break

    text = f"""
✅ *Template sélectionné:* {template_name}

Maintenant, je vais vous poser quelques questions pour compléter votre document.

📝 *Étape 1/7*
Entrez votre *prénom*:
"""

    await query.edit_message_text(text, parse_mode="Markdown")

    return FORM_FIRST_NAME


# =============================================================================
# HANDLERS - FORMULAIRE ÉTAPE PAR ÉTAPE
# Collecte des informations personnelles
# =============================================================================

async def form_first_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collecte le prénom."""
    form_data: UserFormData = context.user_data.get('form_data', UserFormData())
    form_data.first_name = update.message.text.strip()
    context.user_data['form_data'] = form_data

    await update.message.reply_text(
        "📝 *Étape 2/7*\nEntrez votre *nom de famille*:",
        parse_mode="Markdown"
    )

    return FORM_LAST_NAME


async def form_last_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collecte le nom de famille."""
    form_data: UserFormData = context.user_data.get('form_data', UserFormData())
    form_data.last_name = update.message.text.strip()
    context.user_data['form_data'] = form_data

    await update.message.reply_text(
        "📝 *Étape 3/7*\nEntrez votre *adresse* (numéro et rue):",
        parse_mode="Markdown"
    )

    return FORM_ADDRESS


async def form_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collecte l'adresse."""
    form_data: UserFormData = context.user_data.get('form_data', UserFormData())
    form_data.address = update.message.text.strip()
    context.user_data['form_data'] = form_data

    await update.message.reply_text(
        "📝 *Étape 4/7*\nEntrez votre *ville*:",
        parse_mode="Markdown"
    )

    return FORM_CITY


async def form_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collecte la ville."""
    form_data: UserFormData = context.user_data.get('form_data', UserFormData())
    form_data.city = update.message.text.strip()
    context.user_data['form_data'] = form_data

    await update.message.reply_text(
        "📝 *Étape 5/7*\nEntrez votre *code postal*:",
        parse_mode="Markdown"
    )

    return FORM_POSTAL_CODE


async def form_postal_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collecte le code postal."""
    form_data: UserFormData = context.user_data.get('form_data', UserFormData())
    form_data.postal_code = update.message.text.strip().upper()
    context.user_data['form_data'] = form_data

    await update.message.reply_text(
        "📝 *Étape 6/7*\nEntrez votre *numéro d'unité/appartement* _(optionnel)_:",
        reply_markup=build_skip_keyboard(),
        parse_mode="Markdown"
    )

    return FORM_UNIT


async def form_unit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collecte le numéro d'unité (optionnel)."""
    form_data: UserFormData = context.user_data.get('form_data', UserFormData())

    # Vérifier si c'est un callback "skip" ou un texte
    if update.callback_query:
        await update.callback_query.answer()
        form_data.unit = ""
        await update.callback_query.message.reply_text(
            "📝 *Étape 7/7*\nEntrez votre *numéro de téléphone* _(optionnel)_:",
            reply_markup=build_skip_keyboard(),
            parse_mode="Markdown"
        )
    else:
        form_data.unit = update.message.text.strip()
        await update.message.reply_text(
            "📝 *Étape 7/7*\nEntrez votre *numéro de téléphone* _(optionnel)_:",
            reply_markup=build_skip_keyboard(),
            parse_mode="Markdown"
        )

    context.user_data['form_data'] = form_data
    return FORM_PHONE


async def form_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collecte le numéro de téléphone (optionnel)."""
    form_data: UserFormData = context.user_data.get('form_data', UserFormData())

    if update.callback_query:
        await update.callback_query.answer()
        form_data.phone = ""
        message = update.callback_query.message
    else:
        form_data.phone = update.message.text.strip()
        message = update.message

    context.user_data['form_data'] = form_data

    # Router vers les questions spécifiques selon la catégorie
    if form_data.category == "payroll":
        await message.reply_text(
            "💼 *Informations de paie*\nEntrez le *nom de l'employeur*:",
            parse_mode="Markdown"
        )
        return PAYROLL_EMPLOYER

    elif form_data.category == "bank":
        await message.reply_text(
            "🏦 *Informations bancaires*\nEntrez les *4 derniers chiffres* de votre compte:",
            parse_mode="Markdown"
        )
        return BANK_ACCOUNT

    elif form_data.category == "bill":
        await message.reply_text(
            "📃 *Informations de facturation*\nEntrez le *nom de l'entreprise/fournisseur*:",
            parse_mode="Markdown"
        )
        return BILL_COMPANY

    # Par défaut, aller à la confirmation
    return await show_confirmation(message, context)


# =============================================================================
# HANDLERS - QUESTIONS SPÉCIFIQUES PAR CATÉGORIE
# =============================================================================

# --- PAYROLL ---

async def payroll_employer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collecte le nom de l'employeur."""
    form_data: UserFormData = context.user_data.get('form_data', UserFormData())
    form_data.extra_data['employer'] = update.message.text.strip()
    context.user_data['form_data'] = form_data

    await update.message.reply_text(
        "💰 Entrez le *salaire brut* pour la période (ex: 2500):",
        parse_mode="Markdown"
    )

    return PAYROLL_SALARY


async def payroll_salary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collecte le salaire."""
    try:
        salary = float(update.message.text.replace(",", ".").replace("$", "").replace(" ", ""))
    except ValueError:
        await update.message.reply_text("❌ Montant invalide. Entrez un nombre (ex: 2500):")
        return PAYROLL_SALARY

    form_data: UserFormData = context.user_data.get('form_data', UserFormData())
    form_data.extra_data['salary'] = salary
    context.user_data['form_data'] = form_data

    keyboard = [
        [InlineKeyboardButton("Hebdomadaire", callback_data="period_weekly"),
         InlineKeyboardButton("Aux 2 semaines", callback_data="period_biweekly")],
        [InlineKeyboardButton("Mensuel", callback_data="period_monthly")],
    ]

    await update.message.reply_text(
        "📅 Sélectionnez la *période de paie*:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

    return PAYROLL_PERIOD


async def payroll_period(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collecte la période de paie."""
    query = update.callback_query
    await query.answer()

    period_map = {
        "period_weekly": "Hebdomadaire",
        "period_biweekly": "Aux 2 semaines",
        "period_monthly": "Mensuel"
    }

    form_data: UserFormData = context.user_data.get('form_data', UserFormData())
    form_data.extra_data['period'] = period_map.get(query.data, "N/A")
    context.user_data['form_data'] = form_data

    await query.message.reply_text(
        "🗺️ Sélectionnez la *province*:",
        reply_markup=build_province_keyboard(),
        parse_mode="Markdown"
    )

    return PAYROLL_PROVINCE


async def payroll_province(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collecte la province et affiche la confirmation."""
    query = update.callback_query
    await query.answer()

    province = query.data.replace("prov_", "")

    form_data: UserFormData = context.user_data.get('form_data', UserFormData())
    form_data.extra_data['province'] = province
    context.user_data['form_data'] = form_data

    return await show_confirmation(query.message, context)


# --- BANK ---

async def bank_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collecte les infos bancaires."""
    form_data: UserFormData = context.user_data.get('form_data', UserFormData())
    form_data.extra_data['account'] = f"****-****-{update.message.text.strip()}"
    context.user_data['form_data'] = form_data

    return await show_confirmation(update.message, context)


# --- BILL ---

async def bill_company(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collecte le nom de l'entreprise."""
    form_data: UserFormData = context.user_data.get('form_data', UserFormData())
    form_data.extra_data['company'] = update.message.text.strip()
    context.user_data['form_data'] = form_data

    await update.message.reply_text(
        "💵 Entrez le *montant de la facture* (ex: 150.00):",
        parse_mode="Markdown"
    )

    return BILL_AMOUNT


async def bill_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collecte le montant de la facture."""
    try:
        amount = float(update.message.text.replace(",", ".").replace("$", "").replace(" ", ""))
    except ValueError:
        await update.message.reply_text("❌ Montant invalide. Entrez un nombre (ex: 150.00):")
        return BILL_AMOUNT

    form_data: UserFormData = context.user_data.get('form_data', UserFormData())
    form_data.extra_data['amount'] = amount
    context.user_data['form_data'] = form_data

    await update.message.reply_text(
        "📅 Entrez la *date d'échéance* (ex: 2025-01-15):",
        parse_mode="Markdown"
    )

    return BILL_DUE_DATE


async def bill_due_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collecte la date d'échéance."""
    form_data: UserFormData = context.user_data.get('form_data', UserFormData())
    form_data.extra_data['due_date'] = update.message.text.strip()
    context.user_data['form_data'] = form_data

    return await show_confirmation(update.message, context)


# =============================================================================
# HANDLERS - CONFIRMATION ET GÉNÉRATION
# =============================================================================

async def show_confirmation(message, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Affiche le résumé des informations pour confirmation."""
    form_data: UserFormData = context.user_data.get('form_data', UserFormData())

    summary = format_form_summary(form_data)
    summary += "\n\n*Voulez-vous générer le document?*"

    await message.reply_text(
        summary,
        reply_markup=build_confirm_keyboard(),
        parse_mode="Markdown"
    )

    return CONFIRM


async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gère la confirmation finale et génère le document."""
    query = update.callback_query
    await query.answer()

    if query.data == "confirm_cancel":
        await query.edit_message_text("❌ Opération annulée.")
        return ConversationHandler.END

    if query.data == "confirm_edit":
        await query.edit_message_text(
            "📌 *Recommencer*\nChoisissez une catégorie:",
            reply_markup=build_main_menu_keyboard(),
            parse_mode="Markdown"
        )
        context.user_data['form_data'] = UserFormData()
        return MAIN_MENU

    if query.data == "confirm_yes":
        await query.edit_message_text("⏳ Génération du document en cours...")

        form_data: UserFormData = context.user_data.get('form_data', UserFormData())

        # Générer le PDF
        try:
            pdf_bytes = generate_document_pdf(form_data)

            # Déterminer le nom du fichier
            template_name = form_data.template_id.replace("_", "-")
            filename = f"{template_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"

            # Envoyer le document
            await query.message.reply_document(
                document=io.BytesIO(pdf_bytes),
                filename=filename,
                caption=f"✅ Votre document a été généré avec succès!\n\n📄 *{filename}*",
                parse_mode="Markdown"
            )

            # Sauvegarder dans la base de données
            user = update.effective_user
            save_document(user.id, user.username or user.first_name, form_data, pdf_bytes)

            # Message de fin
            await query.message.reply_text(
                "🎉 *Document sauvegardé!*\n\n"
                "Utilisez /start pour créer un nouveau document\n"
                "Utilisez /history pour voir vos documents",
                parse_mode="Markdown"
            )

        except Exception as e:
            logger.error(f"Erreur de génération: {e}")
            await query.message.reply_text(
                "❌ Une erreur est survenue lors de la génération.\n"
                "Veuillez réessayer avec /start"
            )

        return ConversationHandler.END

    return CONFIRM


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handler pour annuler la conversation en cours."""
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Opération annulée.\n\nUtilisez /start pour recommencer.",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


# =============================================================================
# MAIN - CONFIGURATION ET DÉMARRAGE DU BOT
# =============================================================================

def main():
    """
    Point d'entrée principal du bot.
    Configure tous les handlers et démarre le polling.
    """
    # Vérifier le token
    if not BOT_TOKEN:
        raise RuntimeError("❌ BOT_TOKEN non défini!")

    # Initialiser la base de données
    init_database()

    # Créer l'application
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Handler de conversation principal
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            # Menu principal
            MAIN_MENU: [
                CallbackQueryHandler(handle_category_selection, pattern="^cat_"),
            ],

            # Sélection de template
            SELECT_TEMPLATE: [
                CallbackQueryHandler(handle_template_selection, pattern="^(tpl_|back_)"),
            ],

            # Formulaire de base
            FORM_FIRST_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, form_first_name),
            ],
            FORM_LAST_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, form_last_name),
            ],
            FORM_ADDRESS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, form_address),
            ],
            FORM_CITY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, form_city),
            ],
            FORM_POSTAL_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, form_postal_code),
            ],
            FORM_UNIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, form_unit),
                CallbackQueryHandler(form_unit, pattern="^skip$"),
            ],
            FORM_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, form_phone),
                CallbackQueryHandler(form_phone, pattern="^skip$"),
            ],

            # Questions spécifiques - Payroll
            PAYROLL_EMPLOYER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, payroll_employer),
            ],
            PAYROLL_SALARY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, payroll_salary),
            ],
            PAYROLL_PERIOD: [
                CallbackQueryHandler(payroll_period, pattern="^period_"),
            ],
            PAYROLL_PROVINCE: [
                CallbackQueryHandler(payroll_province, pattern="^prov_"),
            ],

            # Questions spécifiques - Bank
            BANK_ACCOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bank_account),
            ],

            # Questions spécifiques - Bill
            BILL_COMPANY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bill_company),
            ],
            BILL_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bill_amount),
            ],
            BILL_DUE_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bill_due_date),
            ],

            # Confirmation
            CONFIRM: [
                CallbackQueryHandler(handle_confirmation, pattern="^confirm_"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),  # Permet de redémarrer
        ],
    )

    # Ajouter les handlers
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("history", history_command))

    # Démarrer le bot
    logger.info("🚀 Bot démarré avec succès!")
    app.run_polling()


if __name__ == "__main__":
    main()
