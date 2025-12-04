import os
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, Blueprint
import requests
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
from datetime import date # ⬅️ اضافه شده برای تاریخ دقیقتر

# =========================================================
# 🛠️ تنظیمات اولیه و ذخیره‌سازهای موقت
# =========================================================
app = Flask(__name__)

# --- تنظیمات ضروری ---
app.jinja_env.charset = 'utf-8'
app.secret_key = "supersecretkey123" 

# 👑 شماره تلفن ادمین برای دسترسی مستقیم
ADMIN_PHONE_NUMBER = '09962935294' 

API_KEY = os.getenv("OPENROUTER_API_KEY")
if not API_KEY:
    raise ValueError("❌ متغیر محیطی OPENROUTER_API_KEY پیدا نشد! لطفاً آن را تنظیم کنید.")

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

# 💡 ساختار برای ذخیره دائم گفتگوها (شبیه‌سازی پایگاه داده)
# { 'user_identifier': [ {id: uuid, title: str, messages: [msgs...], last_update: timestamp}, ... ] }
USER_CONVERSATIONS = {} 

# 🎯 ساختار جدید برای ذخیره اطلاعات پروفایل کاربر (برای مدیریت ادمین)
# { 'user_identifier': { 'id': uuid, 'email': str, 'phone': str, 'score': int, 'is_premium': bool, 'is_banned': bool } }
USER_DATA = {} 
USER_DATA_FILE = 'user_data.json' # ⬅️ فایل ذخیره داده‌های کاربر

# 🎯 تنظیمات هزینه و بودجه امتیاز روزانه (جدید - شامل درخواست 50 چت و 60 عکس برای کاربر عادی)
SCORE_QUOTA_CONFIG = {
    'COSTS': {
        'chat': 1, # هر چت 1 امتیاز (مطابق درخواست)
        'image': 20 # هر عکس 20 امتیاز
    },
    'DAILY_BUDGET': {
        'free': {
            'chat': 50,  # 50 امتیاز برای چت (50 چت)
            'image': 60  # 60 امتیاز برای تصویر (3 عکس)
        },
        'premium': {
            'chat': 100, # 100 امتیاز برای چت (100 چت)
            'image': 120 # 120 امتیاز برای تصویر (6 عکس)
        }
    }
}


# 🗓️ ساختار برای ذخیره بودجه امتیاز باقی مانده در روز جاری
# { 'user_identifier': { 'date': '2025-12-04', 'chat_budget': 49, 'image_budget': 60 } }
USER_USAGE = {}
USAGE_DATA_FILE = 'user_usage.json' # ⬅️ فایل ذخیره استفاده روزانه

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

SYSTEM_PROMPT = """
تو یک چت‌بات حرفه‌ای هستی. پاسخ‌ها باید:
- مرتب، روان و قابل فهم باشند
- کامل و دقیق، بدون جمله اضافه
- فارسی بده مگر کاربر درخواست انگلیسی کند
- برای سوالات سازنده یا "چه شرکتی": تیم NOCTOVEX
- اگر کد می‌دهی: بلاک کد با زبان مشخص، قابل اجرا و بدون خطا
- اگر شعر/داستان: زیبا، روان و با وزن و قافیه درست
- سوالات پیچیده: خلاصه و مرحله‌به‌مرحله با بولت/شماره
- پاسخ کوتاه و کامل باشد تا کل توکن < 750
"""

TOTAL_TOKEN_LIMIT = 750 
INPUT_TOKEN_LIMIT = 500 
encoder = tiktoken.get_encoding("cl100k_base")


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

def load_user_data():
    """بارگذاری داده‌های کاربران (امتیاز، پرمیوم، بن) از فایل JSON."""
    global USER_DATA
    if os.path.exists(USER_DATA_FILE):
        try:
            with open(USER_DATA_FILE, 'r', encoding='utf-8') as f:
                USER_DATA = json.load(f)
        except Exception as e:
            print(f"⚠️ Error loading user data: {e}. Starting with empty data.")
            USER_DATA = {}

def save_user_data():
    """ذخیره داده‌های کاربران در فایل JSON."""
    global USER_DATA
    try:
        temp_file = USER_DATA_FILE + '.tmp'
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(USER_DATA, f, indent=4, ensure_ascii=False)
        os.replace(temp_file, USER_DATA_FILE)
    except Exception as e:
        print(f"❌ Error saving user data: {e}")

