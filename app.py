import os
from urllib.parse import quote, urlencode
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
from functools import wraps
import json
from flask_sqlalchemy import SQLAlchemy
from datetime import date, datetime, timedelta # timedelta برای تاریخ انقضا اضافه شد
import sqlalchemy.exc
from sqlalchemy import or_

# =========================================================
# 🛠️ تنظیمات اولیه و اتصال به دیتابیس
# =========================================================
app = Flask(__name__)

# 💡 اضافه شدن مسیردهی صریح برای فایل‌های استاتیک و قالب‌ها
app.static_folder = 'static'
app.template_folder = 'templates'

# --- تنظیمات ضروری ---
app.jinja_env.charset = 'utf-8'
app.secret_key = "supersecretkey123"

# 👑 شماره تلفن ادمین برای دسترسی مستقیم
ADMIN_PHONE_NUMBER = '09962935294'

# 🔔 شماره تلفن برای دریافت هشدار اتمام توکن
TOKEN_ALERT_PHONE_NUMBER = '0902328702'

# 🛍️ تنظیمات ورود با بازار (Bazaar Login Config)
BAZAAR_CLIENT_ID = "8Fk3ykSaqDNnBs54"
BAZAAR_CLIENT_SECRET = "GQfRhVPuPyvOJ0L86BTpq2lgH6wnPojq"

# =========================================================
# 🔑 تنظیمات جدید درگاه پرداخت بازارپی (نسخه Badje)
# =========================================================
BASE_URL = "https://api.bazaar-pay.ir/badje/v1"
AUTH_TOKEN = "01f16b92299ad730cb405e22ebf9a9f14b11b970"
DESTINATION_NAME = "kodular_bazaar"
YOUR_DOMAIN = "https://alie-1.onrender.com"

PRICES = {
    'weekly': 459000,    # ۴۵,۹۰۰ تومان (به ریال)
    'monthly': 1690000,  # ۱۶۹,۰۰۰ تومان (به ریال)
    'package': 30000     # ۳,۰۰۰ تومان (به ریال)
}
FREE_CHAT_LIMIT = 15

# ----------------- 💾 تنظیمات PostgreSQL (Render Internal) -----------------
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("❌ متغیر محیطی DATABASE_URL (اتصال به دیتابیس) پیدا نشد!")

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

# ----------------- 📱 تنظیمات SMS.ir (جایگزین Kavenegar) -----------------
SMSIR_API_KEY = 'rTAR33leVoNpAjnUUzzu2rygt72VrlXa7OrOqTHA5K1VgeSs' # ⬅️ کلید نمونه، تغییر دهید
SMSIR_TEMPLATE_ID = 660708 # ⬅️ شناسه قالب کد تأیید، تغییر دهید
SMSIR_VERIFY_URL = "https://api.sms.ir/v1/send/verify"

phone_verification_codes = {}
# ---------------------------------------------------------

# =========================================================
# 🔑 سیستم مدیریت کلیدهای GapGPT (Key Rotation & Fallback)
# =========================================================

GAPGPT_KEYS = {}
for i in range(1, 6): # از 1 تا 5
    key_name = f"GAPGPT_API_KEY_{i}"
    key_value = os.getenv(key_name)
    if key_value:
        GAPGPT_KEYS[key_name] = key_value

if not GAPGPT_KEYS:
    raise ValueError("❌ حداقل یک متغیر محیطی GAPGPT_API_KEY_i پیدا نشد! لطفاً آن را تنظیم کنید.")

KEY_NAMES_ORDER = list(GAPGPT_KEYS.keys())
BLOCKED_KEYS = set()
KEY_INDEX = 0

def send_token_alert(key_name, reason):
    """ارسال پیامک هشدار برای اتمام/خطای کلید API."""
    if not TOKEN_ALERT_PHONE_NUMBER:
        return
    print(f"🔔 هشدار: اخطار! کلید GapGPT ({key_name}) با خطا مواجه شد ({reason}). موقتا مسدود شد.")

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
    if total_keys == 0: return None, None
    if len(BLOCKED_KEYS) == total_keys and initial_attempt:
        print("🚨 همه کلیدهای API مسدود هستند. ریست کردن و تلاش مجدد.")
        BLOCKED_KEYS.clear()
    for _ in range(total_keys):
        key_name = KEY_NAMES_ORDER[KEY_INDEX]
        KEY_INDEX = (KEY_INDEX + 1) % total_keys
        if key_name not in BLOCKED_KEYS:
            return key_name, GAPGPT_KEYS[key_name]
    return None, None
# ---------------------------------------------------------

# 🎯 تنظیمات هزینه و بودجه امتیاز روزانه
SCORE_QUOTA_CONFIG = {
    'COSTS': {
        'chat': 1,
        'image': 20,
        'long_response': 1
    },
    'DAILY_BUDGET': {
        'free': {
            'chat': 30,
            'image': 80,
            'long_response': 5
        },
        'premium': {
            'chat': 80,
            'image': 200,
            'long_response': 15
        }
    }
}

# ---------------------------------------------------------
GAPGPT_BASE_URL = "https://api.gapapi.com/v1/chat/completions"
CHAT_MODEL_NAME = "gpt-4o-mini"
TRANSLATION_MODEL_NAME = "gpt-4o-mini"
POLLINATIONS_URL = "https://image.pollinations.ai/prompt/"
STATIC_DIR = os.path.join(app.root_path, 'static', 'temp_images')
IMAGE_LIFETIME = 3600
IMAGE_QUALITY_PARAMS = [
    "hd", "detailed", "4k", "8k", "highly detailed",
    "trending on artstation", "cinematic light", "masterpiece", "photorealistic"
]

if not os.path.exists(STATIC_DIR):
    os.makedirs(STATIC_DIR)

