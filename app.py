import os
from urllib.parse import quote # کتابخانه مورد نیاز برای انکود کردن آدرس بازار
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
from flask_sqlalchemy import SQLAlchemy
from datetime import date, datetime
import sqlalchemy.exc
from sqlalchemy import or_

# =========================================================
# 🛠️ تنظیمات اولیه و اتصال به دیتابیس
# =========================================================
app = Flask(__name__)

# --- تنظیمات ضروری ---
app.jinja_env.charset = 'utf-8'

# ⚠️ رفع ایراد امنیتی ۱: استفاده از متغیرهای محیطی برای کلیدها
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    # اگر در متغیر محیطی تنظیم نشده باشد، یک کلید موقت و ناامن استفاده می‌کند
    print("Warning: SECRET_KEY not set in environment. Using insecure fallback.")
    SECRET_KEY = "fallback_insecure_key_12345" 
app.secret_key = SECRET_KEY

# 👑 شماره تلفن ادمین برای دسترسی مستقیم
ADMIN_PHONE_NUMBER = '09962935294'

# 🔔 شماره تلفن برای دریافت هشدار اتمام توکن
TOKEN_ALERT_PHONE_NUMBER = '09023287024'

# 🛍️ تنظیمات ورود با بازار (Bazaar Login Config)
BAZAAR_CLIENT_ID = os.getenv("BAZAAR_CLIENT_ID")
BAZAAR_CLIENT_SECRET = os.getenv("BAZAAR_CLIENT_SECRET")

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
app.config['MAIL_USERNAME'] = 'noctovex@gmail.com' # بهتر است این هم متغیر محیطی باشد
# ⚠️ رفع ایراد امنیتی ۲: حذف رمز عبور هاردکد شده
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
if not app.config['MAIL_PASSWORD']:
    raise ValueError("❌ متغیر محیطی MAIL_PASSWORD (رمز عبور ایمیل) پیدا نشد! لطفاً آن را تنظیم کنید.")

app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USE_SSL'] = False
mail = Mail(app)

# ⚠️ حذف دیکشنری های ناپایدار: verification_codes حذف شد

# ----------------- 📱 تنظیمات Kavenegar -----------------
# ⚠️ رفع ایراد امنیتی ۳: حذف کلید API هاردکد شده
KAVENEGAR_API_KEY = os.getenv('KAVENEGAR_API_KEY')
if not KAVENEGAR_API_KEY:
    raise ValueError("❌ متغیر محیطی KAVENEGAR_API_KEY پیدا نشد! لطفاً آن را تنظیم کنید.")
    
KAVENEGAR_SENDER = '2000300261'
SMS_API = KavenegarAPI(KAVENEGAR_API_KEY)
# ⚠️ حذف دیکشنری های ناپایدار: phone_verification_codes حذف شد
# ---------------------------------------------------------

# =========================================================
# 🔑 سیستم مدیریت کلیدهای OpenRouter (Key Rotation & Fallback)
# =========================================================

# 1. بارگذاری تمام کلیدهای تعریف شده در متغیرهای محیطی
OPENROUTER_KEYS = {}
for i in range(1, 6): # از 1 تا 5
    key_name = f"OPENROUTER_API_KEY_{i}"
    key_value = os.getenv(key_name)
    if key_value:
        OPENROUTER_KEYS[key_name] = key_value

if not OPENROUTER_KEYS:
    raise ValueError("❌ حداقل یک متغیر محیطی OPENROUTER_API_KEY_i پیدا نشد! لطفاً حداقل یکی را تنظیم کنید.")

# 2. متغیرهای سراسری برای مدیریت حالت کلیدها
# لیست نام کلیدها برای حفظ ترتیب چرخش
KEY_NAMES_ORDER = list(OPENROUTER_KEYS.keys()) 
# کلیدهایی که به دلیل خطا (402, 401) مسدود شده‌اند
BLOCKED_KEYS = set()
# شاخص برای شروع جستجوی کلید فعال
KEY_INDEX = 0

def send_token_alert(key_name, reason):
    """ارسال پیامک هشدار برای اتمام/خطای کلید API."""
    if not TOKEN_ALERT_PHONE_NUMBER or not KAVENEGAR_API_KEY:
        print("Warning: TOKEN_ALERT_PHONE_NUMBER or KAVENEGAR_API_KEY not set.")
        return

    try:
        params = {
            'sender': KAVENEGAR_SENDER,
            'receptor': TOKEN_ALERT_PHONE_NUMBER,
            'message': f'⚠️ اخطار! کلید OpenRouter ({key_name}) با خطا مواجه شد ({reason}). موقتا مسدود شد.',
        }
        # SMS_API از قبل با کلید تنظیم شده
        SMS_API.sms_send(params) 
        print(f"🔔 هشدار پیامکی برای {key_name} ارسال شد.")
    except Exception as e:
        print(f"Error sending SMS alert: {e}")

def handle_key_failure(key_name, status_code):
    """مسدود کردن کلید معیوب و ارسال هشدار."""
    if key_name not in BLOCKED_KEYS:
        BLOCKED_KEYS.add(key_name)
        reason = f"HTTP {status_code}"
        send_token_alert(key_name, reason)
        print(f"❌ کلید {key_name} به دلیل خطای {status_code} مسدود شد.")

def get_openrouter_key(initial_attempt=True):
    """برگرداندن کلید فعال بعدی به صورت چرخشی (Round-Robin)."""
    global KEY_INDEX
    
    total_keys = len(KEY_NAMES_ORDER)
    if total_keys == 0:
        return None, None

    # اگر همه کلیدها مسدود باشند، یکبار سعی می‌کنیم همه را ریست کنیم
    if len(BLOCKED_KEYS) == total_keys and initial_attempt:
        print("🚨 همه کلیدهای API مسدود هستند. ریست کردن و تلاش مجدد.")
        BLOCKED_KEYS.clear()
        
    # شروع چرخش از شاخص فعلی
    for _ in range(total_keys):
        key_name = KEY_NAMES_ORDER[KEY_INDEX]
        
        # مهم: شاخص را برای تلاش بعدی افزایش بده
        KEY_INDEX = (KEY_INDEX + 1) % total_keys

        if key_name not in BLOCKED_KEYS:
            return key_name, OPENROUTER_KEYS[key_name]
    
    # اگر بعد از چرخش کامل، هیچ کلید فعالی پیدا نشد
    return None, None