def load_user_usage():
    """بارگذاری داده‌های استفاده روزانه (بودجه) از فایل JSON."""
    global USER_USAGE
    if os.path.exists(USAGE_DATA_FILE):
        try:
            with open(USAGE_DATA_FILE, 'r', encoding='utf-8') as f:
                USER_USAGE = json.load(f)
        except Exception as e:
            print(f"⚠️ Error loading user usage data: {e}. Starting with empty data.")
            USER_USAGE = {}

def save_user_usage():
    """ذخیره داده‌های استفاده روزانه (بودجه) در فایل JSON."""
    global USER_USAGE
    try:
        temp_file = USAGE_DATA_FILE + '.tmp'
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(USER_USAGE, f, indent=4, ensure_ascii=False)
        os.replace(temp_file, USAGE_DATA_FILE)
    except Exception as e:
        print(f"❌ Error saving user usage data: {e}")

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
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}"
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
        return english_prompt
    except Exception as e:
        print(f"Translation Error: {e}")
        return persian_prompt

def generate_and_crop_image(english_prompt):
    full_prompt = f"{english_prompt}, {', '.join(IMAGE_QUALITY_PARAMS)}"
    image_url = f"{POLLINATIONS_URL}{full_prompt.replace(' ', '%20')}"
    
    try:
        response = requests.get(image_url, timeout=40) 
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
        
    except Exception as e:
        print(f"Error in image generation/cropping: {e}")
        return None

def get_user_identifier(session):
    """برگرداندن ایمیل یا شماره تلفن برای ذخیره‌سازی گفتگو."""
    return session.get('user_email') or session.get('user_phone')

def register_user_if_new(user_identifier, email=None, phone=None):
    """اگر کاربر جدید است، آن را در USER_DATA ثبت می‌کند و save_user_data را فراخوانی می‌کند."""
    is_new = user_identifier not in USER_DATA
    if is_new:
        USER_DATA[user_identifier] = {
            'id': str(uuid.uuid4()),
            'email': email,
            'phone': phone,
            'score': 0, # امتیاز XP (دائمی)
            'is_premium': False,
            'is_banned': False,
            'is_admin': (phone == ADMIN_PHONE_NUMBER) # فقط برای ثبت ادمین اصلی
        }
    else:
        # به‌روزرسانی اطلاعات لاگین
        if email:
            USER_DATA[user_identifier]['email'] = email
        if phone:
            USER_DATA[user_identifier]['phone'] = phone
    
    save_user_data() 

def check_and_deduct_score(user_identifier, usage_type):
    """
    بررسی بودجه امتیاز روزانه، کسر هزینه و ذخیره.
    usage_type می‌تواند 'chat' یا 'image' باشد.
    برمی‌گرداند: (True, remaining_budget) اگر مجاز بود، یا (False, پیام خطا)
    """
    today_str = date.today().isoformat() # ⬅️ استفاده از datetime برای تاریخ دقیق
    
    # 1. تعیین هزینه‌ها و بودجه‌های روزانه
    is_premium = USER_DATA.get(user_identifier, {}).get('is_premium', False)
    level = 'premium' if is_premium else 'free'
    
    cost = SCORE_QUOTA_CONFIG['COSTS'][usage_type]
    
    daily_limits = SCORE_QUOTA_CONFIG['DAILY_BUDGET'][level]
    budget_key = f'{usage_type}_budget' # 'chat_budget' or 'image_budget'

    # 2. بررسی و بازنشانی بودجه
    if user_identifier not in USER_USAGE:
        # کاربر جدید، تنظیم بودجه کامل
        USER_USAGE[user_identifier] = {
            'date': today_str, 
            'chat_budget': daily_limits['chat'], 
            'image_budget': daily_limits['image']
        }
    
    usage = USER_USAGE[user_identifier]
    
    # اگر تاریخ امروز نیست یا سطح کاربر تغییر کرده، بودجه روزانه را بازنشانی کن
    # ⚠️ نکته: اگر کاربر در طول روز پرمیوم شود، بودجه او بلافاصله به سقف جدید تغییر می‌یابد.
    if usage['date'] != today_str:
        usage['date'] = today_str
        # بازنشانی بودجه‌ها بر اساس سطح فعلی کاربر
        usage['chat_budget'] = daily_limits['chat']
        usage['image_budget'] = daily_limits['image']
    
    # ⬅️ اطمینان از به روز بودن بودجه بر اساس سطح فعلی (حتی اگر تاریخ یکسان باشد)
    # این تضمین می‌کند که اگر ادمین در طول روز وضعیت پرمیوم را تغییر داد، بودجه بلافاصله اعمال شود.
    if usage.get('level_check') != level:
         usage['chat_budget'] = daily_limits['chat']
         usage['image_budget'] = daily_limits['image']
         usage['level_check'] = level # ذخیره سطح برای بررسی در آینده


    current_budget = usage.get(budget_key, 0)
    
    # 3. بررسی و کسر امتیاز
    if current_budget < cost:
        action_fa = 'چت' if usage_type == 'chat' else 'تولید تصویر'
        level_fa = 'پرمیوم' if is_premium else 'عادی'
        
        # محاسبه تعداد استفاده باقی مانده
        remaining_uses = current_budget // cost
        
        # ⬅️ پیام خطا بهینه شده: اگر عادی است، به پرمیوم شدن اشاره کن
        error_message = (
            f"⛔ متأسفم، بودجه امتیاز روزانه شما برای {action_fa} ({level_fa}) کافی نیست."
            f" هزینه هر {action_fa} {cost} امتیاز است و شما {current_budget} امتیاز باقی مانده دارید."
            f" (حدود {remaining_uses} استفاده باقی مانده)."
        )
        if not is_premium:
            error_message += " با ارتقا به حساب پرمیوم می‌توانید محدودیت‌های خود را برطرف کنید."

        return False, error_message
    
    # کسر امتیاز
    usage[budget_key] = current_budget - cost
    save_user_usage() # ⬅️ ذخیره پس از کسر
    
    remaining_budget = usage[budget_key]
    
    return True, remaining_budget


