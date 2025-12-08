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
- **فقط مهم‌ترین و اصلی‌ترین نکات** موضوع را ذکر کن.
- پاسخ‌ها باید **کامل، روان و دقیق** باشند و در سقف نهایی **۴۰۰ توکن** به پایان برسند. (به هیچ عنوان پاسخ را از وسط جمله قطع نکن).
"""
# 💡 ثابت‌های جدید برای حالت پاسخ بلند
LONG_RESPONSE_TOKEN_THRESHOLD = 300 # آستانه توکن ورودی برای پاسخ بلند
LONG_RESPONSE_MAX_COMPLETION_TOKENS = 3000 # حداکثر توکن خروجی برای پاسخ بلند
LONG_RESPONSE_TOTAL_TOKEN_LIMIT = 3200 # سقف کل توکن (ورودی + خروجی) برای پاسخ بلند


TOTAL_TOKEN_LIMIT = 550
INPUT_TOKEN_LIMIT = 100
MAX_COMPLETION_TOKENS = 400
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
        # 💡 مدیریت کاربران مهمان برای پاسخ بلند
        if user_message_tokens >= LONG_RESPONSE_TOKEN_THRESHOLD:
            return jsonify({
                "reply": "⛔ متأسفم، این پیام طولانی است و برای پاسخ به آن، نیاز به **حالت پاسخ بلند** است. این حالت برای کاربران مهمان در دسترس نیست. لطفاً وارد شوید یا پیام خود را خلاصه کنید."
            })
        
        # اگر مهمان و پیام کوتاه بود، با سهمیه پیش‌فرض چت ادامه بده (و کسر امتیازی نخواهد بود)


    # --- پاسخ‌های اختصاصی (حذف نشده) ---
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

    
    # 💡 تنظیم سقف توکن و System Prompt بر اساس حالت پاسخ بلند
    if is_long_response:
        current_total_token_limit = LONG_RESPONSE_TOTAL_TOKEN_LIMIT
        current_max_completion_tokens = LONG_RESPONSE_MAX_COMPLETION_TOKENS
        
        # در حالت پاسخ بلند، پیام سیستم را برای پاسخ کامل‌تر تنظیم کن
        system_prompt_to_use = """
        تو یک چت‌بات مفید هستی. پاسخ‌ها را به زبان فارسی و روان بده.
        - برای سوالات سازنده: تیم NOCTOVEX به رهبری مهراب عزیزی
        - پاسخ‌ها باید **فوق‌العاده کامل، مفصل و دقیق** باشند و در سقف نهایی **۳۰۰۰ توکن** به پایان برسند. (به هیچ عنوان پاسخ را از وسط جمله قطع نکن).
        """
        
    else:
        current_total_token_limit = TOTAL_TOKEN_LIMIT
        current_max_completion_tokens = MAX_COMPLETION_TOKENS
        system_prompt_to_use = SYSTEM_PROMPT


    messages_list = [{"role": "system", "content": system_prompt_to_use}]
    messages_list.extend(session.get("conversation", []))
    messages_list.append({"role": "user", "content": user_message})

    # --- فشرده‌سازی تاریخچه و محاسبه توکن ---
    while count_tokens(messages_list) >= current_total_token_limit and len(session["conversation"]) >= 2:
        session["conversation"] = session["conversation"][2:]
        # مجدداً لیست پیام‌ها را با تاریخچه کوتاه‌تر بازسازی کن
        messages_list = [{"role": "system", "content": system_prompt_to_use}]
        messages_list.extend(session.get("conversation", []))
        messages_list.append({"role": "user", "content": user_message})

    prompt_tokens = count_tokens(messages_list)
    remaining_tokens = current_total_token_limit - prompt_tokens
    max_tokens_calculated = max(20, remaining_tokens)
    max_tokens = min(max_tokens_calculated, current_max_completion_tokens)

    if remaining_tokens <= 120 and not is_long_response:
        # اگر پاسخ بلند نیست و توکن کم است، هشدار بده
        messages_list.append({
            "role": "system",
            "content": "⚠️ توکن کم باقی مانده است. لطفاً پاسخ را خلاصه، کامل و روان بده، اما هرگز نصفه نباشد."
        })

    # --- مکانیزم چرخش کلید و تلاش مجدد ---
    max_attempts = len(OPENROUTER_KEYS)
    ai_message = None

    for attempt in range(max_attempts):
        key_name, current_api_key = get_openrouter_key(initial_attempt=(attempt==0))
        
        if not current_api_key:
            # اگر هیچ کلید فعالی باقی نماند
            ai_message = "❌ خطایی در سیستم رخ داد. سرور در حال به‌روزرسانی است، لطفاً کمی بعد دوباره امتحان کنید."
            break # خروج از حلقه تلاش

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {current_api_key}"
        }

        data = {
            "model": CHAT_MODEL_NAME,
            "messages": messages_list,
            "max_tokens": max_tokens
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

    # ۱. بررسی وجود کاربر
    if not user:
        return jsonify({"status": "error", "message": "لطفاً ابتدا وارد حساب کاربری خود شوید."}), 403

    # ۲. بررسی بن بودن
    if user.is_banned:
        return jsonify({
            "status": "error",
            "message": "⛔ متأسفم، حساب کاربری شما توسط مدیر سیستم مسدود شده است."
        }), 403

    # ۳. بررسی امتیاز و کسر آن
    is_allowed, result = check_and_deduct_score(user_identifier, 'image')
    if not is_allowed:
        return jsonify({"status": "error", "message": result}), 429

    # ۴. بررسی خالی نبودن متن ورودی
    if not persian_prompt or len(persian_prompt.split()) < 1:
        return jsonify({
            "status": "error",
            "message": "لطفاً موضوع دقیق‌تر تصویر مورد نظرتان را به فارسی بنویسید."
        }), 400

    try:
        # ۵. ترجمه پرامپت به انگلیسی (با استفاده از چرخش کلیدهای OpenRouter)
        english_prompt = translate_prompt_to_english(persian_prompt)

        # ۶. تولید لینک هوشمند (بدون دانلود توسط سرور)
        # اضافه کردن Seed تصادفی برای جلوگیری از تکرار و کش شدن
        seed = random.randint(1, 1000000)
        quality = "%20".join(IMAGE_QUALITY_PARAMS)
        
        # لینک مستقیم به Pollinations
        direct_image_url = f"{POLLINATIONS_URL}{english_prompt.replace(' ', '%20')}%20{quality}?nologo=true&seed={seed}"

        # ۷. بازگرداندن لینک به مرورگر کاربر
        return jsonify({
            "status": "success",
            "message": f"تصویر شما با پرامپت '{persian_prompt}' تولید شد. 🖼️",
            "image_url": direct_image_url
        })

    except Exception as e:
        print(f"Image Generator Handler Error: {e}")
        return jsonify({
            "status": "error",
            "message": f"❌ خطای داخلی سرور هنگام پردازش تصویر."
        }), 500


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
        'max_long_responses': daily_limits.get('long_response', 0) // long_response_cost if long_response_cost > 0 else daily_limits.get('long_response', 0), # 💡 اضافه شده
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
# 🛍️ مسیرهای احراز هویت با کافه‌بازار (Bazaar Auth)
# =========================================================

@app.route("/bazaar_login")
def bazaar_login():
    """هدایت کاربر به صفحه لاگین بازار."""
    
    # آدرس بازگشت دقیقاً طبق درخواست شما
    redirect_uri = "https://alie-0die.onrender.com/bazaar_callback"
    
    # 1. انکود کردن آدرس بازگشت (طبق درخواست بازار: escape شده)
    from urllib.parse import quote
    encoded_redirect_uri = quote(redirect_uri, safe='') 
    
    # 2. ایجاد پارامتر امنیتی 'state' برای جلوگیری از حملات CSRF
    state = uuid.uuid4().hex
    session['state'] = state # ذخیره state در سشن برای بررسی در مرحله بعد

    # 3. URL جدید و صحیح برای ورود با بازار (بر اساس مستندات جدید بازار: /user/oauth)
    bazaar_auth_url = (
        f"https://cafebazaar.ir/user/oauth?"
        f"redirect_url={encoded_redirect_uri}"
        f"&client_id={BAZAAR_CLIENT_ID}" # استفاده از متغیر سراسری
        f"&state={state}"
    )
    
    return redirect(bazaar_auth_url)

# =========================================================
# ✅ تابع اصلاح شده برای دریافت کد و تبادل توکن (Callback URL)
# =========================================================

@app.route("/bazaar_callback")
def bazaar_callback():
    # ... کدهای ابتدایی شما ...
    auth_code = request.args.get('code')
    received_state = request.args.get('state')
    expected_state = session.pop('state', None)

    if not auth_code:
        return "Authentication failed: No code received from Bazaar", 400
    if not received_state or received_state != expected_state:
        return "Authentication failed: Invalid state parameter", 400

    redirect_uri = "https://alie-0die.onrender.com/bazaar_callback"
    token_url = "https://cafebazaar.ir/user/oauth/token/" # آدرس صحیح

    data = {
        'grant_type': 'authorization_code',
        'code': auth_code,
        'client_id': BAZAAR_CLIENT_ID,
        'client_secret': BAZAAR_CLIENT_SECRET,
        'redirect_uri': redirect_uri
    }
    
    try:
        response = requests.post(token_url, data=data, timeout=10)
        
        # 🚨 خطایابی جدید: اگر کد HTTP غیر 200 بود، به جای raise_for_status، ابتدا متن را بررسی می‌کنیم.
        if response.status_code != 200:
            # اگر بازار با خطا (مثلاً 400 یا 500) پاسخ دهد
            print(f"Bazaar Token Exchange Failed. HTTP Status: {response.status_code}")
            # چاپ متن پاسخ برای دیدن علت اصلی خطا
            print(f"Bazaar Response Text: {response.text}") 
            # اگر پاسخ متنی بود، آن را نمایش می‌دهیم.
            return f"Error {response.status_code}: {response.text}", response.status_code
        
        # اگر کد HTTP 200 بود، اما JSON نبود، خطای 'Expecting value' می‌دهد.
        # در این حالت، متن پاسخ را برای بررسی چاپ کنید.
        try:
            tokens = response.json()
        except json.JSONDecodeError as e:
            print(f"JSON Decode Error: {e}")
            print(f"Bazaar Non-JSON Response Text: {response.text}")
            return f"Error exchanging token: Invalid response format from Bazaar. Response: {response.text}", 500

        # ... (بقیه منطق پردازش توکن و لاگین)
        access_token = tokens.get('access_token')
        
        # ثبت یا بازیابی کاربر
        bazaar_user_id = f"bazaar_{uuid.uuid4().hex[:8]}" 
        
        user = register_user_if_new(bazaar_user_id)
        
        if not user:
             return "Internal Error: Could not create user from Bazaar account", 500

        session.clear()
        session['user_id'] = user.id
        session['user_identifier'] = bazaar_user_id
        session['is_admin'] = user.is_admin

        return redirect(url_for('account'))

    except requests.exceptions.RequestException as e:
        print(f"Bazaar Token Exchange Network Error: {e}")
        return f"Error exchanging token with Bazaar: Network Error: {str(e)}", 500
    except Exception as e:
        print(f"Bazaar OAuth General Error: {e}")
        return "Internal Server Error during Bazaar Login", 500

# =========================================================
# ▶️ اجرای برنامه
# =========================================================

if __name__ == "__main__":
    if os.environ.get("FLASK_ENV") != "production":
        cleanup_old_images()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)