# ---------------------------------------------------------

# 🎯 تنظیمات هزینه و بودجه امتیاز روزانه
SCORE_QUOTA_CONFIG = {
    'COSTS': {
        'chat': 1, # هر چت 1 امتیاز
        'image': 20, # هر عکس 20 امتیاز
        'long_response': 1 # 💡 هزینه هر پاسخ بلند
    },
    'DAILY_BUDGET': {
        'free': {
            'chat': 30,  # 30 امتیاز برای چت (30 چت)
            'image': 80,  # 80 امتیاز برای تصویر (4 عکس)
            'long_response': 5 # 💡 5 پاسخ بلند روزانه
        },
        'premium': {
            'chat': 80, # 80 امتیاز برای چت (80 چت)
            'image': 200, # 200 امتیاز برای تصویر (10 عکس)
            'long_response': 15 # 💡 15 پاسخ بلند روزانه
        }
    }
}

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
CHAT_MODEL_NAME = "deepseek/deepseek-chat"
TRANSLATION_MODEL_NAME = "google/gemini-2.0-flash-exp:free"

POLLINATIONS_URL = "https://image.pollinations.ai/prompt/"
STATIC_DIR = os.path.join(app.root_path, 'static', 'temp_images')
IMAGE_LIFETIME = 3600

IMAGE_QUALITY_PARAMS = [
    "hd", "detailed", "4k", "8k", "highly detailed",
    "trending on artstation", "cinematic light", "masterpiece", "photorealistic"
]

if not os.path.exists(STATIC_DIR):
    os.makedirs(STATIC_DIR)

# 💡 تغییر: اعمال محدودیت سخت‌گیرانه توکن و خلاصه سازی
SYSTEM_PROMPT = """
تو یک چت‌بات مفید هستی. پاسخ‌ها را به زبان فارسی و روان بده.
- برای سوالات سازنده: تیم NOCTOVEX به رهبری مهراب عزیزی
- پاسخ‌های تو باید **کامل، خلاصه و متمرکز** بر روی هسته سوال باشند.
- **به هیچ عنوان از ۲۰۰ توکن برای پاسخ استفاده نکن** مگر اینکه ناچار باشی.
- هدف تو مصرف حداقل توکن ممکن برای ارائه یک پاسخ کافی است.
"""

# 💡 ثابت‌های جدید برای محدودیت توکن پیام ورودی (درخواست کاربر)
MAX_PROMPT_TOKEN_ALL = 750 # محدودیت حداکثر توکن پیام ورودی برای همه کاربران
MAX_PROMPT_TOKEN_NON_PREMIUM = 700 # 💡 تغییر: محدودیت حداکثر توکن پیام ورودی برای کاربران غیرپرمیوم (700)
PREMIUM_ONLY_MESSAGE = "پیام های طولانی فقط برای افراد پرمیوم وصله. برای پرمیوم کردن به این آیدی در تلگرام پیام بدهید: Im_Mehrab_1" # 💡 تغییر: اضافه شدن آیدی تلگرام


# 💡 ثابت‌های جدید برای حالت پاسخ بلند
LONG_RESPONSE_TOKEN_THRESHOLD = 701 # 💡 تغییر: آستانه برای ورود به حالت بلند یا بلاک (بالاتر از سقف غیرپرمیوم)
LONG_RESPONSE_MAX_COMPLETION_TOKENS = 4000 
LONG_RESPONSE_TOTAL_TOKEN_LIMIT = 4096 


TOTAL_TOKEN_LIMIT = 1000 # 💡 تغییر: کاهش سقف کل توکن به ۱۰۰۰
INPUT_TOKEN_LIMIT = 700 # 💡 تغییر: کاهش سقف توکن ورودی برای فشرده‌سازی زودتر
MAX_COMPLETION_TOKENS = 300 # 💡 تغییر: کاهش سقف توکن خروجی به ۳۰۰ برای پاسخ‌های کوتاه

# 💡 ثابت جدید برای محدودیت چت مهمان
GUEST_CHAT_LIMIT = 5 

encoder = tiktoken.get_encoding("cl100k_base")

# =========================================================
# 🏛️ مدل‌های دیتابیس (SQLAlchemy Models)
# =========================================================
class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email = db.Column(db.String(120), unique=True, nullable=True)
    phone = db.Column(db.String(15), unique=True, nullable=True)
    score = db.Column(db.Integer, default=0)
    is_premium = db.Column(db.Boolean, default=False)
    is_banned = db.Column(db.Boolean, default=False)
    is_admin = db.Column(db.Boolean, default=False)

    usage = db.relationship('UserUsage', backref='user', lazy=True, uselist=False)
    conversations = db.relationship('Conversation', backref='user', lazy='dynamic')


class UserUsage(db.Model):
    __tablename__ = 'user_usage'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), unique=True, nullable=False)

    # ⚠️ رفع ایراد مدل: استفاده از lambda برای callable کردن default
    date = db.Column(db.Date, default=lambda: datetime.utcnow().date())

    chat_budget = db.Column(db.Integer, default=50)
    image_budget = db.Column(db.Integer, default=60)
    long_response_budget = db.Column(db.Integer, default=5) # 💡 فیلد جدید
    level_check = db.Column(db.String(10), nullable=True)