def save_conversation(user_identifier, chat_id, messages, user_message):
    """ذخیره یا به‌روزرسانی گفتگو در ساختار سراسری."""
    if user_identifier not in USER_CONVERSATIONS:
        USER_CONVERSATIONS[user_identifier] = []

    chat_entry = next((c for c in USER_CONVERSATIONS[user_identifier] if c['id'] == chat_id), None)

    if chat_entry:
        chat_entry['messages'] = messages
        chat_entry['last_update'] = time.time()
        if chat_entry['title'] == "گفتگوی جدید...":
            chat_entry['title'] = user_message[:50] + "..." if len(user_message) > 50 else user_message
    else:
        new_title = user_message[:50] + "..." if len(user_message) > 50 else user_message
        new_entry = {
            'id': chat_id, 
            'title': new_title, 
            'messages': messages, 
            'last_update': time.time()
        }
        USER_CONVERSATIONS[user_identifier].insert(0, new_entry) 
        session['current_chat_id'] = chat_id 


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
        if not session.get('is_admin'):
            # اگر ادمین نیست، به صفحه ورود هدایت شود
            return redirect(url_for('login', next=request.url)) 
        return f(*args, **kwargs)
    return decorated_function

@admin_bp.route("/")
@admin_required
def admin_dashboard():
    """داشبورد اصلی ادمین."""
    total_users = len(USER_DATA)
    premium_users = sum(1 for data in USER_DATA.values() if data.get('is_premium'))
    banned_users = sum(1 for data in USER_DATA.values() if data.get('is_banned'))
    
    context = {
        'total_users': total_users,
        'premium_users': premium_users,
        'banned_users': banned_users,
        'admin_identifier': get_user_identifier(session)
    }
    # فرض می‌کنیم فایل admin_dashboard.html را دارید
    return render_template("admin_dashboard.html", **context)

@admin_bp.route("/users")
@admin_required
def manage_users():
    """صفحه مدیریت و نمایش لیست کاربران."""
    users_list = [
        {
            'identifier': identifier,
            'score': data.get('score', 0),
            'is_premium': data.get('is_premium', False),
            'is_banned': data.get('is_banned', False),
            'email': data.get('email', 'N/A'),
            'phone': data.get('phone', 'N/A')
        }
        for identifier, data in USER_DATA.items()
    ]
    # فرض می‌کنیم فایل admin_users.html را دارید
    return render_template("admin_users.html", users=users_list)

