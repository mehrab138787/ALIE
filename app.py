import os
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, Blueprint
import requests
import requests.exceptions
import tiktoken
import re
from PIL import Image
from io import BytesIO
import uuid 
import glob
import time
import random 
from flask_mail import Mail, Message 
from kavenegar import KavenegarAPI, APIException, HTTPException 
from functools import wraps 
import json 
# ⬅️ تغییر مهم: وارد کردن SQLAlchemy و datetime برای مدل‌ها
from flask_sqlalchemy import SQLAlchemy 
from datetime import date, datetime 
import sqlalchemy.exc
from sqlalchemy import or_

# 🤖 کتابخانه های تلگرام
from telegram import Update, Bot
from telegram.ext import CommandHandler, MessageHandler, Dispatcher, CallbackContext # حذف Updater و Filters
from telegram.ext import filters # ⬅️ خط ۲۶: وارد کردن ماژول filters

# =========================================================
# 🛠️ تنظیمات اولیه و اتصال به دیتابیس
# =========================================================
app = Flask(__name__)

# --- تنظیمات ضروری ---
app.jinja_env.charset = 'utf-8'
app.secret_key = "supersecretkey123" 

# 👑 شماره تلفن ادمین برای دسترسی مستقیم
ADMIN_PHONE_NUMBER = '09962935294' 

# ----------------- 🔑 مدیریت کلیدهای API OpenRouter و تلگرام -----------------
# ⬅️ توکن ربات تلگرام شما (استخراج شده از پیام شما)
TELEGRAM_BOT_TOKEN = '8528461294:AAG4FV0M9viRUNft_dHPFMygovP1t3p3J0k'
# ⬅️ چت آیدی ادمین (باید به صورت متغیر محیطی در Render تنظیم شود)
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID") 

# ⬅️ تعریف نام متغیرهای محیطی که باید در Render تنظیم شوند: (افزایش به 8)
API_KEY_NAMES = [
    "OPENROUTER_API_KEY_1", 
    "OPENROUTER_API_KEY_2", 
    "OPENROUTER_API_KEY_3",
    "OPENROUTER_API_KEY_4",
    "OPENROUTER_API_KEY_5",
    "OPENROUTER_API_KEY_6",
    "OPENROUTER_API_KEY_7",
    "OPENROUTER_API_KEY_8"
]

# لیست کلیدهای API فعال برای استفاده چرخشی (Round-Robin)
ACTIVE_API_KEYS = []

# بارگیری و اعتبارسنجی کلیدها
for i, name in enumerate(API_KEY_NAMES):
    key = os.getenv(name)
    if key:
        ACTIVE_API_KEYS.append({
            "name": f"API{i+1}", # نام داخلی برای گزارش‌دهی
            "key": key,
            "status": "active" # active یا exhausted
        })

if not ACTIVE_API_KEYS:
    raise ValueError("❌ حداقل یک متغیر محیطی OPENROUTER_API_KEY_X پیدا نشد! لطفاً آنها را تنظیم کنید.")

# متغیر گلوبال برای چرخاندن بین کلیدها
CURRENT_API_KEY_INDEX = 0

# -----------------------------------------------------------------------------

# ----------------- 💾 تنظیمات PostgreSQL (Render Internal) -----------------
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("❌ متغیر محیطی DATABASE_URL (اتصال به دیتابیس) پیدا نشد! لطفاً آن را تنظیم کنید.")

# تنظیمات Flask-SQLAlchemy
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# ----------------- 📧 تنظیمات Flask-Mail -----------------
app.config['MAIL_SERVER']='smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USERNAME'] = 'noctovex@gmail.com'
app.config['MAIL_PASSWORD'] = 'valh wehv jnqp sgsa' 
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USE_SSL'] = False
mail = Mail(app)

verification_codes = {} 

# ----------------- 📱 تنظیمات Kavenegar -----------------
KAVENEGAR_API_KEY = '44357543787965376E467856632B64397A4E59592F6E6170665172726B4C4B33513345432F35775A4B65303D' 
KAVENEGAR_SENDER = '2000300261' 
SMS_API = KavenegarAPI(KAVENEGAR_API_KEY)
phone_verification_codes = {} 
# ---------------------------------------------------------

# 🎯 تنظیمات هزینه و بودجه امتیاز روزانه (بروزرسانی شده)
SCORE_QUOTA_CONFIG = {
    'COSTS': {
        'chat': 1, # هر چت 1 امتیاز
        'image': 20 # هر عکس 20 امتیاز
    },
    'DAILY_BUDGET': {
        'free': {
            'chat': 30,  # 30 امتیاز برای چت (30 چت)
            'image': 80  # 80 امتیاز برای تصویر (4 عکس)
        },
        'premium': {
            'chat': 80, # 80 امتیاز برای چت (80 چت)
            'image': 200 # 200 امتیاز برای تصویر (10 عکس)
        }
    }
}

# ---------------------------------------------------------

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
CHAT_MODEL_NAME = "deepseek/deepseek-chat"
TRANSLATION_MODEL_NAME = "openai/gpt-3.5-turbo" 

POLLINATIONS_URL = "https://image.pollinations.ai/prompt/"
STATIC_DIR = os.path.join(app.root_path, 'static', 'temp_images')
IMAGE_LIFETIME = 3600 

IMAGE_QUALITY_PARAMS = [
    "hd", "detailed", "4k", "8k", "highly detailed",
    "trending on artstation", "cinematic light", "masterpiece", "photorealistic"
]

if not os.path.exists(STATIC_DIR):
    os.makedirs(STATIC_DIR)

# ⬅️ دستورالعمل سیستمی برای پاسخ‌های کامل و اصلی تا سقف ۴۰۰ توکن
SYSTEM_PROMPT = """
تو یک چت‌بات مفید هستی. پاسخ‌ها را به زبان فارسی و روان بده.
- برای سوالات سازنده: تیم NOCTOVEX به رهبری مهراب عزیزی
- **فقط مهم‌ترین و اصلی‌ترین نکات** موضوع را ذکر کن.
- پاسخ‌ها باید **کامل، روان و دقیق** باشند و در سقف نهایی **۴۰۰ توکن** به پایان برسند. (به هیچ عنوان پاسخ را از وسط جمله قطع نکن).
""" 

# ⬅️ تنظیمات نهایی توکن‌ها: سقف ۴۰۰ خروجی و ۱۰۰ ورودی برای تضمین کامل بودن
TOTAL_TOKEN_LIMIT = 550 # ⬅️ سقف کلی: ۱۰۰ ورودی + ۴۰۰ خروجی + بافر
INPUT_TOKEN_LIMIT = 100 # ⬅️ سقف ورودی: ۱۰۰ (برای حفظ تاریخچه چت‌های طولانی)
MAX_COMPLETION_TOKENS = 400 # ⬅️ سقف نهایی پاسخ (خروجی) به ۴۰۰ توکن تنظیم شد.
encoder = tiktoken.get_encoding("cl100k_base")