class Conversation(db.Model):
    __tablename__ = 'conversations'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False)
    title = db.Column(db.String(100), nullable=False, default="گفتگوی جدید...")

    # ⚠️ رفع ایراد مدل: استفاده از lambda برای callable کردن default
    last_update = db.Column(db.Float, default=lambda: time.time())

    messages_json = db.Column(db.Text, nullable=False)

# 💾 مدل جدید برای کدهای تأیید (رفع ایراد پایداری)
class VerificationCode(db.Model):
    __tablename__ = 'verification_codes'
    identifier = db.Column(db.String(120), primary_key=True) # Email or Phone
    code = db.Column(db.String(6), nullable=False)
    expiry_time = db.Column(db.Float, nullable=False) # time.time() float


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
    # KAVENEGAR_API_KEY توسط چک‌های اولیه تضمین شده است
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
    # از session['user_identifier'] که در لاگین ست شده استفاده می‌کند
    return session.get('user_identifier') 

def get_user_by_identifier(identifier):
    """یافتن کاربر بر اساس ایمیل یا شماره تلفن."""
    return User.query.filter(
        or_(User.email == identifier, User.phone == identifier)
    ).first()

def get_user_by_id(user_id):
    """یافتن کاربر بر اساس UUID."""
    return User.query.get(user_id)


def register_user_if_new(user_identifier, email=None, phone=None):
    """
    اگر کاربر جدید است، آن را در دیتابیس ثبت می‌کند.
    اگر موجود است، اطلاعات لاگین (email/phone) را به‌روز می‌کند و آبجکت User را برمی‌گرداند.
    """
    user = get_user_by_identifier(user_identifier)

    if not user:
        is_admin = (phone == ADMIN_PHONE_NUMBER)
        user = User(
            id=str(uuid.uuid4()),
            email=email,
            phone=phone,
            score=0,
            is_premium=False,
            is_banned=False,
            is_admin=is_admin
        )
        db.session.add(user)
    else:
        if email:
            user.email = email
        if phone:
            user.phone = phone

    # در اینجا commit نمی‌کنیم تا تابع verify آن را انجام دهد و تراکنش اتمیک باشد
    try:
        db.session.flush() # اعمال تغییرات بدون commit نهایی
        return user
    except sqlalchemy.exc.IntegrityError as e:
        db.session.rollback()
        print(f"Database Integrity Error during registration: {e}")
        return None


def check_and_deduct_score(user_identifier, usage_type):
    """
    بررسی بودجه امتیاز روزانه، کسر هزینه و ذخیره.
    """
    user = get_user_by_identifier(user_identifier)
    if not user:
        return False, "خطای داخلی: کاربر در دیتابیس یافت نشد."

    today_date = datetime.utcnow().date()

    is_premium = user.is_premium
    level = 'premium' if is_premium else 'free'
    cost = SCORE_QUOTA_CONFIG['COSTS'][usage_type]
    daily_limits = SCORE_QUOTA_CONFIG['DAILY_BUDGET'][level]
    budget_key = f'{usage_type}_budget'

    usage = user.usage

    if not usage:
        usage = UserUsage(
            user_id=user.id,
            date=today_date,
            chat_budget=daily_limits['chat'],
            image_budget=daily_limits['image'],
            long_response_budget=daily_limits.get('long_response', 0), # 💡 به‌روزرسانی سهمیه اولیه
            level_check=level
        )
        db.session.add(usage)
    # ⚠️ در اینجا باید دقت کنید که usage.date یک آبجکت date است
    elif usage.date != today_date or usage.level_check != level: 
        usage.date = today_date
        usage.chat_budget = daily_limits['chat']
        usage.image_budget = daily_limits['image']
        usage.long_response_budget = daily_limits.get('long_response', 0) # 💡 به‌روزرسانی سهمیه ریست روزانه
        usage.level_check = level

    current_budget = getattr(usage, budget_key, 0)

    if current_budget < cost:
        action_fa = (
            'چت' if usage_type == 'chat' else 
            'تولید تصویر' if usage_type == 'image' else 
            'پاسخ بلند' # 💡 اضافه شدن نوع استفاده
        )
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
    user = get_user_by_identifier(user_identifier)
    if not user:
        return

    chat_entry = Conversation.query.filter_by(id=chat_id, user_id=user.id).first()

    messages_json_string = json.dumps(messages, ensure_ascii=False)

    if chat_entry:
        chat_entry.messages_json = messages_json_string
        # ⚠️ رفع ایراد مدل: استفاده از lambda برای callable کردن default
        chat_entry.last_update = time.time()
        if chat_entry.title == "گفتگوی جدید...":
             chat_entry.title = user_message[:50] + "..." if len(user_message) > 50 else user_message
    else:
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