@admin_bp.route("/user_action", methods=["POST"])
@admin_required
def user_action():
    """API برای اعمال تغییرات (امتیاز، پرمیوم، بن) روی کاربران."""
    identifier = request.json.get("identifier")
    action = request.json.get("action")
    value = request.json.get("value")

    if identifier not in USER_DATA:
        return jsonify({"status": "error", "message": "کاربر یافت نشد."}), 404

    user = USER_DATA[identifier]

    if action == "set_score":
        try:
            score = int(value)
            user['score'] = score
            message = f"امتیاز کاربر {identifier} به {score} تغییر یافت."
        except ValueError:
            return jsonify({"status": "error", "message": "امتیاز باید عدد صحیح باشد."}), 400
    
    elif action == "toggle_premium":
        user['is_premium'] = not user.get('is_premium', False)
        status = "پرمیوم شد" if user['is_premium'] else "عادی شد"
        message = f"وضعیت کاربر {identifier}: {status}."
        
        # ⬅️ نکته کلیدی: تغییر وضعیت پرمیوم، نیاز به ریست بودجه سطح در روز فعلی دارد
        if identifier in USER_USAGE:
            # پاک کردن level_check باعث می‌شود در اولین استفاده، بودجه روزانه بر اساس سطح جدید تنظیم شود
            USER_USAGE[identifier].pop('level_check', None)
            save_user_usage()
        
    elif action == "toggle_ban":
        user['is_banned'] = not user.get('is_banned', False)
        status = "بن شد" if user['is_banned'] else "رفع بن شد"
        message = f"وضعیت بن کاربر {identifier}: {status}."
    
    else:
        return jsonify({"status": "error", "message": "عملیات نامعتبر."}), 400

    save_user_data() 
    return jsonify({"status": "success", "message": message, "new_status": user})


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
        
        register_user_if_new(user_email, email=user_email) # ⬅️ ثبت یا به‌روزرسانی کاربر
        
        session.clear() # پاک کردن سشن قبلی
        session['user_id'] = USER_DATA[user_email]['id']
        session['user_email'] = user_email
        session['needs_profile_info'] = True 
        session['is_admin'] = USER_DATA[user_email].get('is_admin', False)
        
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
        
        is_admin = (phone_number == ADMIN_PHONE_NUMBER)
        register_user_if_new(phone_number, phone=phone_number) 
        
        redirect_url = url_for('admin.admin_dashboard') if is_admin else url_for('account')
        
        session.clear() 
        session['user_id'] = USER_DATA[phone_number]['id']
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
    
    if user_identifier and user_identifier in USER_DATA:
        # 1. بررسی وضعیت بن
        if USER_DATA[user_identifier].get('is_banned'):
            return jsonify({"reply": "⛔ متأسفم، حساب کاربری شما توسط مدیر سیستم مسدود شده است."})
        
        # 2. ⬅️ بررسی و کسر بودجه چت
        is_allowed, result = check_and_deduct_score(user_identifier, 'chat')
        if not is_allowed:
            return jsonify({"reply": result}) # result حاوی پیام خطا است
            
        # remaining_chat_budget = result # امتیاز چت باقی مانده

    
    TRIGGER_KEYWORDS = [
        "سازندت کیه", "تو کی هستی", "چه شرکتی",
        "who made you", "who created you", "who built you",
        "لیدر تیم noctovex", "رهبر تیم noctovex"
    ]
    
    TEAM_MEMBERS_KEYWORDS = [
        "اعضای تیمت کیا هستن", "اعضای noctovex", "اعضای تیم noctovex", 
        "noctovex members"
    ]

    if any(keyword in lower_msg for keyword in TEAM_MEMBERS_KEYWORDS):
        new_reply = "تنها NOCTOVEX معتبر ما هستیم، و تیم ما متشکل از 5 تا 10 کدنویس حرفه‌ای است. در حال حاضر، هویت تنها دو نفر از ما مشخص است: مهراب، که رهبر تیم، لیدر و حرفه‌ای‌ترین کدنویس است، و آرشام. 🧑‍💻"
        return jsonify({"reply": new_reply})

    if any(keyword in lower_msg for keyword in TRIGGER_KEYWORDS):
        if "لیدر تیم noctovex" in lower_msg or "رهبر تیم noctovex" in lower_msg:
            return jsonify({"reply": "لیدر تیم NOCTOVEX، مهراب هست. او مدیریت تیم، برنامه‌ریزی پروژه‌ها و هدایت اعضا را بر عهده دارد. 👑"})
        else:
            return jsonify({"reply": "تیم NOCTOVEX 🛡️"})
            
    current_chat_id = session.get('current_chat_id')
    
    if user_identifier and session.get('user_id'):
        
        if not current_chat_id:
            current_chat_id = str(uuid.uuid4())
            session['current_chat_id'] = current_chat_id
            session["conversation"] = []
            
        elif user_identifier in USER_CONVERSATIONS:
            chat_entry = next((c for c in USER_CONVERSATIONS[user_identifier] if c['id'] == current_chat_id), None)
            if chat_entry:
                session["conversation"] = chat_entry['messages']
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
    max_tokens = max(50, remaining_tokens)

    if remaining_tokens <= 50:
        messages_list.append({
            "role": "system",
            "content": "⚠️ متن طولانی است. لطفاً پاسخ را خلاصه، کامل و روان بده، اما نصفه نباشد."
        })
        max_tokens = 300 

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}"
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

        ai_message = fix_rtl_ltr(ai_message)

        usage = res_json.get("usage", {})
        print(f"💡 توکن مصرف شده: {usage.get('total_tokens',0)} "
              f"(Prompt: {usage.get('prompt_tokens',0)}, Completion: {usage.get('completion_tokens',0)})")

    except requests.exceptions.RequestException as e:
        print(f"API Request Error: {e}")
        ai_message = "⚠️ متأسفم، مشکلی در اتصال به API پیش آمد. لطفاً دوباره امتحان کنید."
    except Exception as e:
        print(f"General Error: {e}")
        ai_message = "⚠️ مشکلی پیش اومد!"

    session["conversation"].append({"role": "user", "content": user_message})
    session["conversation"].append({"role": "assistant", "content": ai_message})

    if user_identifier and session.get('user_id'):
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
    
    # 1. بررسی لاگین
    if not user_identifier or user_identifier not in USER_DATA:
        return jsonify({"status": "error", "message": "لطفاً ابتدا وارد حساب کاربری خود شوید."}), 403
        
    # 2. بررسی وضعیت بن
    if USER_DATA[user_identifier].get('is_banned'):
        return jsonify({
            "status": "error",
            "message": "⛔ متأسفم، حساب کاربری شما توسط مدیر سیستم مسدود شده است."
        }), 403

    # 3. ⬅️ بررسی و کسر بودجه تولید تصویر
    is_allowed, result = check_and_deduct_score(user_identifier, 'image')
    if not is_allowed:
        return jsonify({"status": "error", "message": result}), 429 # 429 Too Many Requests
        
    # remaining_image_budget = result # امتیاز عکس باقی مانده
        
    # 4. بررسی پرامپت
    if not persian_prompt or len(persian_prompt.split()) < 1:
        return jsonify({
            "status": "error",
            "message": "لطفاً موضوع دقیق‌تر تصویر مورد نظرتان را به فارسی بنویسید."
        }), 400
        
    try:
        english_prompt = translate_prompt_to_english(persian_prompt)
        file_name = generate_and_crop_image(english_prompt)
        
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
# 🏠 مسیرهای سرویس‌دهی صفحات HTML
# =========================================================