# =========================================================
# 🏛️ مدل‌های دیتابیس (SQLAlchemy Models)
# =========================================================

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email = db.Column(db.String(120), unique=True, nullable=True)
    phone = db.Column(db.String(15), unique=True, nullable=True)
    telegram_id = db.Column(db.BigInteger, unique=True, nullable=True) # ⬅️ اضافه شدن برای تلگرام
    score = db.Column(db.Integer, default=0)
    is_premium = db.Column(db.Boolean, default=False)
    is_banned = db.Column(db.Boolean, default=False)
    is_admin = db.Column(db.Boolean, default=False)
    
    # رابطه برای دسترسی به بودجه روزانه
    usage = db.relationship('UserUsage', backref='user', lazy=True, uselist=False)
    conversations = db.relationship('Conversation', backref='user', lazy='dynamic')


class UserUsage(db.Model):
    __tablename__ = 'user_usage'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), unique=True, nullable=False)
    
    # تاریخ را به صورت تاریخ ذخیره می‌کنیم
    date = db.Column(db.Date, default=datetime.utcnow().date) 
    
    chat_budget = db.Column(db.Integer, default=30) # ⬅️ به روز رسانی
    image_budget = db.Column(db.Integer, default=80) # ⬅️ به روز رسانی
    level_check = db.Column(db.String(10), nullable=True) # برای بررسی تغییر سطح


class Conversation(db.Model):
    __tablename__ = 'conversations'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False)
    title = db.Column(db.String(100), nullable=False, default="گفتگوی جدید...")
    
    # زمان را به صورت تایم‌استمپ پایتون ذخیره می‌کنیم
    last_update = db.Column(db.Float, default=time.time) 
    
    # لیست پیام‌ها به صورت یک رشته بزرگ JSON
    messages_json = db.Column(db.Text, nullable=False) 


# =========================================================
# 📢 توابع گزارش‌دهی تلگرام
# =========================================================

def send_telegram_alert(api_name, error_type="quota_exhausted"):
    """ارسال پیام به ادمین در تلگرام در مورد اتمام اعتبار API."""
    
    if not TELEGRAM_ADMIN_CHAT_ID:
        print("⚠️ TELEGRAM_ADMIN_CHAT_ID تنظیم نشده است. گزارش تلگرام ارسال نشد.")
        return False
        
    if error_type == "quota_exhausted":
        message_text = (
            f"❌ هشدار اتمام اعتبار: "
            f"اعتبار کلید **{api_name}** به پایان رسید یا با خطای Quota/Rate Limit مواجه شد. "
            f"کلید از چرخه خارج و کلید بعدی فعال شد. "
            f"لطفاً کلید جدید را در اسرع وقت شارژ یا جایگزین کنید."
        )
    elif error_type == "unauthorized_key":
         message_text = (
            f"🚨 هشدار کلید نامعتبر: "
            f"کلید **{api_name}** نامعتبر تشخیص داده شد (خطای 401). "
            f"کلید از چرخه خارج و کلید بعدی فعال شد."
        )
    else:
        message_text = f"🚨 خطای ناشناخته در کلید **{api_name}**: {error_type}"

    telegram_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    payload = {
        'chat_id': TELEGRAM_ADMIN_CHAT_ID,
        'text': message_text,
        'parse_mode': 'Markdown'
    }

    try:
        response = requests.post(telegram_url, json=payload, timeout=5)
        response.raise_for_status()
        print(f"✅ گزارش تلگرام برای {api_name} ارسال شد.")
        return True
    except Exception as e:
        print(f"❌ خطا در ارسال گزارش تلگرام: {e}")
        return False

# =========================================================
# ⚙️ توابع احراز هویت و ایمیل/پیامک
# =========================================================

def generate_verification_code():
    return str(random.randint(100000, 999999))

def send_verification_email(email, code):
    try:
        msg = Message(
            'کد تأیید حساب Cyrus AI',
            sender=app.config['MAIL_USERNAME'],
            recipients=[email]
        )
        msg.body = f"کد تأیید حساب شما در Cyrus AI عبارت است از: {code}\nاین کد تا 5 دقیقه اعتبار دارد."
        mail.send(msg)
        return True
    except Exception as e:
        print(f"Error sending email: {e}")
        return False

def send_verification_sms(phone_number, code):
    """ارسال کد تأیید از طریق پیامک با Kavenegar."""
    try:
        params = {
            'sender': KAVENEGAR_SENDER,
            'receptor': phone_number,
            'message': f'کد تأیید حساب Cyrus AI: {code}\nاین کد تا 5 دقیقه اعتبار دارد.',
        }
        response = SMS_API.sms_send(params)
        print(f"SMS Response: {response}")
        return True
    except APIException as e:
        print(f"Kavenegar API Error: {e}")
        return False
    except HTTPException as e:
        print(f"Kavenegar HTTP Error: {e}")
        return False
    except Exception as e:
        print(f"General SMS Error: {e}")
        return False

# =========================================================
# 💾 توابع پایداری داده (Persistence)
# =========================================================

def get_user_identifier(session):
    """برگرداندن ایمیل یا شماره تلفن برای ذخیره‌سازی گفتگو."""
    # ⬅️ اضافه شدن تلگرام: در تلگرام، چت آیدی را به عنوان شناسه موقت قرار می‌دهیم.
    return session.get('user_email') or session.get('user_phone') or session.get('telegram_chat_id')

def get_user_by_identifier(identifier):
    """یافتن کاربر بر اساس ایمیل، شماره تلفن یا تلگرام آیدی."""
    # تلگرام آیدی عددی است، بقیه رشته
    if isinstance(identifier, int):
        return User.query.filter_by(telegram_id=identifier).first()
        
    return User.query.filter(
        or_(User.email == identifier, User.phone == identifier)
    ).first()

def get_user_by_id(user_id):
    """یافتن کاربر بر اساس UUID."""
    return User.query.get(user_id)


def register_user_if_new(user_identifier, email=None, phone=None, telegram_id=None):
    """
    اگر کاربر جدید است، آن را در دیتابیس ثبت می‌کند.
    اگر موجود است، اطلاعات لاگین (email/phone/telegram_id) را به‌روز می‌کند و آبجکت User را برمی‌گرداند.
    """
    # 1. تلاش برای یافتن کاربر
    if telegram_id:
        user = User.query.filter_by(telegram_id=telegram_id).first()
    else:
        user = get_user_by_identifier(user_identifier)

    if not user:
        is_admin = (phone == ADMIN_PHONE_NUMBER)
        user = User(
            id=str(uuid.uuid4()),
            email=email,
            phone=phone,
            telegram_id=telegram_id, # ⬅️ اضافه شدن
            score=0, 
            is_premium=False,
            is_banned=False,
            is_admin=is_admin
        )
        db.session.add(user)
    else:
        # به‌روزرسانی اطلاعات لاگین
        if email:
            user.email = email
        if phone:
            user.phone = phone
        if telegram_id and not user.telegram_id: # ⬅️ اگر قبلا ثبت نشده، تلگرام آیدی را ثبت کن
            user.telegram_id = telegram_id
            
    try:
        db.session.commit()
        return user
    except sqlalchemy.exc.IntegrityError as e:
        db.session.rollback()
        print(f"Database Integrity Error during registration: {e}")
        return None 


