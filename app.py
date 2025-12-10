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
app.secret_key = "supersecretkey123"

# 👑 شماره تلفن ادمین برای دسترسی مستقیم
ADMIN_PHONE_NUMBER = '09962935294'

# 🔔 شماره تلفن برای دریافت هشدار اتمام توکن
TOKEN_ALERT_PHONE_NUMBER = '09023287024'

# 🛍️ تنظیمات ورود با بازار (Bazaar Login Config)
BAZAAR_CLIENT_ID = "8Fk3ykSaqDNnBs54"
BAZAAR_CLIENT_SECRET = "GQfRhVPuPyvOJ0L86BTpq2lgH6wnPojq"

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
    if not TOKEN_ALERT_PHONE_NUMBER:
        print("Warning: TOKEN_ALERT_PHONE_NUMBER not set.")
        return

    try:
        params = {
            'sender': KAVENEGAR_SENDER,
            'receptor': TOKEN_ALERT_PHONE_NUMBER,
            'message': f'⚠️ اخطار! کلید OpenRouter ({key_name}) با خطا مواجه شد ({reason}). موقتا مسدود شد.',
        }
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

SYSTEM_PROMPT = """
تو یک چت‌بات مفید هستی. پاسخ‌ها را به زبان فارسی و روان بده.
- برای سوالات سازنده: تیم NOCTOVEX به رهبری مهراب عزیزی
- پاسخ‌ها باید **فوق‌العاده کامل، مفصل و دقیق** باشند و در سقف نهایی **۴۰۰۰ توکن** به پایان برسند. (به هیچ عنوان پاسخ را از وسط جمله قطع نکن).
"""
# 💡 ثابت‌های جدید برای حالت پاسخ بلند
LONG_RESPONSE_TOKEN_THRESHOLD = 300 # آستانه توکن ورودی برای پاسخ بلند
# 🚨 تغییر: کاهش سقف توکن خروجی پاسخ بلند به ۷۵۰ (طبق درخواست قبلی)
LONG_RESPONSE_MAX_COMPLETION_TOKENS = 750 
LONG_RESPONSE_TOTAL_TOKEN_LIMIT = 4096 


# 🚨 تغییر: آستانه جدید برای محدودیت‌های ورودی
PREMIUM_ONLY_TOKEN_THRESHOLD = 2000
NORMAL_GUEST_INPUT_LIMIT = 400 # 🚨 تغییر جدید: آستانه حداکثر توکن ورودی برای کاربران عادی/مهمان

TOTAL_TOKEN_LIMIT = 4096 
INPUT_TOKEN_LIMIT = 4096 
# 🚨 تغییر: سقف توکن خروجی برای حالت عادی به ۷۵۰ (طبق درخواست قبلی)
MAX_COMPLETION_TOKENS = 750 

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

    date = db.Column(db.Date, default=datetime.utcnow().date)

    chat_budget = db.Column(db.Integer, default=50)
    image_budget = db.Column(db.Integer, default=60)
    long_response_budget = db.Column(db.Integer, default=5) # 💡 فیلد جدید
    level_check = db.Column(db.String(10), nullable=True)


class Conversation(db.Model):
    __tablename__ = 'conversations'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False)
    title = db.Column(db.String(100), nullable=False, default="گفتگوی جدید...")

    last_update = db.Column(db.Float, default=time.time)

    messages_json = db.Column(db.Text, nullable=False)


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
    return session.get('user_email') or session.get('user_phone')

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

    try:
        db.session.commit()
        return user
    except sqlalchemy.exc.IntegrityError as e:
        db.session.rollback()
        print(f"Database Integrity Error during registration: {e}")
        return None