def translate_prompt_to_english(persian_prompt):
    """تلاش برای ترجمه پرامپت با استفاده از مکانیزم چرخشی کلیدها."""
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
    
    max_attempts = len(OPENROUTER_KEYS)

    # حلقه تلاش مجدد
    for attempt in range(max_attempts):
        key_name, current_api_key = get_openrouter_key(initial_attempt=(attempt==0))
        
        if not current_api_key:
            # اگر هیچ کلیدی فعال نیست، با پرامپت فارسی ادامه بده
            return persian_prompt 

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {current_api_key}"
        }

        data = {
            "model": TRANSLATION_MODEL_NAME,
            "messages": messages,
            "max_tokens": 150
        }

        try:
            response = requests.post(OPENROUTER_URL, json=data, headers=headers, timeout=15)
            response.raise_for_status()
            res_json = response.json()
            english_prompt = res_json["choices"][0]["message"]["content"].strip()
            return english_prompt # موفقیت
            
        except requests.exceptions.RequestException as e:
            status_code = getattr(e.response, 'status_code', 500)
            print(f"Translation API Error (Key: {key_name}): {e}. Status: {status_code}")
            
            # اگر 402 یا 401 بود، کلید را مسدود و تلاش بعدی
            if status_code in [402, 401]:
                handle_key_failure(key_name, status_code) 
                # اگر آخرین کلید بود، پرامپت فارسی را برگردان
                if attempt == max_attempts - 1:
                    return persian_prompt 
                continue # برو به کلید بعدی
            else:
                return persian_prompt # خطای دیگر (مانند 500)
        
        except Exception as e:
            print(f"Translation General Error: {e}")
            return persian_prompt
            
    # اگر حلقه بدون موفقیت کامل شد
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
        # از user_id به عنوان روش اصلی احراز هویت استفاده شود
        user_id = session.get('user_id') 
        user = get_user_by_id(user_id) if user_id else None

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

    # user_identifier را از session به دست می‌آورد
    admin_identifier = get_user_identifier(session) 

    context = {
        'total_users': total_users,
        'premium_users': premium_users,
        'banned_users': banned_users,
        'admin_identifier': admin_identifier
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
    """ارسال کد تأیید برای ایمیل و ذخیره در دیتابیس (Persistence Fix)"""
    user_email = request.json.get("email", "").strip().lower()

    if not user_email:
        return jsonify({"status": "error", "message": "لطفاً ایمیل خود را وارد کنید."}), 400

    code = generate_verification_code()
    expiry = time.time() + 300 # 5 minutes

    # 💾 ذخیره/به‌روزرسانی کد در دیتابیس
    code_entry = VerificationCode.query.filter_by(identifier=user_email).first()
    if code_entry:
        code_entry.code = code
        code_entry.expiry_time = expiry
    else:
        code_entry = VerificationCode(identifier=user_email, code=code, expiry_time=expiry)
        db.session.add(code_entry)
        
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Database error saving code: {e}")
        return jsonify({"status": "error", "message": "خطای دیتابیس در ذخیره کد."}), 500


    if not send_verification_email(user_email, code):
        return jsonify({"status": "error", "message": "خطا در ارسال ایمیل. لطفاً ایمیل خود را بررسی کنید."}), 500

    return jsonify({"status": "success", "message": "کد تأیید به ایمیل شما ارسال شد. لطفاً صندوق ورودی خود را بررسی کنید."})

@app.route("/verify_code", methods=["POST"])
def verify_code():
    """تأیید کد ایمیلی و لاگین کاربر."""
    user_email = request.json.get("email", "").strip().lower()
    entered_code = request.json.get("code", "").strip()

    # 🔍 بازیابی کد از دیتابیس
    stored_data = VerificationCode.query.filter_by(identifier=user_email).first()
    
    if not stored_data:
        return jsonify({"status": "error", "message": "ایمیل نامعتبر یا درخواستی برای آن ثبت نشده است."}), 400

    if time.time() > stored_data.expiry_time:
        db.session.delete(stored_data)
        db.session.commit()
        return jsonify({"status": "error", "message": "کد تأیید منقضی شده است. لطفاً مجدداً درخواست کد دهید."}), 400

    if entered_code == stored_data.code:
        # 1. ثبت یا بازیابی کاربر (فلاشت می‌شود)
        user = register_user_if_new(user_email, email=user_email)
        
        if not user:
            # register_user_if_new قبلاً rollback کرده
            return jsonify({"status": "error", "message": "خطا در ثبت/بازیابی کاربر از دیتابیس."}), 500
        
        # 2. حذف کد تأیید پس از استفاده
        db.session.delete(stored_data)

        # 3. commit نهایی تراکنش
        try:
            db.session.commit() 
        except Exception as e:
            db.session.rollback()
            print(f"Final commit error: {e}")
            return jsonify({"status": "error", "message": "خطا در ذخیره سازی نهایی."}), 500


        session.clear() # پاک کردن سشن قبلی
        session['user_id'] = user.id
        session['user_identifier'] = user_email
        session['is_admin'] = user.is_admin

        # انتقال به داشبورد یا صفحه اصلی
        if user.is_admin:
            return jsonify({"status": "success", "redirect": url_for('admin.admin_dashboard')})
        
        return jsonify({"status": "success", "redirect": url_for('account')})
    else:
        return jsonify({"status": "error", "message": "کد وارد شده صحیح نیست."}), 400


@app.route("/send_sms_code", methods=["POST"])
def send_sms_code():
    """دریافت شماره تلفن و ارسال کد تأیید پیامکی و ذخیره در دیتابیس."""
    phone_number = request.json.get("phone", "").strip()

    if not re.match(r'^0?9\d{9}$', phone_number):
        return jsonify({"status": "error", "message": "لطفاً یک شماره تلفن معتبر (مانند 0912...) وارد کنید."}), 400

    code = generate_verification_code()
    expiry = time.time() + 300 # 5 minutes
    
    # 💾 ذخیره/به‌روزرسانی کد در دیتابیس
    code_entry = VerificationCode.query.filter_by(identifier=phone_number).first()
    if code_entry:
        code_entry.code = code
        code_entry.expiry_time = expiry
    else:
        code_entry = VerificationCode(identifier=phone_number, code=code, expiry_time=expiry)
        db.session.add(code_entry)
        
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Database error saving code: {e}")
        return jsonify({"status": "error", "message": "خطای دیتابیس در ذخیره کد."}), 500

    if not send_verification_sms(phone_number, code):
        return jsonify({"status": "error", "message": "خطا در ارسال پیامک. لطفاً شماره را بررسی کنید."}), 500

    return jsonify({"status": "success", "message": "کد تأیید به شماره شما ارسال شد. لطفاً پیامک‌ها را بررسی کنید."})

@app.route("/verify_sms_code", methods=["POST"])
def verify_sms_code():
    """تأیید کد پیامکی و لاگین کاربر."""
    phone_number = request.json.get("phone", "").strip()
    entered_code = request.json.get("code", "").strip()

    # 🔍 بازیابی کد از دیتابیس
    stored_data = VerificationCode.query.filter_by(identifier=phone_number).first()
    
    if not stored_data:
        return jsonify({"status": "error", "message": "شماره نامعتبر یا درخواستی برای آن ثبت نشده است."}), 400

    if time.time() > stored_data.expiry_time:
        db.session.delete(stored_data)
        db.session.commit()
        return jsonify({"status": "error", "message": "کد تأیید منقضی شده است. لطفاً مجدداً درخواست کد دهید."}), 400

    if entered_code == stored_data.code:
        # 1. ثبت یا بازیابی کاربر (فلاشت می‌شود)
        user = register_user_if_new(phone_number, phone=phone_number)

        if not user:
            # register_user_if_new قبلاً rollback کرده
            return jsonify({"status": "error", "message": "خطا در ثبت/بازیابی کاربر از دیتابیس."}), 500
        
        # 2. حذف کد تأیید پس از استفاده
        db.session.delete(stored_data)

        # 3. commit نهایی تراکنش
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"Final commit error: {e}")
            return jsonify({"status": "error", "message": "خطا در ذخیره سازی نهایی."}), 500


        session.clear() # پاک کردن سشن قبلی
        session['user_id'] = user.id
        session['user_identifier'] = phone_number
        session['is_admin'] = user.is_admin

        # انتقال به داشبورد یا صفحه اصلی
        if user.is_admin:
            return redirect(url_for('admin.admin_dashboard'))
        
        return jsonify({"status": "success", "redirect": url_for('account')})
    else:
        return jsonify({"status": "error", "message": "کد وارد شده صحیح نیست."}), 400