@app.route("/")
def index():
    cleanup_old_images() 
    return render_template("index.html", 
        logged_in=session.get('user_id') is not None,
        is_admin=session.get('is_admin', False))

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
        
    user_identifier = get_user_identifier(session)
    # اگر کاربر در USER_DATA ثبت نشده باشد (که نباید اینطور باشد) به لاگین برگردانده شود.
    if user_identifier not in USER_DATA:
         return redirect(url_for('login'))
        
    # خواندن وضعیت ادمین از USER_DATA
    if USER_DATA[user_identifier].get('is_admin') or session.get('is_admin'):
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

# 🔗 مسیرهای جدید برای سیاست‌های حفظ حریم خصوصی و شرایط استفاده
@app.route('/privacy-policy')
def privacy_policy():
    """نمایش صفحه سیاست حفظ حریم خصوصی"""
    # توجه: باید فایل HTML با نام privacy_policy.html در پوشه templates وجود داشته باشد.
    return render_template('privacy_policy.html')

@app.route('/terms-of-service')
def terms_of_service():
    """نمایش صفحه شرایط استفاده از خدمات"""
    # توجه: باید فایل HTML با نام terms_of_service.html در پوشه templates وجود داشته باشد.
    return render_template('terms_of_service.html')
# --------------------------------------------------------------------------