def check_and_deduct_score(user_identifier, usage_type):
    """
    بررسی بودجه امتیاز روزانه, کسر هزینه و ذخیره.
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
        user_identifier = get_user_identifier(session)
        user = get_user_by_identifier(identifier=user_identifier)

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
# 💬 مسیر چت و بقیه مسیرها (با اعمال محدودیت و چرخش کلید)
# =========================================================
@app.route("/chat", methods=["POST"])
def chat():
    user_message = request.json.get("message", "")
    lower_msg = user_message.lower()

    if not user_message.strip():
        return jsonify({"reply": "لطفاً پیامی ارسال کنید."})

    user_identifier = get_user_identifier(session)
    user = get_user_by_identifier(user_identifier)

    # --- تعیین نوع استفاده و بررسی توکن ---
    # توکن‌های پیام کاربر را محاسبه کن
    user_message_tokens = count_tokens([{"role": "user", "content": user_message}])
    
    # 💡 منطق جدید برای پاسخ بلند
    is_long_response = False
    usage_type = 'chat'

    if user and user_identifier:
        
        # 🚨 تغییر ۱: محدودیت ۴۰۰ توکن برای کاربران عادی (غیر پرمیوم)
        if not user.is_premium:
            if user_message_tokens > NORMAL_GUEST_INPUT_LIMIT:
                 return jsonify({
                    "reply": f"⛔ متأسفم، پرامت شما ({user_message_tokens} توکن) بیشتر از توکن حالت حساب عادی ({NORMAL_GUEST_INPUT_LIMIT} توکن) هست. لطفاً حساب خود را **پرمیوم** کنید یا پیام خود را کوتاه کنید."
                })
            
        if user_message_tokens >= LONG_RESPONSE_TOKEN_THRESHOLD:
            # کاربر وارد شده، پیامش هم بلند است -> فعال‌سازی حالت پاسخ بلند
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

        # 🚨 تغییر ۳: محدودیت ۴۰۰ توکن برای کاربران مهمان
        if user_message_tokens > NORMAL_GUEST_INPUT_LIMIT: 
             return jsonify({
                "reply": f"⛔ متأسفم، پرامت شما ({user_message_tokens} توکن) بیشتر از توکن حالت حساب مهمان ({NORMAL_GUEST_INPUT_LIMIT} توکن) هست. لطفاً حساب خود را **پرمیوم** کنید یا وارد شوید و پیام خود را کوتاه کنید."
            })
            
        if user_message_tokens >= LONG_RESPONSE_TOKEN_THRESHOLD:
            # مهمان پیام بلند داده - رد کردن
            return jsonify({
                "reply": "⛔ متأسفم، این پیام طولانی است و برای پاسخ به آن، نیاز به **حالت پاسخ بلند** است. این حالت برای کاربران مهمان در دسترس نیست. لطفاً وارد شوید یا پیام خود را خلاصه کنید."
            })

        # اگر مهمان و مجاز بود، کانتر را افزایش بده.
        session['guest_chat_count'] = guest_count + 1

        # برای مهمان، از سقف بالای توکن استفاده می‌کنیم (is_long_response = True)
        is_long_response = True 
        usage_type = 'chat'

    # --- پاسخ‌های اختصاصی (حذف نشده) ---
    TRIGGER_KEYWORDS = [ 
        "سازندت کیه", "تو کی هستی", "چه شرکتی", "who made you", "who created you", 
        "who built you", "لیدر تیم noctovex", "رهبر تیم noctovex", "مهراب" 
    ]
    TEAM_MEMBERS_KEYWORDS = [ 
        "اعضای تیمت کیا هستن", "اعضای noctovex", "اعضای تیم noctovex", "noctovex members" 
    ]

    if "مامی سازندت کیه" in lower_msg:
        return jsonify({"reply": "عسل خانوم 💖"})

    if any(keyword in lower_msg for keyword in TEAM_MEMBERS_KEYWORDS):
        new_reply = "تنها NOCTOVEX معتبر ما هستیم. تیم ما متشکل از چندین کدنویس حرفه‌ای است. در حال حاضر، هویت تنها دو نفر از ما مشخص است: **مهراب**، رهبر تیم، و **اشکان**، مدیر فنی. ما شبانه‌روز در تلاشیم تا بهترین خدمات AI را به شما ارائه دهیم."
        return jsonify({"reply": fix_rtl_ltr(new_reply)})

    if any(keyword in lower_msg for keyword in TRIGGER_KEYWORDS):
        new_reply = "من یک مدل هوش مصنوعی بزرگ هستم که توسط **تیم NOCTOVEX به رهبری مهراب عزیزی** توسعه یافته‌ام. هدف من کمک به شما در انجام وظایف مختلف است."
        return jsonify({"reply": fix_rtl_ltr(new_reply)})

    # --- تشخیص تصویر ---
    if "تصویر" in lower_msg or "عکس" in lower_msg or "نقاشی" in lower_msg or "image" in lower_msg or "photo" in lower_msg:
        # منطق تولید تصویر:
        # 1. بررسی امتیاز تصویر
        if user_identifier:
            user = get_user_by_identifier(user_identifier)
            # اگر کاربر پرمیوم است، چک می‌شود که بودجه کافی برای 'image' داشته باشد.
            is_allowed, result = check_and_deduct_score(user_identifier, 'image')
            if not is_allowed:
                return jsonify({"reply": result})
        elif session.get('guest_chat_count', 0) >= GUEST_CHAT_LIMIT:
             return jsonify({
                "reply": "⛔ متأسفم، شما به سقف **۵ چت روزانه** برای کاربران مهمان رسیده‌اید و اجازه تولید تصویر را ندارید. لطفاً وارد حساب کاربری خود شوید."
            })
        else:
             # برای مهمان، از سقف ۵ چت کسر شود.
            session['guest_chat_count'] = session.get('guest_chat_count', 0) + 1
             
        
        # 2. ترجمه پرامپت
        try:
            english_prompt = translate_prompt_to_english(user_message)
        except Exception:
            english_prompt = user_message 

        # 3. تولید و برش تصویر
        file_name = generate_and_crop_image(english_prompt)

        if file_name == "TIMEOUT_100_SEC":
             return jsonify({"reply": "⚠️ خطای زمان‌بندی: تولید تصویر بیش از ۱۰۰ ثانیه طول کشید. لطفاً پرامپت خود را ساده‌تر کنید یا مجدداً تلاش نمایید."})
        elif file_name:
            image_url = url_for('static', filename=f'temp_images/{file_name}', _external=True)
            return jsonify({
                "reply": f"تصویر شما با پرامپت: **{user_message}**\n[مشاهده تصویر]({image_url})",
                "image_url": image_url
            })
        else:
            return jsonify({"reply": "⛔ متأسفم، در حال حاضر امکان تولید تصویر وجود ندارد. لطفاً بعداً دوباره امتحان کنید."})


    # --- مدیریت تاریخچه و توکن‌ها ---
    chat_id = request.json.get("chat_id")
    messages = request.json.get("messages", [])
    
    # 1. حذف پیام‌های قدیمی برای حفظ سقف توکن
    current_token_count = count_tokens(messages)
    
    # تعیین سقف توکن بر اساس حالت پاسخ بلند
    # -----------------------------------------------------------------------
    if is_long_response:
        current_total_token_limit = LONG_RESPONSE_TOTAL_TOKEN_LIMIT
        current_max_completion_tokens = LONG_RESPONSE_MAX_COMPLETION_TOKENS # ۷۵۰
    else:
        current_total_token_limit = TOTAL_TOKEN_LIMIT
        current_max_completion_tokens = MAX_COMPLETION_TOKENS # ۷۵۰
        
    system_prompt_to_use = SYSTEM_PROMPT 
    
    # 💡 اعمال دستور کوتاه کردن پاسخ برای مدل (برای رعایت سقف ۷۵۰)
    if current_max_completion_tokens <= 750:
         # تزریق دستور محدودیت به سیستم پرامپت برای تضمین کامل بودن در عین کوتاهی
         system_prompt_to_use = SYSTEM_PROMPT.replace(
             "در سقف نهایی **۴۰۰۰ توکن** به پایان برسند.", 
             "در سقف نهایی **۷۵۰ توکن** به پایان برسند. پاسخ‌ها باید کامل، دقیق و روان باشند، اما اگر جواب خیلی طولانی است، آن را با حفظ اطلاعات کلیدی، کوتاه کن تا از ۷۵۰ توکن تجاوز نکند."
         )
         
    # -----------------------------------------------------------------------
    
    # محاسبه حداکثر توکن ورودی مجاز (کل سقف منهای توکن خروجی مورد نیاز)
    max_input_tokens_allowed = current_total_token_limit - current_max_completion_tokens

    # حذف پیام‌های قدیمی تا توکن‌ها در محدوده مجاز قرار گیرند
    while current_token_count > max_input_tokens_allowed and len(messages) > 1:
        # حذف دومین پیام (قدیمی‌ترین پیام کاربر یا پاسخ مدل)، اولین پیام (سیستم) حذف نمی‌شود.
        messages.pop(1) 
        current_token_count = count_tokens(messages)

    # 2. اضافه کردن پیام جدید کاربر
    messages.append({"role": "user", "content": user_message})

    # 3. ایجاد لیست نهایی برای ارسال به API
    messages_list = [{"role": "system", "content": system_prompt_to_use}]
    messages_list.extend(messages)
    
    # 4. انتخاب کلید فعال و ارسال درخواست
    key_name, current_api_key = get_openrouter_key()

    if not current_api_key:
        return jsonify({"reply": "⛔ متأسفم، در حال حاضر تمام کلیدهای API موقتاً نامعتبر یا فاقد اعتبار هستند. لطفاً دقایقی دیگر دوباره امتحان کنید."})


    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {current_api_key}",
        "HTTP-Referer": "YOUR_SITE_URL", # جایگزین کنید
        "X-Title": "YOUR_APP_NAME" # جایگزین کنید
    }

    data = {
        "model": CHAT_MODEL_NAME,
        "messages": messages_list,
        "max_tokens": current_max_completion_tokens, # اعمال سقف خروجی (۷۵۰)
        "temperature": 0.7
    }

    response_text = ""
    status_code = 200

    try:
        response = requests.post(OPENROUTER_URL, json=data, headers=headers, timeout=60)
        status_code = response.status_code
        response.raise_for_status() # برای خطاهای 4xx و 5xx استثنا ایجاد می‌کند
        
        res_json = response.json()
        response_text = res_json["choices"][0]["message"]["content"]
        
        # 5. اضافه کردن پاسخ مدل به تاریخچه
        messages.append({"role": "assistant", "content": response_text})

        # 6. ذخیره گفتگو
        if user_identifier and chat_id:
            save_conversation(user_identifier, chat_id, messages, user_message)

    except requests.exceptions.RequestException as e:
        status_code = getattr(e.response, 'status_code', 500)
        print(f"Chat API Error (Key: {key_name}): {e}. Status: {status_code}")

        if status_code in [402, 401]:
            handle_key_failure(key_name, status_code) 
            response_text = "⛔ متأسفم، اعتبار یکی از کلیدهای API به پایان رسیده است. لطفاً دقایقی دیگر دوباره امتحان کنید یا با مدیر تماس بگیرید."
        elif status_code == 429:
             response_text = "⚠️ حجم درخواست‌ها زیاد است. لطفاً کمی صبر کنید و دوباره تلاش کنید."
        elif status_code == 500:
             response_text = "❌ خطای سرور داخلی در پردازش مدل. لطفاً مجدداً تلاش کنید."
        else:
            response_text = f"خطای API ناشناخته ({status_code})."
    
    except Exception as e:
        print(f"General Chat Error: {e}")
        response_text = "خطای ناشناخته در برنامه."

    return jsonify({"reply": fix_rtl_ltr(response_text)})


# =========================================================
# 🖼️ مسیرهای تولید تصویر (حذف نشده)
# =========================================================
@app.route("/generate_image", methods=["POST"])
def generate_image():
    user_message = request.json.get("prompt", "").strip()
    if not user_message:
        return jsonify({"status": "error", "message": "لطفاً پرامپت تصویر را وارد کنید."}), 400

    user_identifier = get_user_identifier(session)

    # 1. بررسی امتیاز و سهمیه
    if user_identifier:
        is_allowed, result = check_and_deduct_score(user_identifier, 'image')
        if not is_allowed:
            return jsonify({"status": "error", "message": result}), 403
    else:
        # محدودیت چت مهمان را برای تولید تصویر هم اعمال می‌کنیم.
        today_date_str = datetime.utcnow().date().isoformat()
        if session.get('guest_last_date') != today_date_str:
            session['guest_chat_count'] = 0
            session['guest_last_date'] = today_date_str
            
        guest_count = session.get('guest_chat_count', 0)
        
        # مهمان اجازه تولید تصویر ندارد مگر سقف چت پر شود
        if guest_count >= GUEST_CHAT_LIMIT:
             return jsonify({
                "status": "error", 
                "message": "⛔ متأسفم، شما به سقف **۵ چت روزانه** برای کاربران مهمان رسیده‌اید و اجازه تولید تصویر را ندارید. لطفاً وارد حساب کاربری خود شوید."
            }), 403
             
        # اگر مهمان باشد و مجاز، یک واحد از سهمیه چت او کسر می‌کنیم.
        session['guest_chat_count'] = guest_count + 1


    # 2. ترجمه پرامپت
    try:
        english_prompt = translate_prompt_to_english(user_message)
    except Exception:
        english_prompt = user_message # در صورت خطا، از پرامپت فارسی استفاده می‌کنیم

    # 3. تولید و برش تصویر
    file_name = generate_and_crop_image(english_prompt)

    if file_name == "TIMEOUT_100_SEC":
        return jsonify({
             "status": "error", 
             "message": "⚠️ خطای زمان‌بندی: تولید تصویر بیش از ۱۰۰ ثانیه طول کشید. لطفاً پرامپت خود را ساده‌تر کنید یا مجدداً تلاش نمایید."
         }), 504
    elif file_name:
        image_url = url_for('static', filename=f'temp_images/{file_name}', _external=True)
        return jsonify({
            "status": "success",
            "image_url": image_url,
            "prompt": user_message
        })
    else:
        return jsonify({"status": "error", "message": "⛔ متأسفم، در حال حاضر امکان تولید تصویر وجود ندارد. لطفاً بعداً دوباره امتحان کنید."}), 500

# =========================================================
# 🔐 مسیرهای مربوط به ورود با بازار (Bazaar Login)
# =========================================================
@app.route("/bazaar_login", methods=["GET"])
def bazaar_login():
    base_url = "https://public-auth.tsetmc.com/oauth2/auth"
    redirect_uri = url_for('bazaar_callback', _external=True)
    state = str(uuid.uuid4())
    session['state'] = state

    full_url = (
        f"{base_url}?"
        f"response_type=code&"
        f"client_id={BAZAAR_CLIENT_ID}&"
        f"redirect_uri={quote(redirect_uri)}&"
        f"scope=basic profile&"
        f"state={state}"
    )

    return redirect(full_url)

@app.route("/bazaar_callback", methods=["GET"])
def bazaar_callback():
    code = request.args.get('code')
    state_received = request.args.get('state')
    state_expected = session.get('state')

    if state_received != state_expected:
        return "Authentication Failed: State mismatch.", 403

    if not code:
        return "Authentication Failed: No code received.", 400

    try:
        # 1. تبادل کد با توکن
        token_url = "https://public-auth.tsetmc.com/oauth2/token"
        redirect_uri = url_for('bazaar_callback', _external=True)

        token_data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": BAZAAR_CLIENT_ID,
            "client_secret": BAZAAR_CLIENT_SECRET
        }

        token_response = requests.post(token_url, data=token_data, timeout=10)
        token_response.raise_for_status()
        token_info = token_response.json()
        access_token = token_info.get("access_token")

        if not access_token:
            return "Authentication Failed: Could not get access token.", 500

        # 2. دریافت اطلاعات کاربر
        user_info_url = "https://public-auth.tsetmc.com/oauth2/userinfo"
        user_info_headers = {
            "Authorization": f"Bearer {access_token}"
        }

        user_info_response = requests.get(user_info_url, headers=user_info_headers, timeout=10)
        user_info_response.raise_for_status()
        user_info = user_info_response.json()

        # 3. استخراج شناسه کاربری (اول شماره تلفن، بعد account_id)
        # ⚠️ توجه: شماره تلفن ممکن است null باشد
        bazaar_identifier = user_info.get('phone_number') or user_info.get('account_id')
        
        if not bazaar_identifier:
            return "Authentication Failed: Could not find any identifier (phone or account_id) in User Info response.", 500

        # حذف state از سشن
        if 'state' in session:
            session.pop('state') 
            
        # 4. ثبت یا بازیابی کاربر بر اساس شماره تلفن/شناسه
        # 🔴 استفاده از شناسه پیدا شده (شماره تلفن یا account_id)
        bazaar_user_id = f"bazaar_{bazaar_identifier}" 
        
        user = register_user_if_new(bazaar_user_id)
        
        if not user:
             return "Internal Error: Could not create user from Bazaar account", 500

        session.clear()
        session['user_id'] = user.id
        session['user_identifier'] = bazaar_user_id
        session['is_admin'] = user.is_admin

        return redirect(url_for('account'))

    except requests.exceptions.RequestException as e:
        error_message = f"Bazaar API Error: {e}"
        print(error_message)
        return f"Authentication Failed (API): {error_message}", 500
    except Exception as e:
        error_message = f"General Authentication Error: {e}"
        print(error_message)
        return f"Authentication Failed (General): {error_message}", 500

# =========================================================
# ⚙️ مسیرهای مدیریت حساب (Account)
# =========================================================
@app.route("/account")
def account():
    user_identifier = get_user_identifier(session)
    if not user_identifier:
        return redirect(url_for('login'))
    
    user = get_user_by_identifier(user_identifier)
    
    if not user:
        # اگر کاربر در دیتابیس نبود، لاگ‌اوت
        session.clear()
        return redirect(url_for('login'))
        
    # اطلاعات استفاده روزانه
    usage = user.usage
    today_date = datetime.utcnow().date()
    
    # اطمینان از به‌روزرسانی بودجه روزانه (اگر تاریخ یا سطح تغییر کرده)
    is_premium = user.is_premium
    level = 'premium' if is_premium else 'free'
    daily_limits = SCORE_QUOTA_CONFIG['DAILY_BUDGET'][level]

    if not usage or usage.date != today_date or usage.level_check != level:
        # این منطق به‌طور خودکار در check_and_deduct_score هم اجرا می‌شود، اما اینجا برای نمایش به‌روز نیاز است.
        chat_budget = daily_limits['chat']
        image_budget = daily_limits['image']
        long_response_budget = daily_limits.get('long_response', 0)
    else:
        chat_budget = usage.chat_budget
        image_budget = usage.image_budget
        long_response_budget = usage.long_response_budget


    # محاسبه تعداد استفاده‌های باقی‌مانده
    remaining_chats = chat_budget // SCORE_QUOTA_CONFIG['COSTS']['chat']
    remaining_images = image_budget // SCORE_QUOTA_CONFIG['COSTS']['image']
    remaining_long_responses = long_response_budget // SCORE_QUOTA_CONFIG['COSTS']['long_response']

    # بازیابی تاریخچه‌ی گفتگوها
    conversations = Conversation.query.filter_by(user_id=user.id).order_by(Conversation.last_update.desc()).limit(10).all()
    
    chat_history = [
        {'id': conv.id, 'title': conv.title, 'last_update': conv.last_update} 
        for conv in conversations
    ]

    context = {
        'user_identifier': user_identifier,
        'user_email': user.email,
        'user_phone': user.phone,
        'is_premium': user.is_premium,
        'is_admin': user.is_admin,
        'score': user.score,
        'is_banned': user.is_banned,
        'remaining_chats': remaining_chats,
        'remaining_images': remaining_images,
        'remaining_long_responses': remaining_long_responses,
        'daily_chat_limit': daily_limits['chat'] // SCORE_QUOTA_CONFIG['COSTS']['chat'],
        'daily_image_limit': daily_limits['image'] // SCORE_QUOTA_CONFIG['COSTS']['image'],
        'daily_long_response_limit': daily_limits.get('long_response', 0) // SCORE_QUOTA_CONFIG['COSTS']['long_response'],
        'chat_history': chat_history,
        'needs_profile_info': session.pop('needs_profile_info', False)
    }

    return render_template("account.html", **context)

@app.route("/login")
def login():
    # 🚨 اصلاح خطای TemplateNotFound: به جای login.html که وجود ندارد، از account_login.html استفاده می‌شود.
    return render_template("account_login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route("/conversation/<chat_id>", methods=["GET"])
def get_conversation(chat_id):
    user_identifier = get_user_identifier(session)
    user = get_user_by_identifier(user_identifier)
    
    if not user:
        return jsonify({"status": "error", "message": "لطفاً ابتدا وارد حساب کاربری خود شوید."}), 401
    
    conversation = Conversation.query.filter_by(id=chat_id, user_id=user.id).first()
    
    if not conversation:
        return jsonify({"status": "error", "message": "گفتگو یافت نشد."}), 404
        
    try:
        messages = json.loads(conversation.messages_json)
    except json.JSONDecodeError:
        messages = []
        
    return jsonify({
        "status": "success",
        "title": conversation.title,
        "messages": messages
    })

@app.route("/delete_conversation/<chat_id>", methods=["POST"])
def delete_conversation(chat_id):
    user_identifier = get_user_identifier(session)
    user = get_user_by_identifier(user_identifier)
    
    if not user:
        return jsonify({"status": "error", "message": "لطفاً ابتدا وارد حساب کاربری خود شوید."}), 401
        
    conversation = Conversation.query.filter_by(id=chat_id, user_id=user.id).first()
    
    if not conversation:
        return jsonify({"status": "error", "message": "گفتگو یافت نشد."}), 404
        
    try:
        db.session.delete(conversation)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Error deleting conversation: {e}")
        return jsonify({"status": "error", "message": "خطا در حذف گفتگو از دیتابیس."}), 500
        
    return jsonify({"status": "success", "message": "گفتگو با موفقیت حذف شد."})


@app.route("/")
def index():
    user_identifier = get_user_identifier(session)
    if user_identifier:
        return redirect(url_for('account'))
    return redirect(url_for('login'))


# =========================================================
# ▶️ اجرای برنامه
# =========================================================

if __name__ == "__main__":
    with app.app_context():
        # db.drop_all() # استفاده از این خط برای ریست کامل دیتابیس است.
        db.create_all()
        # اجرای وظیفه‌ی تمیزکاری تصاویر در پس‌زمینه (اگر سرور از threading پشتیبانی کند)
        # cleanup_images() 
    app.run(debug=True, host='0.0.0.0', port=os.environ.get("PORT", 5000))