# =========================================================
# ⚙️ مسیرهای کاربری (Logout & Account)
# =========================================================
@app.route("/account")
def account():
    """صفحه پروفایل و اطلاعات کاربری."""
    user_id = session.get('user_id')

    if not user_id:
        return redirect(url_for('login'))

    user = get_user_by_id(user_id)

    if not user:
        session.clear()
        return redirect(url_for('login'))

    today_date = datetime.utcnow().date()
    is_premium = user.is_premium
    level = 'premium' if is_premium else 'free'
    daily_limits = SCORE_QUOTA_CONFIG['DAILY_BUDGET'][level]

    usage = user.usage

    chat_budget_remaining = 0
    image_budget_remaining = 0
    long_response_budget_remaining = 0 # 💡 سهمیه پاسخ بلند

    if not usage:
        # اگر تا حالا استفاده نکرده، بودجه کامل روزانه را نمایش بده
        chat_budget_remaining = daily_limits['chat']
        image_budget_remaining = daily_limits['image']
        long_response_budget_remaining = daily_limits.get('long_response', 0) # 💡 سهمیه پاسخ بلند

    elif usage.date != today_date or usage.level_check != level:
        chat_budget_remaining = daily_limits['chat']
        image_budget_remaining = daily_limits['image']
        long_response_budget_remaining = daily_limits.get('long_response', 0) # 💡 سهمیه پاسخ بلند

    else:
        chat_budget_remaining = usage.chat_budget
        image_budget_remaining = usage.image_budget
        long_response_budget_remaining = usage.long_response_budget # 💡 سهمیه پاسخ بلند

    
    chat_cost = SCORE_QUOTA_CONFIG['COSTS']['chat']
    image_cost = SCORE_QUOTA_CONFIG['COSTS']['image']
    long_response_cost = SCORE_QUOTA_CONFIG['COSTS'].get('long_response', 1) # 💡 هزینه پاسخ بلند

    user_data = {
        'identifier': user.email or user.phone or user.id,
        'is_admin': user.is_admin,
        'score': user.score,
        'is_premium': is_premium,
        'is_banned': user.is_banned,
        'chat_budget_remaining': chat_budget_remaining,
        'image_budget_remaining': image_budget_remaining,
        'long_response_budget_remaining': long_response_budget_remaining, # 💡 اضافه شده

        'chat_cost': chat_cost,
        'image_cost': image_cost,
        'long_response_cost': long_response_cost, # 💡 اضافه شده
        
        'chats_remaining': chat_budget_remaining // chat_cost,
        'images_remaining': image_budget_remaining // image_cost,
        'long_responses_remaining': long_response_budget_remaining // long_response_cost if long_response_cost > 0 else long_response_budget_remaining, # 💡 اضافه شده

        'max_chats': daily_limits['chat'] // chat_cost,
        'max_images': daily_limits['image'] // image_cost,
        'max_long_responses': daily_limits.get('long_response', 0) // long_response_cost if long_response_cost > 0 else daily_limits.get('long_response', 0) # 💡 اضافه شده
    }

    conversations = []
    if user_id:
        conversations = Conversation.query.filter_by(user_id=user_id).order_by(Conversation.last_update.desc()).all()
        # فقط ۵ گفتگوی اخیر
        conversations = conversations[:5] 

    return render_template("account.html", user=user_data, conversations=conversations)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for('index'))

# =========================================================
# 💬 مسیر چت و منطق اصلی (CORE LOGIC)
# =========================================================