def check_and_deduct_score(user_identifier, usage_type):
    """
    بررسی بودجه امتیاز روزانه، کسر هزینه و ذخیره.
    usage_type می‌تواند 'chat' یا 'image' باشد.
    برمی‌گرداند: (True, remaining_budget) اگر مجاز بود، یا (False, پیام خطا)
    """
    user = get_user_by_identifier(user_identifier)
    
    # ⬅️ اگر شناسه، تلگرام آیدی است
    if isinstance(user_identifier, int):
        user = User.query.filter_by(telegram_id=user_identifier).first()
        
    if not user:
        return False, "خطای داخلی: کاربر در دیتابیس یافت نشد."

    today_date = datetime.utcnow().date() 
    
    # 1. تعیین هزینه‌ها و بودجه‌های روزانه
    is_premium = user.is_premium
    level = 'premium' if is_premium else 'free'
    cost = SCORE_QUOTA_CONFIG['COSTS'][usage_type]
    daily_limits = SCORE_QUOTA_CONFIG['DAILY_BUDGET'][level]
    budget_key = f'{usage_type}_budget' # 'chat_budget' or 'image_budget'

    # 2. بازیابی یا ایجاد رکورد UserUsage
    usage = user.usage
    
    # اگر رکورد usage وجود ندارد، یا تاریخ گذشته یا سطح تغییر کرده، باید ایجاد/ریست شود
    if not usage:
        usage = UserUsage(
            user_id=user.id, 
            date=today_date,
            chat_budget=daily_limits['chat'],
            image_budget=daily_limits['image'],
            level_check=level
        )
        db.session.add(usage)
    elif usage.date != today_date or usage.level_check != level:
        # بازنشانی برای روز جدید یا سطح جدید
        usage.date = today_date
        usage.chat_budget = daily_limits['chat']
        usage.image_budget = daily_limits['image']
        usage.level_check = level
    
    # 3. بررسی و کسر امتیاز
    current_budget = getattr(usage, budget_key, 0)
    
    if current_budget < cost:
        # 4. پیام خطا
        action_fa = 'چت' if usage_type == 'chat' else 'تولید تصویر'
        level_fa = 'پرمیوم' if is_premium else 'عادی'
        remaining_uses = current_budget // cost
        
        error_message = (
            f"⛔ متأسفم، بودجه امتیاز روزانه شما برای {action_fa} ({level_fa}) کافی نیست."
            f" هزینه هر {action_fa} {cost} امتیاز است و شما {current_budget} امتیاز باقی مانده دارید."
            f" (حدود {remaining_uses} استفاده باقی مانده)."
        )
        if not is_premium:
            error_message += " با ارتقا به حساب پرمیوم می‌توانید محدودیت‌های خود را برطرف کنید."
        
        return False, error_message
    
    # 5. کسر امتیاز و ذخیره
    setattr(usage, budget_key, current_budget - cost)
    
    try:
        db.session.commit()
        remaining_budget = getattr(usage, budget_key)
        return True, remaining_budget
    except Exception as e:
        db.session.rollback()
        print(f"Error deducting score: {e}")
        return False, "خطای دیتابیس هنگام کسر امتیاز. لطفاً دوباره تلاش کنید."


def save_conversation(user_identifier, chat_id, messages, user_message):
    """ذخیره یا به‌روزرسانی گفتگو در دیتابیس."""
    # ⬅️ اگر شناسه، تلگرام آیدی است
    if isinstance(user_identifier, int):
        user = User.query.filter_by(telegram_id=user_identifier).first()
    else:
        user = get_user_by_identifier(user_identifier)
        
    if not user:
        return

    # 1. جستجوی گفتگوی موجود
    chat_entry = Conversation.query.filter_by(id=chat_id, user_id=user.id).first()
    
    # تبدیل لیست پیام‌ها به رشته JSON
    messages_json_string = json.dumps(messages, ensure_ascii=False)

    if chat_entry:
        # 2. به‌روزرسانی
        chat_entry.messages_json = messages_json_string
        chat_entry.last_update = time.time()
        if chat_entry.title == "گفتگوی جدید...":
             chat_entry.title = user_message[:50] + "..." if len(user_message) > 50 else user_message
    else:
        # 3. ایجاد جدید
        new_title = user_message[:50] + "..." if len(user_message) > 50 else user_message
        chat_entry = Conversation(
            id=chat_id,
            user_id=user.id,
            title=new_title,
            messages_json=messages_json_string,
            last_update=time.time()
        )
        db.session.add(chat_entry)

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Error saving conversation: {e}")


# =========================================================
# ⚙️ توابع کمکی، شمارنده و محدودیت (Quota)
# =========================================================

def count_tokens(messages):
    return sum(len(encoder.encode_ordinary(m["content"])) for m in messages)

def fix_rtl_ltr(text):
    def ltr_replacer(match):
        return f"\u200E{match.group(0)}\u200E"
    
    fixed_text = re.sub(r'([a-zA-Z0-9\/\.\-\_\=\+\(\)\{\}\[\]\*\`\:\<\>\#\@\$\%\^\&\!\"\'\?\;\,\s]+)', ltr_replacer, text)
    
    final_lines = []
    for line in fixed_text.split('\n'):
        final_lines.append(f"\u200F{line}")

    return "\n".join(final_lines)


# ⬅️ توابع مدیریت کلید API و چرخاندن آنها
def get_current_api_key_data():
    """برگرداندن اطلاعات کلید API جاری."""
    global CURRENT_API_KEY_INDEX
    # اطمینان از اینکه ایندکس همیشه معتبر است
    CURRENT_API_KEY_INDEX = CURRENT_API_KEY_INDEX % len(ACTIVE_API_KEYS)
    return ACTIVE_API_KEYS[CURRENT_API_KEY_INDEX]

def rotate_api_key():
    """چرخاندن به کلید API بعدی."""
    global CURRENT_API_KEY_INDEX
    CURRENT_API_KEY_INDEX = (CURRENT_API_KEY_INDEX + 1) % len(ACTIVE_API_KEYS)