SYSTEM_PROMPT = """تو یک چت‌بات مفید هستی. پاسخ‌ها را به زبان فارسی و روان بده.
- برای سوالات سازنده: تیم NOCTOVEX به رهبری مهراب عزیزی
- پاسخ‌ها باید **فوق‌العاده مختصر، مفید و خیلی کوتاه** باشند و در سقف نهایی **۴۰۰ توکن** به پایان برسند."""

LONG_RESPONSE_TOKEN_THRESHOLD = 350
LONG_RESPONSE_MAX_COMPLETION_TOKENS = 400
LONG_RESPONSE_TOTAL_TOKEN_LIMIT = 500

TOTAL_TOKEN_LIMIT = 500
INPUT_TOKEN_LIMIT = 500
MAX_COMPLETION_TOKENS = 400

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
    chat_count = db.Column(db.Integer, default=0)
    premium_expiry = db.Column(db.DateTime, nullable=True)
    extra_chat_packages = db.Column(db.Integer, default=0) # بسته‌های 5 تایی
    usage = db.relationship('UserUsage', backref='user', lazy=True, uselist=False)
    conversations = db.relationship('Conversation', backref='user', lazy='dynamic')

class UserUsage(db.Model):
    __tablename__ = 'user_usage'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), unique=True, nullable=False)
    date = db.Column(db.Date, default=datetime.utcnow().date)
    chat_budget = db.Column(db.Integer, default=50)
    image_budget = db.Column(db.Integer, default=60)
    long_response_budget = db.Column(db.Integer, default=5)
    package_chat_budget = db.Column(db.Integer, default=0) # 💡 بودجه بسته‌های 24 ساعته (که در واقع از extra_chat_packages کم می‌شود)
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
def generate_verification_code(): return str(random.randint(100000, 999999))
def send_verification_email(email, code):
    try:
        msg = Message('کد تأیید حساب Cyrus AI', sender=app.config['MAIL_USERNAME'], recipients=[email])
        msg.body = f"کد تأیید حساب شما در Cyrus AI عبارت است از: {code}\nاین کد تا 5 دقیقه اعتبار دارد."
        mail.send(msg)
        return True
    except Exception as e:
        print(f"Error sending email: {e}")
        return False