@app.route("/chat", methods=["POST"])
def chat():
    """دریافت پیام کاربر، مدیریت سشن، کسر امتیاز و تولید پاسخ AI."""
    user_message = request.json.get("message", "").strip()
    user_identifier = get_user_identifier(session)
    user = get_user_by_identifier(user_identifier) if user_identifier else None
    
    is_premium = user.is_premium if user else False

    # 1. بررسی پیام ورودی خالی
    if not user_message:
        return jsonify({"reply": "لطفاً پیام خود را وارد کنید."})

    # 2. شمارش توکن پیام ورودی
    user_message_tokens = count_tokens([{"role": "user", "content": user_message}])
    
    # 3. اعمال محدودیت توکن پیام ورودی (برای پرمیوم و غیرپرمیوم)
    
    # محدودیت کلی برای همه (حتی پرمیوم)
    if user_message_tokens > MAX_PROMPT_TOKEN_ALL: # 750
        return jsonify({
            "reply": f"⛔ متأسفم، پیام شما خیلی طولانی است و از سقف کلی {MAX_PROMPT_TOKEN_ALL} توکن تجاوز می‌کند."
        })
    
    # محدودیت برای کاربران غیرپرمیوم و مهمان (حالا دقیقاً 700 توکن)
    if not is_premium and user_message_tokens > MAX_PROMPT_TOKEN_NON_PREMIUM: # 700
        return jsonify({ 
            "reply": f"⛔ متأسفم، ({user_message_tokens} توکن). {PREMIUM_ONLY_MESSAGE}"
        })

    # =========================================================
    # 💡 مدیریت بودجه و محدودیت مهمان
    is_long_response = False
    usage_type = 'chat'
    
    if user and user_identifier:
        # اگر پرمیوم است و پیام طولانی داده (برای ارتقا به حالت پاسخ بلند اگر سقفش کم بود)
        if is_premium and user_message_tokens >= LONG_RESPONSE_TOKEN_THRESHOLD: # 701
            usage_type = 'long_response'
            is_long_response = True
        
        # 1. بررسی وضعیت بن
        if user.is_banned:
            return jsonify({"reply": "⛔ متأسفم، حساب کاربری شما توسط مدیر سیستم مسدود شده است."})

        # 2. بررسی و کسر بودجه چت/پاسخ بلند
        is_allowed, result = check_and_deduct_score(user_identifier, usage_type)
        if not is_allowed:
            return jsonify({"reply": result})
    else:
        # 💡 مدیریت کاربران مهمان و اعمال محدودیت ۵ چت روزانه
        today_date_str = datetime.utcnow().date().isoformat()
        
        # ریست کانتر مهمان اگر روز جدید است
        if session.get('guest_last_date') != today_date_str:
            session['guest_chat_count'] = 0
            session['guest_last_date'] = today_date_str

        guest_count = session.get('guest_chat_count', 0)

        if guest_count >= GUEST_CHAT_LIMIT:
            return jsonify({ 
                "reply": "⛔ متأسفم، شما به سقف **۵ چت روزانه** برای کاربران مهمان رسیده‌اید. لطفاً وارد حساب کاربری خود شوید تا چت‌های نامحدود دریافت کنید."
            })
        
        # اگر مهمان و پیامش بالای ۷۰۰ بود، اینجا هم بلاک می‌شود (تکراری ولی برای اطمینان)
        if user_message_tokens > MAX_PROMPT_TOKEN_NON_PREMIUM:
             return jsonify({ 
                "reply": f"⛔ متأسفم، این پیام طولانی است. {PREMIUM_ONLY_MESSAGE}"
            })
        
        # اگر مهمان و مجاز بود، کانتر را افزایش بده.
        session['guest_chat_count'] = guest_count + 1
        
        # برای مهمان، از سقف بالای توکن استفاده نمی‌کنیم (is_long_response = False)
        is_long_response = False
        usage_type = 'chat' # مهمان فقط چت عادی دارد

    # --- پاسخ‌های داخلی (Built-in) ---
    lower_msg = user_message.lower()
    if 'امکانات' in lower_msg or 'چه کاری' in lower_msg or 'چیکار' in lower_msg:
        new_reply = "من یک هوش مصنوعی هستم که توسط تیم NOCTOVEX توسعه داده شده‌ام. می‌توانم: پاسخ‌های دقیق و مفصل به سوالات شما بدهم 🧠، تصاویر خلاقانه با هوش مصنوعی تولید کنم 🖼️، برای شما بازی کنم 🎮 و موارد دیگر..."
        return jsonify({"reply": new_reply})
    
    TRIGGER_KEYWORDS = ['سازنده', 'توسعه دهنده', 'تیم']
    if any(keyword in lower_msg for keyword in TRIGGER_KEYWORDS):
        new_reply = "من توسط تیم NOCTOVEX توسعه داده شده‌ام. این تیم توسط **مهراب عزیزی** رهبری می‌شود که مدیریت پروژه، برنامه‌ریزی و هدایت توسعه‌دهندگان را بر عهده دارد. 👑"
        return jsonify({"reply": new_reply})
        
    # --- مدیریت تاریخچه و توکن‌ها ---
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
            else:
                session.pop('current_chat_id', None)
                session["conversation"] = []
                current_chat_id = str(uuid.uuid4())
                session['current_chat_id'] = current_chat_id
    else:
        session.pop('current_chat_id', None)
        if "conversation" not in session:
            session["conversation"] = []

    # 💡 تنظیم سقف توکن بر اساس حالت چت (اولویت با سقف پایین شماست)
    # -----------------------------------------------------------------------
    if is_long_response:
        # اگر پرمیوم بود و پیام طولانی داد (این مسیر را باز می‌گذاریم)
        current_total_token_limit = LONG_RESPONSE_TOTAL_TOKEN_LIMIT
        max_tokens = LONG_RESPONSE_MAX_COMPLETION_TOKENS
    else:
        # حالت پیش فرض (شامل کاربران عادی، مهمان و پرمیوم‌هایی که پیام کوتاه دادند)
        current_total_token_limit = TOTAL_TOKEN_LIMIT # 1000
        max_tokens = MAX_COMPLETION_TOKENS # 300
    
    system_prompt_to_use = SYSTEM_PROMPT 
    # -----------------------------------------------------------------------

    messages_list = [{"role": "system", "content": system_prompt_to_use}]
    messages_list.extend(session.get("conversation", []))
    messages_list.append({"role": "user", "content": user_message})

    # --- فشرده‌سازی تاریخچه و محاسبه توکن ---
    # اگر بعد از اضافه کردن پیام جدید، توکن بیش از حد شد، فقط دو پیام قدیمی را حذف کن
    while count_tokens(messages_list) >= current_total_token_limit and len(session["conversation"]) >= 2:
        # حذف دو پیام قدیمی (یک جفت سوال و جواب)
        session["conversation"] = session["conversation"][2:] 
        
        # بازسازی لیست پیام‌ها
        messages_list = [{"role": "system", "content": system_prompt_to_use}]
        messages_list.extend(session.get("conversation", []))
        messages_list.append({"role": "user", "content": user_message})


    # --- ارسال به OpenRouter (با مکانیزم Key Rotation) ---
    max_attempts = len(OPENROUTER_KEYS)
    ai_message = None

    for attempt in range(max_attempts):
        key_name, current_api_key = get_openrouter_key(initial_attempt=(attempt==0))
        
        if not current_api_key:
            ai_message = "❌ متأسفم، تمام کلیدهای API موقتاً مسدود شده‌اند. لطفاً کمی بعد دوباره امتحان کنید."
            break # اگر کلید فعال نیست، تلاش را متوقف کن

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {current_api_key}"
        }

        data = {
            "model": CHAT_MODEL_NAME,
            "messages": messages_list,
            "max_tokens": max_tokens # 300 توکن
        }

        try:
            response = requests.post(OPENROUTER_URL, json=data, headers=headers, timeout=10)
            response.raise_for_status()
            res_json = response.json()
            ai_message = res_json["choices"][0]["message"]["content"]
            
            # موفقیت: از حلقه خارج شو
            break 
            
        except requests.exceptions.RequestException as e:
            status_code = getattr(e.response, 'status_code', 500)
            print(f"API Request Error (Key: {key_name}): {e}. Status: {status_code}")
            
            # مدیریت خطاهای اتمام توکن یا نامعتبر (402, 401)
            if status_code in [402, 401]:
                handle_key_failure(key_name, status_code) 
                if attempt == max_attempts - 1:
                    # آخرین تلاش هم شکست خورد
                    ai_message = "❌ خطایی در سیستم رخ داد. سرور در حال به‌روزرسانی است، لطفاً کمی بعد دوباره امتحان کنید."
                    break
                continue # رفتن به کلید بعدی
            else: 
                # خطای دیگر (مانند 500)
                ai_message = "⚠️ متأسفم، مشکلی در اتصال به سرور پیش آمد. لطفاً دوباره امتحان کنید."
                break
                
        except Exception as e:
            print(f"General Error: {e}")
            ai_message = "⚠️ مشکلی پیش اومد!"
            break
    
    # --- ذخیره‌سازی و پاسخ نهایی ---
    if ai_message:
        ai_message = fix_rtl_ltr(ai_message)
    else:
        # اگر به هر دلیلی ai_message در حلقه بالا مقداردهی نشد
        ai_message = "❌ خطایی در سیستم رخ داد. سرور در حال به‌روزرسانی است، لطفاً کمی بعد دوباره امتحان کنید."

    # اگر پیام موفقیت آمیز باشد، آن را به تاریخچه اضافه کن
    if not ai_message.startswith(("❌", "⚠️", "⛔")):
        session["conversation"].append({"role": "user", "content": user_message})
        session["conversation"].append({"role": "assistant", "content": ai_message})

        if user and session.get('user_id'):
            save_conversation(user_identifier, session['current_chat_id'], session["conversation"], user_message)
        
        if len(session["conversation"]) > 50:
            session["conversation"] = session["conversation"][-50:]

    return jsonify({"reply": ai_message})