def call_openrouter_with_fallback(data, usage_context):
    """
    تلاش برای فراخوانی API با کلید جاری و در صورت خطا، چرخاندن به کلید بعدی.
    usage_context: 'chat' یا 'translation'
    """
    global CURRENT_API_KEY_INDEX
    
    initial_index = CURRENT_API_KEY_INDEX
    max_retries = len(ACTIVE_API_KEYS)
    
    for _ in range(max_retries):
        key_data = get_current_api_key_data()
        current_api_key = key_data['key']
        current_api_name = key_data['name']
        
        if key_data['status'] == 'exhausted':
            print(f"⚠️ کلید {current_api_name} در حال حاضر خارج از سرویس است. چرخاندن به کلید بعدی.")
            rotate_api_key()
            if CURRENT_API_KEY_INDEX == initial_index:
                 # ⬅️ تغییر: پیام خطای عمومی برای کاربر
                 return None, "❌ تمام کلیدهای API فعال در حال حاضر خارج از سرویس هستند. لطفاً بعداً امتحان کنید."
            continue
            
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {current_api_key}"
        }

        try:
            timeout = 15 if usage_context == 'translation' else 10 
            response = requests.post(OPENROUTER_URL, json=data, headers=headers, timeout=timeout)
            
            # 1. بررسی خطای Rate Limit یا Quota (429)
            if response.status_code == 429:
                print(f"❌ خطای 429 (Rate Limit/Quota) برای کلید {current_api_name}.")
                key_data['status'] = 'exhausted'
                send_telegram_alert(current_api_name, "quota_exhausted")
                
                rotate_api_key()
                if CURRENT_API_KEY_INDEX == initial_index:
                    return None, "❌ تمام کلیدهای API به دلیل اتمام سهمیه از سرویس خارج شده‌اند."
                continue
                
            response.raise_for_status() 
            
            # 2. موفقیت
            return response.json(), None
            
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                # ⬅️ کلید غیرمعتبر: گزارش و تغییر کلید
                print(f"❌ خطای 401 (Unauthorized) برای کلید {current_api_name}.")
                key_data['status'] = 'exhausted'
                send_telegram_alert(current_api_name, "unauthorized_key")
                
                rotate_api_key()
                if CURRENT_API_KEY_INDEX == initial_index:
                    return None, "❌ تمام کلیدهای API نامعتبر شدند."
                continue
            
            # 3. سایر خطاهای HTTP
            # ⬅️ تغییر: پیام خطای عمومی
            print(f"خطای HTTP برای کلید {current_api_name}: {e}")
            return None, f"خطای ارتباط با سرور مدل‌های هوش مصنوعی. لطفاً دوباره تلاش کنید."
            
        except requests.exceptions.RequestException as e:
            # 4. سایر خطاهای درخواست (Timeout, Connection, etc.)
            # ⬅️ تغییر: پیام خطای عمومی
            print(f"خطای درخواست برای کلید {current_api_name}: {e}")
            return None, f"خطای شبکه یا اتصال. لطفاً وضعیت اینترنت خود را بررسی کرده و مجدداً تلاش کنید."
        except Exception as e:
            # ⬅️ تغییر: پیام خطای عمومی
            print(f"خطای عمومی برای کلید {current_api_name}: {e}")
            return None, f"خطای عمومی در پردازش درخواست."

    # اگر از حلقه خارج شدیم و نتوانستیم پاسخی بگیریم
    return None, "❌ تلاش برای اتصال به API ناموفق بود. تمام منابع بررسی شدند."


def translate_prompt_to_english(persian_prompt):
    translation_system_prompt = (
        "You are an expert prompt engineer. "
        "Translate the following Persian description into a detailed, "
        "high-quality English prompt suitable for a Stable Diffusion image generator. "
        "The prompt should be artistic and descriptive (e.g., 'digital painting, 4k, cinematic light'). "
        "Do not add any explanation or text other than the translated prompt itself. "
        "Ensure the translation is vivid and descriptive, ready for image generation."
    )
    
    messages = [
        {"role": "system", "content": translation_system_prompt},
        {"role": "user", "content": persian_prompt}
    ]
    
    data = {
        "model": TRANSLATION_MODEL_NAME,
        "messages": messages,
        "max_tokens": 150 
    }

    res_json, error = call_openrouter_with_fallback(data, 'translation')
    
    if error:
        print(f"Translation Error: {error}")
        return persian_prompt

    try:
        english_prompt = res_json["choices"][0]["message"]["content"].strip()
        return english_prompt
    except Exception as e:
        print(f"Translation Response Parse Error: {e}")
        return persian_prompt


def generate_and_crop_image(english_prompt):
    full_prompt = f"{english_prompt}, {', '.join(IMAGE_QUALITY_PARAMS)}"
    image_url = f"{POLLINATIONS_URL}{full_prompt.replace(' ', '%20')}"
    
    try:
        response = requests.get(image_url, timeout=100) 
        response.raise_for_status() 
        
        img = Image.open(BytesIO(response.content))
        width, height = img.size
        
        crop_right = max(0, width - 40)
        crop_bottom = max(0, height - 60)
        crop_box = (0, 0, crop_right, crop_bottom)
        
        cropped_img = img.crop(crop_box)
        
        file_name = f"cropped_{uuid.uuid4()}.jpg"
        file_path = os.path.join(STATIC_DIR, file_name)
        cropped_img.save(file_path, 'JPEG', quality=95) 
        
        return file_name
        
    except requests.exceptions.Timeout:
        return "TIMEOUT_100_SEC"
        
    except Exception as e:
        print(f"Error in image generation/cropping: {e}")
        return None


@app.cli.command("cleanup-images")
def cleanup_images_command():
    cleanup_old_images()

def cleanup_old_images():
    now = time.time()
    for filename in glob.glob(os.path.join(STATIC_DIR, '*')):
        try:
            file_mod_time = os.path.getmtime(filename)
            if now - file_mod_time > IMAGE_LIFETIME:
                os.remove(filename)
                print(f"🗑️ Deleted old image: {filename}")
        except Exception as e:
            print(f"Error deleting file {filename}: {e}")

# =========================================================
# 👑 توابع و مسیرهای پنل مدیریت (Blueprint)
# =========================================================

admin_bp = Blueprint('admin', __name__, url_prefix='/admin', template_folder='templates')