def send_verification_sms(phone_number, code):
    """ارسال کد تأیید از طریق پیامک با SMS.ir (ارسال سریع)."""
    if phone_number.startswith('0'): mobile = phone_number[1:]
    else: mobile = phone_number
    payload = {"mobile": mobile, "templateId": SMSIR_TEMPLATE_ID, "parameters": [{"name": "Code", "value": code}]}
    headers = {'x-api-key': SMSIR_API_KEY, 'Content-Type': 'application/json'}
    try:
        response = requests.post(SMSIR_VERIFY_URL, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
        res_json = response.json()
        if res_json.get('status') == 1:
            print(f"SMS.ir Response: Success - MessageId: {res_json['data']['messageId']}")
            return True
        else:
            print(f"SMS.ir Error Response: {res_json.get('message', 'Unknown Error')}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"SMS.ir API Error (RequestException): {e}")
        return False
    except Exception as e:
        print(f"General SMS Error: {e}")
        return False

# =========================================================
# 💾 توابع پایداری داده (Persistence)
# =========================================================
def get_user_identifier(session): return session.get('user_email') or session.get('user_phone') or session.get('user_identifier')
def get_user_by_identifier(identifier):
    if not identifier: return None
    return User.query.filter(or_(User.email == identifier, User.phone == identifier, User.id == identifier)).first()
def get_user_by_id(user_id): return User.query.get(user_id)

def register_user_if_new(user_identifier, email=None, phone=None):
    user = get_user_by_identifier(user_identifier)
    if not user:
        is_admin = (phone == ADMIN_PHONE_NUMBER)
        user = User(id=str(uuid.uuid4()), email=email, phone=phone, score=0, is_premium=False, is_banned=False, is_admin=is_admin)
        db.session.add(user)
    else:
        if email: user.email = email
        if phone: user.phone = phone
    try:
        db.session.commit()
        return user
    except sqlalchemy.exc.IntegrityError as e:
        db.session.rollback()
        print(f"Database Integrity Error during registration: {e}")
        return None

def check_and_deduct_score(user_identifier, usage_type):
    """بررسی بودجه امتیاز روزانه، کسر هزینه و ذخیره. (منطق اصلاح شده برای اولویت بسته)"""
    user = get_user_by_identifier(user_identifier)
    if not user: return False, "خطای داخلی: کاربر در دیتابیس یافت نشد."
    
    today_date = datetime.utcnow().date()
    now = datetime.utcnow()
    
    is_premium_active = user.is_premium and user.premium_expiry and user.premium_expiry > now
    level = 'premium' if is_premium_active else 'free'
    
    cost = SCORE_QUOTA_CONFIG['COSTS'][usage_type]
    daily_limits = SCORE_QUOTA_CONFIG['DAILY_BUDGET'][level]
    budget_key = f'{usage_type}_budget'

    usage = user.usage

    # --- ریست روزانه و بررسی وضعیت پرمیوم ---
    if not usage or usage.date != today_date or usage.level_check != level:
        usage = usage or UserUsage(user_id=user.id, date=today_date)
        if not usage in db.session: db.session.add(usage)
        
        usage.date = today_date
        usage.chat_budget = daily_limits['chat']
        usage.image_budget = daily_limits['image']
        usage.long_response_budget = daily_limits.get('long_response', 0)
        usage.level_check = level

    current_budget = getattr(usage, budget_key, 0)

    # --- 🎯 منطق کسر امتیاز جدید: اول بسته 24 ساعته، سپس بودجه روزانه ---
    if usage_type == 'chat' and not is_premium_active:
        # 1. اولویت با استفاده از بسته خریداری شده (extra_chat_packages)
        if user.extra_chat_packages and user.extra_chat_packages > 0 and current_budget < cost:
            user.extra_chat_packages -= 1
            message = f"✅ از بسته اضافی شما استفاده شد. {user.extra_chat_packages} بسته باقی مانده است."
            try:
                db.session.commit()
                return True, message
            except Exception:
                 db.session.rollback()
                 return False, "خطای دیتابیس هنگام استفاده از بسته اضافی."

    # 2. کسر از بودجه روزانه (برای همه موارد و چت‌هایی که بسته ندارند)
    if current_budget < cost:
        action_fa = ('چت' if usage_type == 'chat' else 'تولید تصویر' if usage_type == 'image' else 'پاسخ بلند')
        level_fa = 'پرمیوم' if is_premium_active else 'عادی'
        remaining_uses = current_budget // cost
        error_message = (f"⛔ متأسفم، بودجه امتیاز روزانه شما برای {action_fa} ({level_fa}) کافی نیست. هزینه هر {action_fa} {cost} امتیاز است و شما {current_budget} امتیاز باقی مانده دارید. (حدود {remaining_uses} استفاده باقی مانده).")
        if not is_premium_active: error_message += " با ارتقا به حساب پرمیوم می‌توانید محدودیت‌های خود را برطرف کنید."
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
    user = get_user_by_identifier(user_identifier)
    if not user: return
    chat_entry = Conversation.query.filter_by(id=chat_id, user_id=user.id).first()
    messages_json_string = json.dumps(messages, ensure_ascii=False)
    if chat_entry:
        chat_entry.messages_json = messages_json_string
        chat_entry.last_update = time.time()
        if chat_entry.title == "گفتگوی جدید...": chat_entry.title = user_message[:50] + "..." if len(user_message) > 50 else user_message
    else:
        new_title = user_message[:50] + "..." if len(user_message) > 50 else user_message
        chat_entry = Conversation(id=chat_id, user_id=user.id, title=new_title, messages_json=messages_json_string, last_update=time.time())
        db.session.add(chat_entry)
    try: db.session.commit()
    except Exception as e: db.session.rollback(); print(f"Error saving conversation: {e}")

# =========================================================
# ⚙️ توابع کمکی، شمارنده و محدودیت (Quota)
# =========================================================
def count_tokens(messages): return sum(len(encoder.encode_ordinary(m["content"])) for m in messages)
def fix_rtl_ltr(text):
    def ltr_replacer(match): return f"\u200E{match.group(0)}\u200E"
    fixed_text = re.sub(r'([a-zA-Z0-9\/\.\-\_\=\+\(\)\{\}\[\]\*\`\:\<\>\#\@\$\%\^\&\!\"\'\?\;\,\s]+)', ltr_replacer, text)
    final_lines = [f"\u200F{line}" for line in fixed_text.split('\n')]
    return "\n".join(final_lines)

def translate_prompt_to_english(persian_prompt):
    # ... (منطق ترجمه بدون تغییر)
    translation_system_prompt = ("You are an expert prompt engineer. Translate the following Persian description into a detailed, high-quality English prompt suitable for a Stable Diffusion image generator. The prompt should be artistic and descriptive (e.g., 'digital painting, 4k, cinematic light'). Do not add any explanation or text other than the translated prompt itself. Ensure the translation is vivid and descriptive, ready for image generation.")
    messages = [{"role": "system", "content": translation_system_prompt}, {"role": "user", "content": persian_prompt}]
    max_attempts = len(GAPGPT_KEYS)
    for attempt in range(max_attempts):
        key_name, current_api_key = get_openrouter_key(initial_attempt=(attempt==0))
        if not current_api_key: return persian_prompt
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {current_api_key}"}
        data = {"model": TRANSLATION_MODEL_NAME, "messages": messages, "max_tokens": 75}
        try:
            response = requests.post(GAPGPT_BASE_URL, json=data, headers=headers, timeout=15)
            response.raise_for_status()
            english_prompt = response.json()["choices"][0]["message"]["content"].strip()
            return english_prompt
        except requests.exceptions.RequestException as e:
            status_code = getattr(e.response, 'status_code', 500)
            print(f"Translation API Error (Key: {key_name}): {e}. Status: {status_code}")
            if status_code in [402, 401]:
                handle_key_failure(key_name, status_code)
                if attempt == max_attempts - 1: return persian_prompt
                continue
            else: return persian_prompt
        except Exception as e:
            print(f"Translation General Error: {e}")
            return persian_prompt
    return persian_prompt

def generate_and_crop_image(english_prompt):
    full_prompt = f"{english_prompt}, {', '.join(IMAGE_QUALITY_PARAMS)}"
    image_url = f"{POLLINATIONS_URL}{full_prompt.replace(' ', '%20')}"
    try:
        response = requests.get(image_url, timeout=100)
        response.raise_for_status()
        img = Image.open(BytesIO(response.content))
        width, height = img.size
        crop_box = (0, 0, max(0, width - 40), max(0, height - 60))
        cropped_img = img.crop(crop_box)
        file_name = f"cropped_{uuid.uuid4()}.jpg"
        file_path = os.path.join(STATIC_DIR, file_name)
        cropped_img.save(file_path, 'JPEG', quality=95)
        return file_name
    except requests.exceptions.Timeout: return "TIMEOUT_100_SEC"
    except Exception as e: print(f"Error in image generation/cropping: {e}"); return None

@app.cli.command("cleanup-images")
def cleanup_images_command(): cleanup_old_images()
def cleanup_old_images():
    now = time.time()
    for filename in glob.glob(os.path.join(STATIC_DIR, '*')):
        try:
            if now - os.path.getmtime(filename) > IMAGE_LIFETIME:
                os.remove(filename)
                print(f"🗑️ Deleted old image: {filename}")
        except Exception as e: print(f"Error deleting file {filename}: {e}")

# =========================================================
# 👑 توابع و مسیرهای پنل مدیریت (Blueprint) و لاگین مورد نیاز
# =========================================================

# --- تعریف دکوراتورها در ابتدای فایل ---
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user_identifier = get_user_identifier(session)
        user = get_user_by_identifier(identifier=user_identifier)
        if not user or not user.is_admin:
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('user_identifier'): # استفاده از user_identifier برای تشخیص لاگین بودن
            return redirect(url_for('login_phone'))
        return f(*args, **kwargs)
    return decorated_function
# --- پایان تعریف دکوراتورها ---

admin_bp = Blueprint('admin', __name__, url_prefix='/admin', template_folder='templates')

@admin_bp.route("/")
@admin_required
def admin_dashboard():
    # ... (منطق ادمین)
    total_users = User.query.count()
    premium_users = User.query.filter_by(is_premium=True).count()
    banned_users = User.query.filter_by(is_banned=True).count()
    context = {
        'total_users': total_users, 'premium_users': premium_users, 'banned_users': banned_users,
        'admin_identifier': get_user_identifier(session)
    }
    return render_template("admin_dashboard.html", **context)

@admin_bp.route("/users")
@admin_required
def manage_users():
    # ... (منطق مدیریت کاربران)
    all_users = User.query.all()
    users_list = [{'identifier': u.email or u.phone or u.id, 'score': u.score, 'is_premium': u.is_premium, 'is_banned': u.is_banned, 'email': u.email or 'N/A', 'phone': u.phone or 'N/A'} for u in all_users]
    return render_template("admin_users.html", users=users_list)

@admin_bp.route("/user_action", methods=["POST"])
@admin_required
def user_action():
    # ... (منطق اعمال تغییرات)
    identifier = request.json.get("identifier")
    action = request.json.get("action")
    value = request.json.get("value")
    user = get_user_by_identifier(identifier)
    if not user: return jsonify({"status": "error", "message": "کاربر یافت نشد."}), 404
    if action == "set_score":
        try: score = int(value); user.score = score; message = f"امتیاز کاربر {identifier} به {score} تغییر یافت."
        except ValueError: return jsonify({"status": "error", "message": "امتیاز باید عدد صحیح باشد."}), 400
    elif action == "toggle_premium":
        user.is_premium = not user.is_premium
        message = f"وضعیت کاربر {identifier}: {'پرمیوم شد' if user.is_premium else 'عادی شد'}."
        if user.usage: user.usage.level_check = None
    elif action == "toggle_ban":
        user.is_banned = not user.is_banned
        message = f"وضعیت بن کاربر {identifier}: {'بن شد' if user.is_banned else 'رفع بن شد'}."
    else: return jsonify({"status": "error", "message": "عملیات نامعتبر."}), 400
    try: db.session.commit()
    except Exception as e: db.session.rollback(); return jsonify({"status": "error", "message": f"خطای دیتابیس: {e}"}), 500
    return jsonify({"status": "success", "message": message, "new_status": {'is_premium': user.is_premium, 'is_banned': user.is_banned, 'score': user.score}})

app.register_blueprint(admin_bp)

# =========================================================
# 📧 مسیرهای احراز هویت (ایمیل و پیامک)
# =========================================================
@app.route("/send_code", methods=["POST"])
def send_code():
    user_email = request.json.get("email", "").strip().lower()
    if not user_email: return jsonify({"status": "error", "message": "لطفاً ایمیل خود را وارد کنید."}), 400
    code = generate_verification_code()
    verification_codes[user_email] = {'code': code, 'expiry_time': time.time() + 300}
    if not send_verification_email(user_email, code):
        return jsonify({"status": "error", "message": "خطا در ارسال ایمیل. مطمئن شوید تنظیمات SMTP صحیح است."}), 500
    return jsonify({"status": "success", "message": "کد تأیید به ایمیل شما ارسال شد. لطفاً صندوق ورودی را بررسی کنید."})

@app.route("/verify_code", methods=["POST"])
def verify_code():
    user_email = request.json.get("email", "").strip().lower()
    entered_code = request.json.get("code", "").strip()
    if user_email not in verification_codes: return jsonify({"status": "error", "message": "ایمیل نامعتبر یا درخواستی برای آن ثبت نشده است."}), 400
    stored_data = verification_codes[user_email]
    if time.time() > stored_data['expiry_time']:
        del verification_codes[user_email]
        return jsonify({"status": "error", "message": "کد تأیید منقضی شده است. لطفاً مجدداً درخواست کد دهید."}), 400
    if entered_code == stored_data['code']:
        del verification_codes[user_email]
        user = register_user_if_new(user_email, email=user_email)
        if not user: return jsonify({"status": "error", "message": "خطا در ثبت/بازیابی کاربر از دیتابیس."}), 500
        session.clear()
        session['user_id'] = user.id
        session['user_identifier'] = user_email # اصلاح: ذخیره شناسه برای استفاده در مسیرهای دیگر
        session['needs_profile_info'] = True
        session['is_admin'] = user.is_admin
        return jsonify({"status": "success", "redirect": url_for('account')})
    else: return jsonify({"status": "error", "message": "کد وارد شده صحیح نیست."}), 400

@app.route("/send_sms_code", methods=["POST"])
def send_sms_code():
    phone_number = request.json.get("phone", "").strip()
    if not re.match(r'^0?9\d{9}$', phone_number):
        return jsonify({"status": "error", "message": "لطفاً یک شماره تلفن معتبر (مانند 0912...) وارد کنید."}), 400
    code = generate_verification_code()
    phone_verification_codes[phone_number] = {'code': code, 'expiry_time': time.time() + 300}
    if not send_verification_sms(phone_number, code):
        return jsonify({"status": "error", "message": "خطا در ارسال پیامک. لطفاً شماره و تنظیمات SMS.ir را بررسی کنید."}), 500
    return jsonify({"status": "success", "message": "کد تأیید به شماره شما ارسال شد. لطفاً پیامک‌ها را بررسی کنید."})

@app.route("/verify_sms_code", methods=["POST"])
def verify_sms_code():
    phone_number = request.json.get("phone", "").strip()
    entered_code = request.json.get("code", "").strip()
    if phone_number not in phone_verification_codes: return jsonify({"status": "error", "message": "شماره نامعتبر یا درخواستی برای آن ثبت نشده است."}), 400
    stored_data = phone_verification_codes[phone_number]
    if time.time() > stored_data['expiry_time']:
        del phone_verification_codes[phone_number]
        return jsonify({"status": "error", "message": "کد تأیید منقضی شده است. لطفاً مجدداً درخواست کد دهید."}), 400
    if entered_code == stored_data['code']:
        del phone_verification_codes[phone_number]
        user = register_user_if_new(phone_number, phone=phone_number)
        if not user: return jsonify({"status": "error", "message": "خطا در ثبت/بازیابی کاربر از دیتابیس."}), 500
        is_admin = user.is_admin
        redirect_url = url_for('admin.admin_dashboard') if is_admin else url_for('account')
        session.clear()
        session['user_id'] = user.id
        session['user_identifier'] = phone_number
        session['needs_profile_info'] = True
        session['is_admin'] = is_admin
        return jsonify({"status": "success", "redirect": redirect_url})
    else: return jsonify({"status": "error", "message": "کد وارد شده صحیح نیست."}), 400

# =========================================================
# 💬 مسیر چت و بقیه مسیرها (با اعمال محدودیت و چرخش کلید)
# =========================================================

@app.route("/chat", methods=["POST"])
def chat():
    user_message = request.json.get("message", "")
    lower_msg = user_message.lower()
    if not user_message.strip(): return jsonify({"reply": "لطفاً پیامی ارسال کنید."})

    user_identifier = get_user_identifier(session)
    user = get_user_by_identifier(user_identifier)
    now = datetime.utcnow()
    is_active_premium = user and user.is_premium and user.premium_expiry and user.premium_expiry > now
    
    user_message_tokens = count_tokens([{"role": "user", "content": user_message}])
    
    # 1. بررسی محدودیت پیام بلند
    if not is_active_premium and user_message_tokens >= LONG_RESPONSE_TOKEN_THRESHOLD:
        return jsonify({"reply": "⛔ عذر می‌خواهم، محدودیت توکن شما برای حساب عادی رد شده است. برای ارسال پیام‌های طولانی لطفاً اشتراک تهیه کنید.", "show_upgrade": True})

    # 2. بررسی محدودیت چت روزانه و استفاده از بسته اضافی
    if user:
        if user.is_banned: return jsonify({"reply": "⛔ متأسفم، حساب کاربری شما توسط مدیر سیستم مسدود شده است."})
        
        # کسر امتیاز یا بسته
        is_allowed, result = check_and_deduct_score(user_identifier, 'chat')
        if not is_allowed:
            if isinstance(result, str) and "از بسته اضافی" in result:
                 return jsonify({"reply": result, "use_package": True})
            return jsonify({"reply": result})

    else:
        # کاربر مهمان
        today_date_str = now.date().isoformat()
        if session.get('guest_last_date') != today_date_str:
            session['guest_chat_count'] = 0
            session['guest_last_date'] = today_date_str
        guest_count = session.get('guest_chat_count', 0)
        if guest_count >= GUEST_CHAT_LIMIT:
            return jsonify({"reply": "⛔ متأسفم، شما به سقف **۵ چت روزانه** برای کاربران مهمان رسیده‌اید. لطفاً وارد حساب کاربری خود شوید تا چت‌های نامحدود دریافت کنید."})
        session['guest_chat_count'] = guest_count + 1

    # --- پاسخ‌های اختصاصی ---
    if "مامی سازندت کیه" in lower_msg: return jsonify({"reply": "عسل خانوم 💖"})
    if any(keyword in lower_msg for keyword in ["اعضای تیمت کیا هستن", "اعضای noctovex", "noctovex members"]):
        new_reply = "تنها NOCTOVEX معتبر ما هستیم، و تیم ما متشکل از 5 تا 10 کدنویس حرفه‌ای است. در حال حاضر، هویت تنها دو نفر از ما مشخص است: مهراب، که رهبر تیم، لیدر و حرفه‌ای‌ترین کدنویس است، و آرشام. 🧑‍💻"
        return jsonify({"reply": new_reply})
    if any(keyword in lower_msg for keyword in ["سازندت کیه", "تو کی هستی", "چه شرکتی", "who made you"]):
        new_reply = "من توسط تیم NOCTOVEX توسعه داده شده‌ام. این تیم توسط **مهراب عزیزی** رهبری می‌شود که مدیریت پروژه، برنامه‌ریزی و هدایت توسعه‌دهندگان را بر عهده دارد. 👑"
        return jsonify({"reply": new_reply})

    # --- مدیریت تاریخچه و توکن‌ها ---
    current_chat_id = session.get('current_chat_id')
    if user and session.get('user_id'):
        if not current_chat_id:
            current_chat_id = str(uuid.uuid4())
            session['current_chat_id'] = current_chat_id
    else:
        session.pop('current_chat_id', None)

    session["conversation"] = [] # پاکسازی تاریخچه برای حفظ سقف توکن پایین

    messages_list = [{"role": "system", "content": system_prompt_to_use}]
    messages_list.extend(session.get("conversation", []))
    messages_list.append({"role": "user", "content": user_message})

    prompt_tokens = count_tokens(messages_list)
    remaining_tokens = TOTAL_TOKEN_LIMIT - prompt_tokens
    max_tokens = min(max(20, remaining_tokens), MAX_COMPLETION_TOKENS)

    max_attempts = len(GAPGPT_KEYS)
    ai_message = None

    for attempt in range(max_attempts):
        key_name, current_api_key = get_openrouter_key(initial_attempt=(attempt==0))
        if not current_api_key:
            ai_message = "❌ خطایی در سیستم رخ داد. سرور در حال به‌روزرسانی است، لطفاً کمی بعد دوباره امتحان کنید."
            break
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {current_api_key}"}
        data = {"model": CHAT_MODEL_NAME, "messages": messages_list, "max_tokens": max_tokens}
        try:
            response = requests.post(GAPGPT_BASE_URL, json=data, headers=headers, timeout=10)
            response.raise_for_status()
            ai_message = response.json()["choices"][0]["message"]["content"]
            break
        except requests.exceptions.RequestException as e:
            status_code = getattr(e.response, 'status_code', 500)
            print(f"API Request Error (Key: {key_name}): {e}. Status: {status_code}")
            if status_code in [402, 401]:
                handle_key_failure(key_name, status_code)
                if attempt == max_attempts - 1:
                    ai_message = "❌ خطایی در سیستم رخ داد. سرور در حال به‌روزرسانی است، لطفاً کمی بعد دوباره امتحان کنید."
                    break
                continue
            else:
                ai_message = "⚠️ متأسفم، مشکلی در اتصال به سرور پیش آمد. لطفاً دوباره امتحان کنید."
                break
        except Exception as e:
            print(f"General Error: {e}")
            ai_message = "⚠️ مشکلی پیش اومد!"
            break

    if ai_message: ai_message = fix_rtl_ltr(ai_message)
    else: ai_message = "❌ خطایی در سیستم رخ داد. سرور در حال به‌روزرسانی است، لطفاً کمی بعد دوباره امتحان کنید."

    if not ai_message.startswith(("❌", "⚠️", "⛔")):
        current_chat_to_save = [{"role": "user", "content": user_message}, {"role": "assistant", "content": ai_message}]
        session["conversation"] = []
        if user and session.get('user_id') and current_chat_id:
            chat_entry = Conversation.query.filter_by(id=session['current_chat_id'], user_id=user.id).first()
            if chat_entry:
                try:
                    prev_messages = json.loads(chat_entry.messages_json)
                    prev_messages.extend(current_chat_to_save)
                    save_conversation(user_identifier, current_chat_id, prev_messages, user_message)
                except Exception:
                    save_conversation(user_identifier, current_chat_id, current_chat_to_save, user_message)
            else:
                 save_conversation(user_identifier, current_chat_id, current_chat_to_save, user_message)

    session["conversation"] = []
    return jsonify({"reply": ai_message})

@app.route("/clear_history", methods=["POST"])
def clear_history():
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

    if not user: return jsonify({"status": "error", "message": "لطفاً ابتدا وارد حساب کاربری خود شوید."}), 403
    if user.is_banned: return jsonify({"status": "error", "message": "⛔ متأسفم، حساب کاربری شما توسط مدیر سیستم مسدود شده است."}), 403

    is_allowed, result = check_and_deduct_score(user_identifier, 'image')
    if not is_allowed: return jsonify({"status": "error", "message": result}), 429

    if not persian_prompt or len(persian_prompt.split()) < 1:
        return jsonify({"status": "error", "message": "لطفاً موضوع دقیق‌تر تصویر مورد نظرتان را به فارسی بنویسید."}), 400

    try:
        english_prompt = translate_prompt_to_english(persian_prompt)
        seed = random.randint(1, 1000000)
        quality = "%20".join(IMAGE_QUALITY_PARAMS)
        direct_image_url = f"{POLLINATIONS_URL}{english_prompt.replace(' ', '%20')}%20{quality}?nologo=true&seed={seed}"

        return jsonify({"status": "success", "message": f"تصویر شما با پرامپت '{persian_prompt}' تولید شد. 🖼️", "image_url": direct_image_url})
    except Exception as e:
        print(f"Image Generator Handler Error: {e}")
        return jsonify({"status": "error", "message": f"❌ خطای داخلی سرور هنگام پردازش تصویر."}), 500

# =========================================================
# 🏠 مسیرهای سرویس‌دهی صفحات HTML
# =========================================================
@app.route("/")
def index():
    cleanup_old_images()
    conversation_history = session.get("conversation", [])
    display_messages = [{"role": msg["role"], "content": fix_rtl_ltr(msg["content"])} for msg in conversation_history]
    return render_template("index.html", logged_in=session.get('user_id') is not None, is_admin=session.get('is_admin', False), chat_history=display_messages)

@app.route("/image")
def image_page():
    return render_template("image.html", logged_in=session.get('user_id') is not None, is_admin=session.get('is_admin', False))

@app.route("/premium")
def premium_page():
    return render_template("premium.html", logged_in=session.get('user_id') is not None, is_admin=session.get('is_admin', False))

# =========================================================
# 🎮 مسیرهای بازی
# =========================================================
@app.route("/game")
def game_center(): return render_template("game.html", logged_in=session.get('user_id') is not None)
@app.route("/game/car")
def car_game(): return render_template("car_game.html", logged_in=session.get('user_id') is not None)
@app.route("/game/guess")
def guess_game(): return render_template("number_guess_game.html", logged_in=session.get('user_id') is not None)

# --- مسیرهای احراز هویت ---
@app.route("/login")
def login():
    if session.get('user_id'): return redirect(url_for('account'))
    return render_template("account_login.html")
@app.route("/login_phone")
def login_phone():
    if session.get('user_id'): return redirect(url_for('account'))
    return render_template("account_login_phone.html")
@app.route("/login_google")
def login_google(): return redirect(url_for('login'))

@app.route("/account")
def account():
    if not session.get('user_id'): return redirect(url_for('login'))
    user_id = session.get('user_id')
    user = get_user_by_id(user_id)
    if not user: session.clear(); return redirect(url_for('login'))
    if user.is_admin or session.get('is_admin'): return redirect(url_for('admin.admin_dashboard'))
    if session.get('needs_profile_info'): return redirect(url_for('complete_profile_mock'))
    return redirect(url_for('profile'))

@app.route("/verify_page")
def verify_page(): return render_template("account_verify.html")
@app.route("/verify_page_phone")
def verify_page_phone(): return render_template("account_verify_phone.html")

# --- مسیرهای تک صفحه‌ای ---
@app.route("/support")
def support(): return render_template("support.html")
@app.route("/about")
def about(): return render_template("about.html")
@app.route("/terms_of_service")
def terms_of_service(): return render_template("terms_of_service.html")
@app.route("/privacy_policy")
def privacy_policy(): return render_template("privacy_policy.html")

@app.route("/profile")
def profile():
    if not session.get('user_id'): return redirect(url_for('login'))
    user_id = session.get('user_id')
    user = get_user_by_id(user_id)
    if not user: session.clear(); return redirect(url_for('login'))

    is_premium = user.is_premium
    level = 'premium' if is_premium else 'free'
    today_date = datetime.utcnow().date()
    now = datetime.utcnow()
    daily_limits = SCORE_QUOTA_CONFIG['DAILY_BUDGET'][level]

    usage = user.usage
    if not usage or usage.date != today_date or (user.is_premium and user.premium_expiry and user.premium_expiry > now and usage.level_check != 'premium') or (not user.is_premium and usage.level_check != 'free'):
        chat_budget_remaining = daily_limits['chat']
        image_budget_remaining = daily_limits['image']
        long_response_budget_remaining = daily_limits.get('long_response', 0)
    else:
        chat_budget_remaining = usage.chat_budget
        image_budget_remaining = usage.image_budget
        long_response_budget_remaining = usage.long_response_budget

    chat_cost = SCORE_QUOTA_CONFIG['COSTS']['chat']
    image_cost = SCORE_QUOTA_CONFIG['COSTS']['image']
    long_response_cost = SCORE_QUOTA_CONFIG['COSTS'].get('long_response', 1)

    user_data = {
        'identifier': user.email or user.phone or user.id,
        'is_admin': user.is_admin, 'score': user.score, 'is_premium': user.is_premium, 'is_banned': user.is_banned,
        'chat_budget_remaining': chat_budget_remaining,
        'image_budget_remaining': image_budget_remaining,
        'long_response_budget_remaining': long_response_budget_remaining,
        'chat_cost': chat_cost, 'image_cost': image_cost, 'long_response_cost': long_response_cost,
        'chats_remaining': chat_budget_remaining // chat_cost,
        'images_remaining': image_budget_remaining // image_cost,
        'long_responses_remaining': long_response_budget_remaining // long_response_cost if long_response_cost > 0 else long_response_budget_remaining,
        'max_chats': daily_limits['chat'] // chat_cost,
        'max_images': daily_limits['image'] // image_cost,
        'max_long_responses': daily_limits.get('long_response', 0) // long_response_cost if long_response_cost > 0 else daily_limits.get('long_response', 0),
    }
    return render_template("account_profile.html", user_data=user_data)

@app.route("/complete_profile", methods=['GET', 'POST'])
def complete_profile_mock():
    if not session.get('user_id'): return redirect(url_for('login'))
    user_id = session.get('user_id')
    user = get_user_by_id(user_id)
    if not user: session.clear(); return redirect(url_for('login'))
    user_data = {'identifier': user.email or user.phone or user.id}
    if request.method == 'POST':
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
    if not session.get('user_id'): return redirect(url_for('login'))
    return render_template("my_conversations.html")

@app.route("/get_conversations_list", methods=["GET"])
def get_conversations_list():
    user_id = session.get('user_id')
    if not user_id: return jsonify({"status": "error", "message": "لطفاً ابتدا وارد حساب کاربری خود شوید."}), 403
    conversations_query = Conversation.query.filter_by(user_id=user_id).order_by(Conversation.last_update.desc()).all()
    formatted_list = []
    for chat in conversations_query:
        date_str = time.strftime('%Y/%m/%d - %H:%M', time.localtime(chat.last_update))
        try:
            messages = json.loads(chat.messages_json)
            preview = messages[1]['content'][:80] + '...' if len(messages) > 1 else 'شروع گفتگو...'
        except Exception: preview = 'خطا در بارگذاری پیام‌ها...'
        formatted_list.append({'id': chat.id, 'title': chat.title, 'last_update': date_str, 'preview': preview})
    return jsonify({"status": "success", "conversations": formatted_list})

@app.route("/load_conversation/<chat_id>", methods=["POST"])
def load_conversation(chat_id):
    user_id = session.get('user_id')
    if not user_id: return jsonify({"status": "error", "message": "مجوز دسترسی ندارید."}), 403
    chat_entry = Conversation.query.filter_by(id=chat_id, user_id=user_id).first()
    if chat_entry:
        try:
            session['conversation'] = json.loads(chat_entry.messages_json)
            session['current_chat_id'] = chat_entry.id
            return jsonify({"status": "success", "message": "گفتگو با موفقیت بارگذاری شد.", "redirect": url_for('index')})
        except Exception: return jsonify({"status": "error", "message": "خطا در پردازش داده‌های گفتگو."}), 500
    else: return jsonify({"status": "error", "message": "گفتگوی مورد نظر یافت نشد."}), 404

# =========================================================
# 🛍️ مسیرهای احراز هویت با کافه‌بازار (Bazaar Auth)
# =========================================================
@app.route("/bazaar_login")
def bazaar_login():
    redirect_uri = "https://alie-0die.onrender.com/bazaar_callback"
    encoded_redirect_uri = quote(redirect_uri, safe='')
    state = uuid.uuid4().hex
    session['state'] = state
    bazaar_auth_url = (f"https://cafebazaar.ir/user/oauth?redirect_url={encoded_redirect_uri}&client_id={BAZAAR_CLIENT_ID}&state={state}&scope=profile")
    return redirect(bazaar_auth_url)

@app.route("/bazaar_callback")
def bazaar_callback():
    auth_code = request.args.get('code')
    received_state = request.args.get('state')
    expected_state = session.get('state')

    token_url = "https://account.cafebazaar.ir/api/v0/tokens"
    userinfo_url = "http://account.cafebazaar.ir/api/v0/userinfo"
    data = {'grant_type': 'authorization_code', 'code': auth_code, 'client_id': BAZAAR_CLIENT_ID, 'client_secret': BAZAAR_CLIENT_SECRET}
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}

    try:
        response = requests.post(token_url, data=data, headers=headers, timeout=10)
        response.raise_for_status()
        tokens = response.json()
        access_token = tokens.get('access_token')
        token_type = tokens.get('token_type', 'Bearer')

        user_headers = {'Authorization': f'{token_type} {access_token}'}
        user_response = requests.get(userinfo_url, headers=user_headers, timeout=10)
        user_response.raise_for_status()
        user_info = user_response.json()

        bazaar_identifier = user_info.get('phone_number') or user_info.get('mobile') or user_info.get('account_id')

        if not bazaar_identifier: return "Authentication Failed: Could not find any identifier (phone or account_id) in User Info response.", 500

        if 'state' in session: session.pop('state')

        bazaar_user_id = f"bazaar_{bazaar_identifier}"
        user = register_user_if_new(bazaar_user_id)
        if not user: return "Internal Error: Could not create user from Bazaar account", 500

        session.clear()
        session['user_id'] = user.id
        session['user_identifier'] = bazaar_user_id
        session['is_admin'] = user.is_admin
        return redirect(url_for('account'))

    except requests.exceptions.RequestException as e:
        print(f"Bazaar Auth Error: {e}")
        return "Authentication Failed due to API error.", 500
    except Exception as e:
        print(f"General Bazaar Auth Error: {e}")
        return "Authentication Failed due to internal error.", 500

# =========================================================
# 💳 مسیرهای پرداخت بازارپی (BazaarPay Routes)
# =========================================================
@app.route("/pay/<plan_type>")
@login_required # استفاده از دکوراتور تعریف شده در ابتدای فایل
def initiate_pay(plan_type):
    user_identifier = session.get('user_identifier')
    user = get_user_by_identifier(user_identifier)

    if not user: return "User not found during payment initiation.", 404

    amounts = {'weekly': 250000, 'monthly': 700000, 'package': 30000}
    amount = amounts.get(plan_type, 30000)
    
    # 🔴 اصلاح شده: استفاده از YOUR_DOMAIN برای ساخت callback
    callback_url = f"{YOUR_DOMAIN}/bazaarpay/callback/{plan_type}/{user.phone or user.id}"

    payload = {"amount": amount, "service_name": f"شارژ حساب {plan_type}", "destination": DESTINATION_NAME, "callback_url": callback_url}

    try:
        headers = {"Content-Type": "application/json"}
        response = requests.post(f"{BASE_URL}/checkout/init/", headers=headers, data=json.dumps(payload))
        response.raise_for_status()
        payment_url_base = response.json().get('payment_url')
        
        user_phone = user.phone if user.phone else ""
        from urllib.parse import urlencode, quote
        query_params = {"phone": user_phone, "redirect_url": callback_url}
        encoded_params = urlencode(query_params, quote_via=quote)

        return redirect(f"{payment_url_base}&{encoded_params}")
    except Exception as e:
        print(f"❌ خطای درگاه: {str(e)}")
        return f"خطا در اتصال به درگاه: {str(e)}", 500

@app.route('/bazaarpay/callback/<plan_type>/<user_id>', methods=['GET', 'POST'])
def bazaarpay_callback(plan_type, user_id):
    checkout_token = request.args.get('token') or request.form.get('token')
    if not checkout_token: return render_template("payment_result.html", success=False, error="توکن پرداخت دریافت نشد")

    try:
        trace_res = requests.post(f"{BASE_URL}/trace/", headers={"Content-Type": "application/json"}, data=json.dumps({"checkout_token": checkout_token}))
        trace_data = trace_res.json()

        if trace_data.get('status') == 'paid_not_committed':
            commit_headers = {"Content-Type": "application/json", "Authorization": f"Token {AUTH_TOKEN}"}
            commit_res = requests.post(f"{BASE_URL}/commit/", headers=commit_headers, data=json.dumps({"checkout_token": checkout_token}))

            if commit_res.status_code == 204:
                user = get_user_by_identifier(user_id)
                if user:
                    if plan_type == 'weekly':
                        user.is_premium = True
                        user.premium_expiry = datetime.utcnow() + timedelta(days=7)
                    elif plan_type == 'monthly':
                        user.is_premium = True
                        user.premium_expiry = datetime.utcnow() + timedelta(days=30)
                    elif plan_type == 'package':
                        user.extra_chat_packages = (user.extra_chat_packages or 0) + 1
                    db.session.commit()
                    return render_template("payment_result.html", success=True)

        return render_template("payment_result.html", success=False, error="پرداخت تایید نشد یا لغو شده است")
    except Exception as e:
        print(f"❌ خطای بازگشت از درگاه: {str(e)}")
        return render_template("payment_result.html", success=False, error=f"خطای سیستمی: {str(e)}")

# =========================================================
# ▶️ اجرای برنامه و Migration
# =========================================================
def migrate_database():
    with app.app_context():
        try:
            db.create_all()
            from sqlalchemy import text
            # اطمینان از وجود ستون‌های مورد نیاز
            db.session.execute(text('ALTER TABLE "users" ADD COLUMN IF NOT EXISTS chat_count INTEGER DEFAULT 0'))
            db.session.execute(text('ALTER TABLE "users" ADD COLUMN IF NOT EXISTS premium_expiry TIMESTAMP'))
            db.session.execute(text('ALTER TABLE "users" ADD COLUMN IF NOT EXISTS extra_chat_packages INTEGER DEFAULT 0'))
            db.session.commit()
            print("✅ وضعیت دیتابیس: تمام جداول و ستون‌ها آماده هستند.")
        except Exception as e:
            db.session.rollback()
            print(f"⚠️ وضعیت دیتابیس: {e}")

migrate_database()

if __name__ == "__main__":
    if os.environ.get("FLASK_ENV") != "production":
        cleanup_old_images()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