@app.route("/chat/history/<chat_id>", methods=["GET"])
def get_chat_history(chat_id):
    """بارگذاری پیام‌های یک گفتگوی خاص از دیتابیس."""
    user_identifier = get_user_identifier(session)
    user = get_user_by_identifier(user_identifier) if user_identifier else None

    if not user:
        return jsonify({"status": "error", "message": "لطفاً ابتدا وارد حساب کاربری خود شوید."}), 403

    chat_entry = Conversation.query.filter_by(id=chat_id, user_id=user.id).first()

    if not chat_entry:
        return jsonify({"status": "error", "message": "گفتگوی مورد نظر یافت نشد."}), 404

    try:
        messages = json.loads(chat_entry.messages_json)
        # حذف پیام سیستم برای نمایش به کاربر
        if messages and messages[0]['role'] == 'system':
            messages = messages[1:]
            
        session['current_chat_id'] = chat_id
        session['conversation'] = messages

        return jsonify({
            "status": "success", 
            "history": messages,
            "title": chat_entry.title
        })

    except Exception:
        return jsonify({"status": "error", "message": "خطا در بارگذاری تاریخچه گفتگو."}), 500

@app.route("/chat/new", methods=["POST"])
def new_chat():
    """شروع یک گفتگوی جدید."""
    session.pop('current_chat_id', None)
    session["conversation"] = []
    
    # اگر کاربر وارد شده باشد، یک UUID جدید برای چت جدید تولید می‌شود.
    if session.get('user_id'):
        new_id = str(uuid.uuid4())
        session['current_chat_id'] = new_id
        return jsonify({"status": "success", "message": "گفتگوی جدید آغاز شد.", "new_chat_id": new_id})

    return jsonify({"status": "success", "message": "گفتگوی جدید آغاز شد."})

@app.route("/chat/delete/<chat_id>", methods=["DELETE"])
def delete_chat(chat_id):
    """حذف یک گفتگوی خاص از دیتابیس."""
    user_identifier = get_user_identifier(session)
    user = get_user_by_identifier(user_identifier) if user_identifier else None

    if not user:
        return jsonify({"status": "error", "message": "لطفاً ابتدا وارد حساب کاربری خود شوید."}), 403

    chat_entry = Conversation.query.filter_by(id=chat_id, user_id=user.id).first()

    if not chat_entry:
        return jsonify({"status": "error", "message": "گفتگوی مورد نظر یافت نشد."}), 404

    try:
        if session.get('current_chat_id') == chat_id:
            session.pop('current_chat_id', None)
            session["conversation"] = []
            
        db.session.delete(chat_entry)
        db.session.commit()
        return jsonify({"status": "success", "message": "گفتگو با موفقیت حذف شد."})
    except Exception as e:
        db.session.rollback()
        print(f"Error deleting chat: {e}")
        return jsonify({"status": "error", "message": "خطا در حذف گفتگو."}), 500