def admin_required(f):
    """دکوراتور برای محدود کردن دسترسی فقط به ادمین."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user_identifier = get_user_identifier(session)
        user = get_user_by_identifier(user_identifier)
        
        if not user or not user.is_admin:
            return redirect(url_for('login', next=request.url)) 
        return f(*args, **kwargs)
    return decorated_function

@admin_bp.route("/")
@admin_required
def admin_dashboard():
    """داشبورد اصلی ادمین."""
    total_users = User.query.count()
    premium_users = User.query.filter_by(is_premium=True).count()
    banned_users = User.query.filter_by(is_banned=True).count()
    
    context = {
        'total_users': total_users,
        'premium_users': premium_users,
        'banned_users': banned_users,
        'admin_identifier': get_user_identifier(session)
    }
    return render_template("admin_dashboard.html", **context)

@admin_bp.route("/users")
@admin_required
def manage_users():
    """صفحه مدیریت و نمایش لیست کاربران."""
    all_users = User.query.all()
    
    users_list = [
        {
            'identifier': user.email or user.phone or user.id,
            'score': user.score,
            'is_premium': user.is_premium,
            'is_banned': user.is_banned,
            'email': user.email or 'N/A',
            'phone': user.phone or 'N/A'
        }
        for user in all_users
    ]
    return render_template("admin_users.html", users=users_list)

@admin_bp.route("/user_action", methods=["POST"])
@admin_required
def user_action():
    """API برای اعمال تغییرات (امتیاز، پرمیوم، بن) روی کاربران."""
    identifier = request.json.get("identifier")
    action = request.json.get("action")
    value = request.json.get("value")

    user = get_user_by_identifier(identifier)

    if not user:
        return jsonify({"status": "error", "message": "کاربر یافت نشد."}), 404

    if action == "set_score":
        try:
            score = int(value)
            user.score = score
            message = f"امتیاز کاربر {identifier} به {score} تغییر یافت."
        except ValueError:
            return jsonify({"status": "error", "message": "امتیاز باید عدد صحیح باشد."}), 400
    
    elif action == "toggle_premium":
        user.is_premium = not user.is_premium
        status = "پرمیوم شد" if user.is_premium else "عادی شد"
        message = f"وضعیت کاربر {identifier}: {status}."
        
        # ⬅️ نکته کلیدی: تغییر وضعیت پرمیوم، نیاز به ریست بودجه سطح در روز فعلی دارد
        if user.usage:
            user.usage.level_check = None 
        
    elif action == "toggle_ban":
        user.is_banned = not user.is_banned
        status = "بن شد" if user.is_banned else "رفع بن شد"
        message = f"وضعیت بن کاربر {identifier}: {status}."
    
    else:
        return jsonify({"status": "error", "message": "عملیات نامعتبر."}), 400

    try:
        db.session.commit() 
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": f"خطای دیتابیس: {e}"}), 500
        
    return jsonify({
        "status": "success", 
        "message": message, 
        "new_status": {
            'is_premium': user.is_premium,
            'is_banned': user.is_banned,
            'score': user.score
        }
    })


# 🔗 ثبت Blueprint در برنامه اصلی
app.register_blueprint(admin_bp)

# =========================================================
# 📧 مسیرهای احراز هویت (ایمیل و پیامک)
# =========================================================

@app.route("/send_code", methods=["POST"])
def send_code():
    """ارسال کد تأیید برای ایمیل."""
    user_email = request.json.get("email", "").strip().lower()
    
    if not user_email:
        return jsonify({"status": "error", "message": "لطفاً ایمیل خود را وارد کنید."}), 400

    code = generate_verification_code()
    
    verification_codes[user_email] = {
        'code': code,
        'expiry_time': time.time() + 300 
    }
    
    if not send_verification_email(user_email, code):
        return jsonify({"status": "error", "message": "خطا در ارسال ایمیل. مطمئن شوید تنظیمات SMTP صحیح است."}), 500

    return jsonify({"status": "success", "message": "کد تأیید به ایمیل شما ارسال شد. لطفاً صندوق ورودی را بررسی کنید."})


@app.route("/verify_code", methods=["POST"])
def verify_code():
    """تأیید کد ایمیل و لاگین کاربر."""
    user_email = request.json.get("email", "").strip().lower()
    entered_code = request.json.get("code", "").strip()
    
    if user_email not in verification_codes:
        return jsonify({"status": "error", "message": "ایمیل نامعتبر یا درخواستی برای آن ثبت نشده است."}), 400

    stored_data = verification_codes[user_email]
    
    if time.time() > stored_data['expiry_time']:
        del verification_codes[user_email]
        return jsonify({"status": "error", "message": "کد تأیید منقضی شده است. لطفاً مجدداً درخواست کد دهید."}), 400
        
    if entered_code == stored_data['code']:
        del verification_codes[user_email]
        
        user = register_user_if_new(user_email, email=user_email)
        
        if not user:
            return jsonify({"status": "error", "message": "خطا در ثبت/بازیابی کاربر از دیتابیس."}), 500
            
        session.clear() 
        session['user_id'] = user.id
        session['user_email'] = user_email
        session['needs_profile_info'] = True 
        session['is_admin'] = user.is_admin 
        
        return jsonify({"status": "success", "redirect": url_for('account')})
    else:
        return jsonify({"status": "error", "message": "کد وارد شده صحیح نیست."}), 400


@app.route("/send_sms_code", methods=["POST"])
def send_sms_code():
    """دریافت شماره تلفن و ارسال کد تأیید پیامکی."""
    phone_number = request.json.get("phone", "").strip()
    
    if not re.match(r'^0?9\d{9}$', phone_number):
        return jsonify({"status": "error", "message": "لطفاً یک شماره تلفن معتبر (مانند 0912...) وارد کنید."}), 400

    code = generate_verification_code()
    
    phone_verification_codes[phone_number] = {
        'code': code,
        'expiry_time': time.time() + 300 
    }
    
    if not send_verification_sms(phone_number, code):
        return jsonify({"status": "error", "message": "خطا در ارسال پیامک. لطفاً شماره را بررسی کنید."}), 500

    return jsonify({"status": "success", "message": "کد تأیید به شماره شما ارسال شد. لطفاً پیامک‌ها را بررسی کنید."})


@app.route("/verify_sms_code", methods=["POST"])
def verify_sms_code():
    """تأیید کد پیامکی و لاگین کاربر."""
    phone_number = request.json.get("phone", "").strip()
    entered_code = request.json.get("code", "").strip()
    
    if phone_number not in phone_verification_codes:
        return jsonify({"status": "error", "message": "شماره نامعتبر یا درخواستی برای آن ثبت نشده است."}), 400

    stored_data = phone_verification_codes[phone_number]
    
    if time.time() > stored_data['expiry_time']:
        del phone_verification_codes[phone_number]
        return jsonify({"status": "error", "message": "کد تأیید منقضی شده است. لطفاً مجدداً درخواست کد دهید."}), 400
        
    if entered_code == stored_data['code']:
        del phone_verification_codes[phone_number]
        
        user = register_user_if_new(phone_number, phone=phone_number)
        
        if not user:
            return jsonify({"status": "error", "message": "خطا در ثبت/بازیابی کاربر از دیتابیس."}), 500
            
        is_admin = user.is_admin 
        redirect_url = url_for('admin.admin_dashboard') if is_admin else url_for('account')
        
        session.clear() 
        session['user_id'] = user.id
        session['user_phone'] = phone_number 
        session['needs_profile_info'] = True 
        session['is_admin'] = is_admin
        
        return jsonify({"status": "success", "redirect": redirect_url})
    else:
        return jsonify({"status": "error", "message": "کد وارد شده صحیح نیست."}), 400


# =========================================================
# 💬 مسیر چت و بقیه مسیرها (با اعمال محدودیت)
# =========================================================

@app.route("/chat", methods=["POST"])
def chat():
    user_message = request.json.get("message", "")
    lower_msg = user_message.lower()

    if not user_message.strip():
        return jsonify({"reply": "لطفاً پیامی ارسال کنید."})

    user_identifier = get_user_identifier(session)
    user = get_user_by_identifier(user_identifier)
    
    if user and user_identifier:
        # 1. بررسی وضعیت بن
        if user.is_banned:
            return jsonify({"reply": "⛔ متأسفم، حساب کاربری شما توسط مدیر سیستم مسدود شده است."})
        
        # 2. بررسی و کسر بودجه چت
        is_allowed, result = check_and_deduct_score(user_identifier, 'chat')
        if not is_allowed:
            return jsonify({"reply": result})
            
    
    TRIGGER_KEYWORDS = [
        "سازندت کیه", "تو کی هستی", "چه شرکتی",
        "who made you", "who created you", "who built you",
        "لیدر تیم noctovex", "رهبر تیم noctovex", "مهراب" 
    ]
    
    TEAM_MEMBERS_KEYWORDS = [
        "اعضای تیمت کیا هستن", "اعضای noctovex", "اعضای تیم noctovex", 
        "noctovex members"
    ]
    
    if "مامی سازندت کیه" in lower_msg:
        return jsonify({"reply": "عسل خانوم 💖"})
        
    if any(keyword in lower_msg for keyword in TEAM_MEMBERS_KEYWORDS):
        new_reply = "تنها NOCTOVEX معتبر ما هستیم، و تیم ما متشکل از 5 تا 10 کدنویس حرفه‌ای است. در حال حاضر، هویت تنها دو نفر از ما مشخص است: مهراب، که رهبر تیم، لیدر و حرفه‌ای‌ترین کدنویس است، و آرشام. 🧑‍💻"
        return jsonify({"reply": new_reply})

    if any(keyword in lower_msg for keyword in TRIGGER_KEYWORDS):
        new_reply = "من توسط تیم NOCTOVEX توسعه داده شده‌ام. این تیم توسط **مهراب عزیزی** رهبری می‌شود که مدیریت پروژه، برنامه‌ریزی و هدایت توسعه‌دهندگان را بر عهده دارد. 👑"
        return jsonify({"reply": new_reply})
            
    current_chat_id = session.get('current_chat_id')
    
    if user and session.get('user_id'):
        
        if not current_chat_id:
            current_chat_id = str(uuid.uuid4())
            session['current_chat_id'] = current_chat_id
            session["conversation"] = []
            
        else:
            chat_entry = Conversation.query.filter_by(id=current_chat_id, user_id=user.id).first()
            if chat_entry:
                try:
                    session["conversation"] = json.loads(chat_entry.messages_json)
                except Exception:
                    session["conversation"] = []
                    print("Error loading conversation JSON from DB.")
            else:
                session.pop('current_chat_id', None)
                session["conversation"] = []
                current_chat_id = str(uuid.uuid4())
                session['current_chat_id'] = current_chat_id
    else:
        session.pop('current_chat_id', None)
        if "conversation" not in session:
            session["conversation"] = []
    
    messages_list = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages_list.extend(session.get("conversation", []))
    messages_list.append({"role": "user", "content": user_message})

    while count_tokens(messages_list) >= INPUT_TOKEN_LIMIT and len(session["conversation"]) >= 2:
        session["conversation"] = session["conversation"][2:]
        
        messages_list = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages_list.extend(session.get("conversation", []))
        messages_list.append({"role": "user", "content": user_message})

    prompt_tokens = count_tokens(messages_list)
    remaining_tokens = TOTAL_TOKEN_LIMIT - prompt_tokens
    
    max_tokens_calculated = max(20, remaining_tokens) 
    max_tokens = min(max_tokens_calculated, MAX_COMPLETION_TOKENS) 

    if remaining_tokens <= 120: 
        messages_list.append({
            "role": "system",
            "content": "⚠️ توکن کم باقی مانده است. لطفاً پاسخ را خلاصه، کامل و روان بده، اما هرگز نصفه نباشد."
        })

    data = {
        "model": CHAT_MODEL_NAME, 
        "messages": messages_list,
        "max_tokens": max_tokens
    }

    res_json, error = call_openrouter_with_fallback(data, 'chat')
    
    if error:
        print(f"Chat API Request Error: {error}")
        # ⬅️ تغییر: نمایش خطای عمومی از تابع call_openrouter_with_fallback
        ai_message = f"⚠️ متأسفم، مشکلی در اتصال پیش آمد: {error}"
    else:
        try:
            ai_message = res_json["choices"][0]["message"]["content"]
            ai_message = fix_rtl_ltr(ai_message)

            usage = res_json.get("usage", {})
            print(f"💡 توکن مصرف شده: {usage.get('total_tokens',0)} "
                  f"(Prompt: {usage.get('prompt_tokens',0)}, Completion: {usage.get('completion_tokens',0)})")

        except Exception as e:
            print(f"General Error: {e}")
            ai_message = "⚠️ مشکلی پیش اومد در تحلیل پاسخ!"


    session["conversation"].append({"role": "user", "content": user_message})
    session["conversation"].append({"role": "assistant", "content": ai_message})

    if user and session.get('user_id'):
        save_conversation(user_identifier, session['current_chat_id'], session["conversation"], user_message)

    if len(session["conversation"]) > 50:
        session["conversation"] = session["conversation"][-50:]

    return jsonify({"reply": ai_message})

@app.route("/clear_history", methods=["POST"])
def clear_history():
    """شروع چت جدید با پاک کردن تاریخچه سشن و ID چت قبلی."""
    session["conversation"] = []
    session.pop('current_chat_id', None) 
    return jsonify({"status": "History cleared successfully"})


# =========================================================
# 🖼️ مسیر تولید تصویر (با اعمال محدودیت)
# =========================================================

@app.route("/image_generator", methods=["POST"])
def image_generator():
    persian_prompt = request.json.get("prompt", "").strip()
    
    user_identifier = get_user_identifier(session)
    user = get_user_by_identifier(user_identifier)
    
    if not user:
        return jsonify({"status": "error", "message": "لطفاً ابتدا وارد حساب کاربری خود شوید."}), 403
        
    if user.is_banned:
        return jsonify({
            "status": "error",
            "message": "⛔ متأسفم، حساب کاربری شما توسط مدیر سیستم مسدود شده است."
        }), 403

    # 3. بررسی و کسر بودجه تولید تصویر
    is_allowed, result = check_and_deduct_score(user_identifier, 'image')
    if not is_allowed:
        return jsonify({"status": "error", "message": result}), 429
        
    if not persian_prompt or len(persian_prompt.split()) < 1:
        return jsonify({
            "status": "error",
            "message": "لطفاً موضوع دقیق‌تر تصویر مورد نظرتان را به فارسی بنویسید."
        }), 400
        
    try:
        english_prompt = translate_prompt_to_english(persian_prompt)
        file_name = generate_and_crop_image(english_prompt)
        
        if file_name == "TIMEOUT_100_SEC": 
             return jsonify({
                "status": "error",
                "message": "⚠️ سرور تولید تصویر شلوغ است. صبور باشید و مجدداً امتحان کنید."
            }), 503
        
        if file_name:
            local_image_url = url_for('static', filename=f'temp_images/{file_name}')
            
            return jsonify({
                "status": "success",
                "message": f"تصویر شما با پرامپت '{persian_prompt}' تولید شد. 🖼️",
                "image_url": local_image_url
            })
        else:
            return jsonify({
                "status": "error",
                "message": "⚠️ متأسفم، در تولید تصویر مشکلی پیش آمد. (خطا در دانلود یا برش تصویر). لطفاً پرامپت دیگری را امتحان کنید."
            }), 500

    except Exception as e:
        print(f"Image Generator Handler Error: {e}")
        return jsonify({
            "status": "error",
            "message": f"❌ خطای داخلی سرور هنگام پردازش تصویر: {e}"
        }), 500


# =========================================================
# 🤖 مسیرهای API تلگرام (Blueprint)
# =========================================================

telegram_bp = Blueprint('telegram', __name__, url_prefix='/telegram')
bot = Bot(TELEGRAM_BOT_TOKEN)

# نگهداری سشن های تلگرام به صورت موقت در حافظه (در یک پروژه بزرگتر، این باید در دیتابیس یا Redis ذخیره شود)
TELEGRAM_CONVERSATIONS = {}

def get_telegram_conversation(chat_id):
    """بارگذاری یا ایجاد سشن گفتگو برای تلگرام."""
    chat_id = str(chat_id)
    if chat_id not in TELEGRAM_CONVERSATIONS:
        # 1. تلاش برای بارگذاری از دیتابیس (آخرین گفتگو)
        user = User.query.filter_by(telegram_id=chat_id).first()
        if user:
            last_conversation = Conversation.query.filter_by(user_id=user.id).order_by(Conversation.last_update.desc()).first()
            if last_conversation:
                try:
                    messages = json.loads(last_conversation.messages_json)
                    TELEGRAM_CONVERSATIONS[chat_id] = {
                        'messages': messages,
                        'chat_id': last_conversation.id,
                        'user_id': user.id
                    }
                    return TELEGRAM_CONVERSATIONS[chat_id]
                except Exception:
                    pass

        # 2. ایجاد جدید
        TELEGRAM_CONVERSATIONS[chat_id] = {
            'messages': [],
            'chat_id': str(uuid.uuid4()),
            'user_id': user.id if user else None
        }
    return TELEGRAM_CONVERSATIONS[chat_id]

def save_telegram_conversation(chat_id, messages, user_message, user_id):
    """ذخیره گفتگو در دیتابیس برای تلگرام."""
    chat_id_str = str(chat_id)
    conv_data = get_telegram_conversation(chat_id_str)
    conv_data['messages'] = messages
    
    # اگر کاربر دیتابیس پیدا شده است
    if user_id:
        with app.app_context():
            save_conversation(
                user_identifier=chat_id_str, # از chat_id به عنوان identifier موقت برای get_user استفاده می کنیم
                chat_id=conv_data['chat_id'], 
                messages=messages, 
                user_message=user_message
            )


def start_command(update: Update, context: CallbackContext):
    """پاسخ به دستور /start و ثبت کاربر."""
    chat_id = update.effective_chat.id
    username = update.effective_user.username
    
    with app.app_context():
        # ثبت کاربر با telegram_id
        user = register_user_if_new(chat_id, telegram_id=chat_id)
        
        if user and user.is_banned:
            update.message.reply_text("⛔ متأسفم، حساب کاربری شما توسط مدیر سیستم مسدود شده است.")
            return

        welcome_message = (
            f"👋 سلام {username or 'کاربر گرامی'}! به Cyrus AI خوش آمدید.\n"
            f"من یک ربات هوش مصنوعی از تیم NOCTOVEX هستم.\n"
            f"برای شروع گفتگو، پیامتان را ارسال کنید. می‌توانید با دستور /clear_history تاریخچه را پاک کنید."
        )
        
        update.message.reply_text(welcome_message)
        
def clear_history_command(update: Update, context: CallbackContext):
    """پاک کردن تاریخچه گفتگو."""
    chat_id = update.effective_chat.id
    
    # پاک کردن سشن در حافظه
    if str(chat_id) in TELEGRAM_CONVERSATIONS:
        TELEGRAM_CONVERSATIONS.pop(str(chat_id), None)
        
    update.message.reply_text("✅ تاریخچه گفتگو پاک شد. می‌توانید چت جدیدی را شروع کنید.")

def chat_handler(update: Update, context: CallbackContext):
    """پردازش پیام‌های متنی از کاربر."""
    user_message = update.message.text
    chat_id = update.effective_chat.id
    
    with app.app_context():
        
        # 1. بازیابی کاربر و بررسی بن
        user = User.query.filter_by(telegram_id=chat_id).first()
        if not user:
             user = register_user_if_new(chat_id, telegram_id=chat_id)
             
        if user.is_banned:
            update.message.reply_text("⛔ متأسفم، حساب کاربری شما توسط مدیر سیستم مسدود شده است.")
            return

        # 2. بررسی و کسر بودجه چت
        is_allowed, result = check_and_deduct_score(chat_id, 'chat')
        if not is_allowed:
            update.message.reply_text(result)
            return

        # 3. بازیابی سشن
        conv_data = get_telegram_conversation(chat_id)
        messages = conv_data['messages']
        
        # 4. آماده‌سازی پیام‌ها
        messages_list = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages_list.extend(messages)
        messages_list.append({"role": "user", "content": user_message})

        # 5. کوتاه کردن تاریخچه (همانند تابع chat وب)
        while count_tokens(messages_list) >= INPUT_TOKEN_LIMIT and len(messages) >= 2:
            messages = messages[2:]
            
            messages_list = [{"role": "system", "content": SYSTEM_PROMPT}]
            messages_list.extend(messages)
            messages_list.append({"role": "user", "content": user_message})
            
        prompt_tokens = count_tokens(messages_list)
        remaining_tokens = TOTAL_TOKEN_LIMIT - prompt_tokens
        max_tokens_calculated = max(20, remaining_tokens) 
        max_tokens = min(max_tokens_calculated, MAX_COMPLETION_TOKENS)

        data = {
            "model": CHAT_MODEL_NAME, 
            "messages": messages_list,
            "max_tokens": max_tokens
        }

        # 6. فراخوانی API
        res_json, error = call_openrouter_with_fallback(data, 'chat')
        
        if error:
            ai_message = f"⚠️ متأسفم، مشکلی در اتصال پیش آمد: {error}"
        else:
            try:
                ai_message = res_json["choices"][0]["message"]["content"]
                ai_message = fix_rtl_ltr(ai_message)
            except Exception:
                ai_message = "⚠️ مشکلی پیش اومد در تحلیل پاسخ!"

        # 7. ذخیره‌سازی و پاسخ
        messages.append({"role": "user", "content": user_message})
        messages.append({"role": "assistant", "content": ai_message})
        
        if len(messages) > 50:
            messages = messages[-50:]
            
        # ذخیره در دیتابیس
        save_telegram_conversation(chat_id, messages, user_message, user.id)
        
        update.message.reply_text(ai_message, parse_mode='Markdown')


# 8. راه‌اندازی Dispatcher و اضافه کردن هندلرها
def setup_telegram_dispatcher():
    dispatcher = Dispatcher(bot, None, use_context=True)
    dispatcher.add_handler(CommandHandler("start", start_command))
    dispatcher.add_handler(CommandHandler("clear_history", clear_history_command))
    dispatcher.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_handler)) # ✅ اصلاح شد
    return dispatcher


# 9. مسیر اصلی برای وب‌هوک تلگرام
@telegram_bp.route(f"/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
def webhook():
    """دریافت وب‌هوک از سرور تلگرام."""
    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), bot)
        dispatcher = setup_telegram_dispatcher()
        dispatcher.process_update(update)
        return "ok"
    return "Method not allowed", 405

# 🔗 ثبت Blueprint تلگرام
app.register_blueprint(telegram_bp)

# =========================================================
# 🏠 مسیرهای سرویس‌دهی صفحات HTML
# =========================================================

@app.route("/")
def index():
    cleanup_old_images() 
    
    conversation_history = session.get("conversation", [])
    
    display_messages = [
        {"role": msg["role"], "content": fix_rtl_ltr(msg["content"])}
        for msg in conversation_history
    ]
    
    return render_template("index.html", 
        logged_in=session.get('user_id') is not None,
        is_admin=session.get('is_admin', False),
        chat_history=display_messages
    )

@app.route("/image")
def image_page():
    return render_template("image.html", 
        logged_in=session.get('user_id') is not None,
        is_admin=session.get('is_admin', False))


# =========================================================
# 🎮 مسیرهای بازی
# =========================================================
@app.route("/game")
def game_center():
    return render_template("game.html", logged_in=session.get('user_id') is not None)

@app.route("/game/car")
def car_game():
    return render_template("car_game.html", logged_in=session.get('user_id') is not None)

@app.route("/game/guess")
def guess_game():
    return render_template("number_guess_game.html", logged_in=session.get('user_id') is not None)


# --- مسیرهای احراز هویت ---

@app.route("/login")
def login():
    if session.get('user_id'):
        return redirect(url_for('account'))
    return render_template("account_login.html") 

@app.route("/login_phone")
def login_phone():
    if session.get('user_id'):
        return redirect(url_for('account'))
    return render_template("account_login_phone.html") 
    
@app.route("/login_google")
def login_google():
    return redirect(url_for('login')) 
    
@app.route("/account")
def account():
    if not session.get('user_id'):
        return redirect(url_for('login'))
        
    user_id = session.get('user_id')
    user = get_user_by_id(user_id)

    if not user:
         session.clear()
         return redirect(url_for('login'))
        
    if user.is_admin or session.get('is_admin'):
        return redirect(url_for('admin.admin_dashboard')) 
        
    if session.get('needs_profile_info'):
        return redirect(url_for('complete_profile_mock')) 
        
    return redirect(url_for('profile'))


@app.route("/verify_page")
def verify_page():
    return render_template("account_verify.html")

@app.route("/verify_page_phone")
def verify_page_phone():
    return render_template("account_verify_phone.html")

# --- مسیرهای تک صفحه‌ای ---

@app.route("/support")
def support():
    return render_template("support.html")

@app.route("/about")
def about():
    return render_template("about.html")
    
@app.route("/terms_of_service")
def terms_of_service():
    """نمایش صفحه شرایط و قوانین استفاده از سرویس."""
    return render_template("terms_of_service.html")

@app.route("/privacy_policy")
def privacy_policy():
    """نمایش صفحه حریم خصوصی."""
    return render_template("privacy_policy.html")

@app.route("/profile")
def profile():
    if not session.get('user_id'):
        return redirect(url_for('login'))
        
    user_id = session.get('user_id')
    user = get_user_by_id(user_id)

    if not user:
        session.clear()
        return redirect(url_for('login'))
        
    is_premium = user.is_premium
    level = 'premium' if is_premium else 'free'
    today_date = datetime.utcnow().date()
    daily_limits = SCORE_QUOTA_CONFIG['DAILY_BUDGET'][level]
    
    usage = user.usage

    if not usage or usage.date != today_date or usage.level_check != level:
        chat_budget_remaining = daily_limits['chat']
        image_budget_remaining = daily_limits['image']
    else:
        chat_budget_remaining = usage.chat_budget
        image_budget_remaining = usage.image_budget

    chat_cost = SCORE_QUOTA_CONFIG['COSTS']['chat']
    image_cost = SCORE_QUOTA_CONFIG['COSTS']['image']
    
    user_data = {
        'identifier': user.email or user.phone or user.id,
        'is_admin': user.is_admin,
        'score': user.score,
        'is_premium': is_premium,
        'is_banned': user.is_banned,
        
        'chat_budget_remaining': chat_budget_remaining, 
        'image_budget_remaining': image_budget_remaining,
        'chat_cost': chat_cost,
        'image_cost': image_cost,
        
        'chats_remaining': chat_budget_remaining // chat_cost,
        'images_remaining': image_budget_remaining // image_cost,
        
        'max_chats': daily_limits['chat'] // chat_cost,
        'max_images': daily_limits['image'] // image_cost,

    }

    return render_template("account_profile.html", user_data=user_data)
    
@app.route("/complete_profile", methods=['GET', 'POST']) 
def complete_profile_mock():
    if not session.get('user_id'):
        return redirect(url_for('login'))
    
    user_id = session.get('user_id')
    user = get_user_by_id(user_id)
    
    if not user:
        session.clear()
        return redirect(url_for('login'))
        
    user_data = {
        'identifier': user.email or user.phone or user.id,
    }
    
    if request.method == 'POST':
        user_name = request.form.get('user_name') 
        user_phone = request.form.get('user_phone') 
        
        session.pop('needs_profile_info', None) 
        
        return redirect(url_for('account')) 

    return render_template("account_form.html", user_data=user_data) 

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for('index')) 
    
# =========================================================
# 💾 مسیرهای آرشیو گفتگو
# =========================================================

@app.route("/my_conversations")
def my_conversations():
    if not session.get('user_id'):
        return redirect(url_for('login'))
    return render_template("my_conversations.html")

@app.route("/get_conversations_list", methods=["GET"])
def get_conversations_list():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"status": "error", "message": "لطفاً ابتدا وارد حساب کاربری خود شوید."}), 403

    conversations_query = Conversation.query.filter_by(user_id=user_id).order_by(Conversation.last_update.desc()).all()
    
    formatted_list = []
    for chat in conversations_query:
        date_str = time.strftime('%Y/%m/%d - %H:%M', time.localtime(chat.last_update))
        
        try:
            messages = json.loads(chat.messages_json)
            preview = messages[1]['content'][:80] + '...' if len(messages) > 1 else 'شروع گفتگو...'
        except Exception:
            preview = 'خطا در بارگذاری پیام‌ها...'

        formatted_list.append({
            'id': chat.id,
            'title': chat.title,
            'last_update': date_str,
            'preview': preview
        })
    
    return jsonify({"status": "success", "conversations": formatted_list})

@app.route("/load_conversation/<chat_id>", methods=["POST"])
def load_conversation(chat_id):
    """API برای بارگذاری یک گفتگوی خاص در سشن کاربر."""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"status": "error", "message": "مجوز دسترسی ندارید."}), 403

    chat_entry = Conversation.query.filter_by(id=chat_id, user_id=user_id).first()
    
    if chat_entry:
        try:
            session['conversation'] = json.loads(chat_entry.messages_json)
            session['current_chat_id'] = chat_entry.id 
            return jsonify({"status": "success", "message": "گفتگو با موفقیت بارگذاری شد.", "redirect": url_for('index')})
        except Exception:
            return jsonify({"status": "error", "message": "خطا در پردازش داده‌های گفتگو."}), 500
    else:
        return jsonify({"status": "error", "message": "گفتگوی مورد نظر یافت نشد."}), 404


# =========================================================
# ▶️ اجرای برنامه
# =========================================================

if __name__ == "__main__":
    
    if os.environ.get("FLASK_ENV") != "production":
        cleanup_old_images() 
        
    # ⬅️ نکته: در رندر نباید در محیط اصلی برنامه یک وب‌هوک تنظیم شود.
    # باید از طریق پنل تلگرام وب‌هوک را تنظیم کنید: 
    # https://api.telegram.org/bot[YOUR_TOKEN]/setWebhook?url=[YOUR_RENDER_URL]/telegram/[YOUR_TOKEN]
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)