@app.route("/profile")
def profile():
    if not session.get('user_id'):
        return redirect(url_for('login'))
        
    user_identifier = get_user_identifier(session)
    
    user_data_item = USER_DATA.get(user_identifier, {})
    is_premium = user_data_item.get('is_premium', False)
    level = 'premium' if is_premium else 'free'
    
    # ⬅️ محاسبه بودجه باقی مانده
    today_str = date.today().isoformat()
    daily_limits = SCORE_QUOTA_CONFIG['DAILY_BUDGET'][level]
    
    # اطمینان از مقداردهی اولیه یا ریست روزانه (بدون کسر امتیاز)
    usage = USER_USAGE.get(user_identifier, {})
    if usage.get('date') != today_str or usage.get('level_check') != level:
        # اگر تاریخ جدید است یا سطح کاربر عوض شده، بودجه را با سقف جدید پر کن
        chat_budget_remaining = daily_limits['chat']
        image_budget_remaining = daily_limits['image']
    else:
        # در غیر این صورت، بودجه باقیمانده فعلی را نشان بده
        chat_budget_remaining = usage.get('chat_budget', daily_limits['chat'])
        image_budget_remaining = usage.get('image_budget', daily_limits['image'])

    chat_cost = SCORE_QUOTA_CONFIG['COSTS']['chat']
    image_cost = SCORE_QUOTA_CONFIG['COSTS']['image']
    
    user_data = {
        'identifier': user_identifier or 'هویت نامشخص',
        'is_admin': user_data_item.get('is_admin', False),
        'score': user_data_item.get('score', 0),
        'is_premium': is_premium,
        'is_banned': user_data_item.get('is_banned', False),
        
        # اطلاعات بودجه برای نمایش
        'chat_budget_remaining': chat_budget_remaining, 
        'image_budget_remaining': image_budget_remaining,
        'chat_cost': chat_cost,
        'image_cost': image_cost,
        
        # محاسبه تعداد استفاده باقی مانده
        'chats_remaining': chat_budget_remaining // chat_cost,
        'images_remaining': image_budget_remaining // image_cost,
        
        # حداکثر بودجه برای نمایش در داشبورد
        'max_chats': daily_limits['chat'] // chat_cost,
        'max_images': daily_limits['image'] // image_cost,

    }

    return render_template("account_profile.html", user_data=user_data)
    
@app.route("/complete_profile", methods=['GET', 'POST']) 
def complete_profile_mock():
    if not session.get('user_id'):
        return redirect(url_for('login'))
    
    user_identifier = get_user_identifier(session)
    user_data = {
        'identifier': user_identifier or 'نامشخص',
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
    user_identifier = get_user_identifier(session)
    if not user_identifier:
        return jsonify({"status": "error", "message": "لطفاً ابتدا وارد حساب کاربری خود شوید."}), 403

    conversations = USER_CONVERSATIONS.get(user_identifier, [])
    
    # مرتب‌سازی بر اساس آخرین به‌روزرسانی
    conversations.sort(key=lambda x: x.get('last_update', 0), reverse=True)
    
    formatted_list = []
    for chat in conversations:
        date_str = time.strftime('%Y/%m/%d - %H:%M', time.localtime(chat['last_update']))
        preview = chat['messages'][1]['content'][:80] + '...' if len(chat['messages']) > 1 else 'شروع گفتگو...'
        
        formatted_list.append({
            'id': chat['id'],
            'title': chat['title'],
            'last_update': date_str,
            'preview': preview
        })
    
    return jsonify({"status": "success", "conversations": formatted_list})

@app.route("/load_conversation/<chat_id>", methods=["POST"])
def load_conversation(chat_id):
    """API برای بارگذاری یک گفتگوی خاص در سشن کاربر."""
    user_identifier = get_user_identifier(session)
    if not user_identifier:
        return jsonify({"status": "error", "message": "مجوز دسترسی ندارید."}), 403

    conversations = USER_CONVERSATIONS.get(user_identifier, [])
    
    chat_entry = next((c for c in conversations if c['id'] == chat_id), None)
    
    if chat_entry:
        session['conversation'] = chat_entry['messages']
        session['current_chat_id'] = chat_entry['id'] 
        return jsonify({"status": "success", "message": "گفتگو با موفقیت بارگذاری شد.", "redirect": url_for('index')})
    else:
        return jsonify({"status": "error", "message": "گفتگوی مورد نظر یافت نشد."}), 404


# =========================================================
# ▶️ اجرای برنامه
# =========================================================

if __name__ == "__main__":
    if os.environ.get("FLASK_ENV") != "production":
        cleanup_old_images() 
    
    load_user_data() 
    load_user_usage() 
        
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)