# =========================================================
# 🖼️ مسیر تولید تصویر
# =========================================================

@app.route("/generate_image", methods=["POST"])
def generate_image():
    user_prompt = request.json.get("prompt", "").strip()
    user_identifier = get_user_identifier(session)
    user = get_user_by_identifier(user_identifier) if user_identifier else None
    
    if not user:
        return jsonify({"status": "error", "message": "لطفاً ابتدا وارد حساب کاربری خود شوید تا بتوانید تصویر تولید کنید."}), 403

    if user.is_banned:
        return jsonify({"reply": "⛔ متأسفم، حساب کاربری شما توسط مدیر سیستم مسدود شده است."}), 403

    if not user_prompt:
        return jsonify({"status": "error", "message": "لطفاً یک توضیحات برای تصویر وارد کنید."}), 400

    # 1. بررسی و کسر بودجه تصویر
    is_allowed, result = check_and_deduct_score(user_identifier, 'image')
    if not is_allowed:
        return jsonify({"status": "error", "message": result}), 402

    # 2. ترجمه پرامپت
    english_prompt = translate_prompt_to_english(user_prompt)
    
    # 3. تولید تصویر
    file_name = generate_and_crop_image(english_prompt)

    if file_name == "TIMEOUT_100_SEC":
        # اگر زمان خطا داد، امتیاز را برگردان
        user_usage = UserUsage.query.filter_by(user_id=user.id).first()
        if user_usage:
            image_cost = SCORE_QUOTA_CONFIG['COSTS']['image']
            user_usage.image_budget += image_cost
            db.session.commit()
        
        return jsonify({"status": "error", "message": "⚠️ زمان تولید تصویر به پایان رسید (۱۰۰ ثانیه). متأسفانه تولید تصویر با شکست مواجه شد و امتیاز شما بازگردانده شد. لطفاً دوباره امتحان کنید."}), 500

    if not file_name:
        return jsonify({"status": "error", "message": "❌ خطایی در تولید تصویر رخ داد. لطفاً دوباره امتحان کنید."}), 500

    # 4. نمایش تصویر
    image_url = url_for('static', filename=f'temp_images/{file_name}', _external=True)

    return jsonify({"status": "success", "image_url": image_url})

# =========================================================
# 🏠 مسیرهای نمایشی
# =========================================================

@app.route("/")
def index():
    return render_template("index.html", logged_in=session.get('user_id') is not None)

@app.route("/chat_ui")
def chat_ui():
    user_id = session.get('user_id')
    user = get_user_by_id(user_id) if user_id else None
    
    # اگر کاربر مهمان است
    if not user:
        # اگر سشن برای مهمان شروع نشده، شروع کن
        if 'guest_chat_count' not in session:
            session['guest_chat_count'] = 0
            session['guest_last_date'] = datetime.utcnow().date().isoformat()
        
        guest_count = session['guest_chat_count']
        
        # اطلاعات سهمیه مهمان
        quota_info = {
            'remaining': GUEST_CHAT_LIMIT - guest_count,
            'limit': GUEST_CHAT_LIMIT,
            'is_premium': False
        }
    else:
        # اگر کاربر لاگین کرده است
        is_premium = user.is_premium
        today_date = datetime.utcnow().date()
        level = 'premium' if is_premium else 'free'
        daily_limits = SCORE_QUOTA_CONFIG['DAILY_BUDGET'][level]
        chat_cost = SCORE_QUOTA_CONFIG['COSTS']['chat']
        
        usage = user.usage
        
        chat_remaining = 0
        if usage and usage.date == today_date and usage.level_check == level:
             chat_remaining = usage.chat_budget // chat_cost
        else:
            chat_remaining = daily_limits['chat'] // chat_cost

        quota_info = {
            'remaining': chat_remaining,
            'limit': daily_limits['chat'] // chat_cost,
            'is_premium': is_premium
        }

    return render_template("chat.html", logged_in=session.get('user_id') is not None, quota_info=quota_info)


@app.route("/image_ui")
def image_ui():
    user_id = session.get('user_id')
    user = get_user_by_id(user_id) if user_id else None

    # اگر کاربر وارد نشده باشد، به صفحه لاگین هدایت می‌شود
    if not user:
        return redirect(url_for('login'))
    
    # اگر کاربر بن شده باشد
    if user.is_banned:
        return render_template("banned.html", user_identifier=user.email or user.phone)

    today_date = datetime.utcnow().date()
    is_premium = user.is_premium
    level = 'premium' if is_premium else 'free'
    daily_limits = SCORE_QUOTA_CONFIG['DAILY_BUDGET'][level]
    image_cost = SCORE_QUOTA_CONFIG['COSTS']['image']
    
    usage = user.usage
    image_remaining = 0

    if usage and usage.date == today_date and usage.level_check == level:
        image_remaining = usage.image_budget // image_cost
    else:
        image_remaining = daily_limits['image'] // image_cost

    quota_info = {
        'remaining': image_remaining,
        'limit': daily_limits['image'] // image_cost,
        'is_premium': is_premium
    }
    
    # ⚠️ این نام قالب قبلاً به "image_generator.html" خطا داشت که حالا به درستی "image.html" است
    return render_template("image.html", logged_in=True, quota_info=quota_info)


@app.route("/games")
def games():
    return render_template("games.html", logged_in=session.get('user_id') is not None)

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


@app.route("/support")
def support():
    return render_template("support.html", logged_in=session.get('user_id') is not None)

@app.route("/about")
def about():
    return render_template("about.html", logged_in=session.get('user_id') is not None)


if __name__ == "__main__":
    with app.app_context():
        # db.drop_all() # برای ریست کردن کامل دیتابیس
        db.create_all()
        cleanup_old_images()
    app.run(debug=True, port=5000)