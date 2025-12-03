import os
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
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

# =========================================================
# 🛠️ تنظیمات اولیه و ذخیره‌سازهای موقت
# =========================================================
app = Flask(__name__)

# --- تنظیمات ضروری ---
app.jinja_env.charset = 'utf-8'
app.secret_key = "supersecretkey123" 

API_KEY = os.getenv("OPENROUTER_API_KEY")
if not API_KEY:
    # این فقط برای محیط محلی است. در محیط‌های واقعی باید از متغیر محیطی استفاده کنید.
    # به عنوان مثال: API_KEY = "YOUR_FALLBACK_API_KEY"
    raise ValueError("❌ متغیر محیطی OPENROUTER_API_KEY پیدا نشد! لطفاً آن را تنظیم کنید.")

# ----------------- 📧 تنظیمات Flask-Mail -----------------
# توجه: این تنظیمات باید با اطلاعات جیمیل واقعی شما جایگزین شوند.
app.config['MAIL_SERVER']='smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USERNAME'] = 'noctovex@gmail.com'
app.config['MAIL_PASSWORD'] = 'valh wehv jnqp sgsa' # رمز عبور اپلیکیشن (App Password)
app.config['MAIL_USE_TLS'] = True    # ⬅️ باید True باشد
app.config['MAIL_USE_SSL'] = False   # ⬅️ باید False باشد
mail = Mail(app)

verification_codes = {} 

# 💡 ساختار جدید برای ذخیره دائم گفتگوها (شبیه‌سازی پایگاه داده)
# { 'user_email': [ {id: uuid, title: str, messages: [msgs...], last_update: timestamp}, ... ] }
USER_CONVERSATIONS = {} 
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
# ⚙️ توابع احراز هویت و ایمیل
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

# =========================================================
# ⚙️ توابع کمکی و ذخیره‌سازی گفتگو 
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

def save_conversation(user_email, chat_id, messages, user_message):
    """ذخیره یا به‌روزرسانی گفتگو در ساختار سراسری."""
    if user_email not in USER_CONVERSATIONS:
        USER_CONVERSATIONS[user_email] = []

    # جستجوی گفتگوی موجود
    chat_entry = next((c for c in USER_CONVERSATIONS[user_email] if c['id'] == chat_id), None)

    if chat_entry:
        chat_entry['messages'] = messages
        chat_entry['last_update'] = time.time()
        # اگر عنوان هنوز موقت است، آن را با اولین پیام کاربر به‌روز کنید
        if chat_entry['title'] == "گفتگوی جدید...":
            # استفاده از 50 کاراکتر اول کاربر به عنوان عنوان
            chat_entry['title'] = user_message[:50] + "..." if len(user_message) > 50 else user_message
    else:
        # گفتگوی جدید
        new_title = user_message[:50] + "..." if len(user_message) > 50 else user_message
        new_entry = {
            'id': chat_id, 
            'title': new_title, 
            'messages': messages, 
            'last_update': time.time()
        }
        # گفتگوهای جدید را به بالای لیست اضافه کنید
        USER_CONVERSATIONS[user_email].insert(0, new_entry) 
        session['current_chat_id'] = chat_id # اطمینان از به روز بودن شناسه در سشن


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
# 📧 مسیرهای احراز هویت
# =========================================================

@app.route("/send_code", methods=["POST"])
def send_code():
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
        
        session['user_id'] = str(uuid.uuid4())
        session['user_email'] = user_email
        session['needs_profile_info'] = True 
        
        # هدایت به مسیر account که در نهایت به complete_profile می‌رود.
        return jsonify({"status": "success", "redirect": url_for('account')})
    else:
        return jsonify({"status": "error", "message": "کد وارد شده صحیح نیست."}), 400


# =========================================================
# 💬 مسیر چت 
# =========================================================

@app.route("/chat", methods=["POST"])
def chat():
    user_message = request.json.get("message", "")
    lower_msg = user_message.lower()

    if not user_message.strip():
        return jsonify({"reply": "لطفاً پیامی ارسال کنید."})

    TRIGGER_KEYWORDS = [
        "سازندت کیه", "تو کی هستی", "چه شرکتی",
        "who made you", "who created you", "who built you",
        "لیدر تیم noctovex", "رهبر تیم noctovex"
    ]
    
    TEAM_MEMBERS_KEYWORDS = [
        "اعضای تیمت کیا هستن", "اعضای noctovex", "اعضای تیم noctovex", 
        "noctovex members"
    ]

    # --- منطق پاسخگویی جدید به اعضای تیم ---
    if any(keyword in lower_msg for keyword in TEAM_MEMBERS_KEYWORDS):
        new_reply = "تنها NOCTOVEX معتبر ما هستیم، و تیم ما متشکل از 5 تا 10 کدنویس حرفه‌ای است. در حال حاضر، هویت تنها دو نفر از ما مشخص است: مهراب، که رهبر تیم، لیدر و حرفه‌ای‌ترین کدنویس است، و آرشام. 🧑‍💻"
        return jsonify({"reply": new_reply})

    # --- منطق پاسخگویی به سازنده و رهبر تیم ---
    if any(keyword in lower_msg for keyword in TRIGGER_KEYWORDS):
        if "لیدر تیم noctovex" in lower_msg or "رهبر تیم noctovex" in lower_msg:
            return jsonify({"reply": "لیدر تیم NOCTOVEX، مهراب هست. او مدیریت تیم، برنامه‌ریزی پروژه‌ها و هدایت اعضا را بر عهده دارد. 👑"})
        else:
            return jsonify({"reply": "تیم NOCTOVEX 🛡️"})
            
    # --- منطق بارگذاری و آماده‌سازی گفتگو ---
    current_chat_id = session.get('current_chat_id')
    
    if session.get('user_email') and session.get('user_id'):
        user_email = session['user_email']
        
        if not current_chat_id:
            # شروع یک چت جدید
            current_chat_id = str(uuid.uuid4())
            session['current_chat_id'] = current_chat_id
            session["conversation"] = []
            
        elif user_email in USER_CONVERSATIONS:
            # بارگذاری چت قبلی اگر شناسه در سشن هست
            chat_entry = next((c for c in USER_CONVERSATIONS[user_email] if c['id'] == current_chat_id), None)
            if chat_entry:
                session["conversation"] = chat_entry['messages']
            else:
                # شناسه در سشن هست اما در آرشیو نیست، چت جدید شروع شود
                session.pop('current_chat_id', None)
                session["conversation"] = []
                current_chat_id = str(uuid.uuid4())
                session['current_chat_id'] = current_chat_id
    else:
        # حالت مهمان: چت موقت و غیرقابل ذخیره
        session.pop('current_chat_id', None)
        if "conversation" not in session:
            session["conversation"] = []
    # ----------------------------------------
    
    messages_list = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages_list.extend(session.get("conversation", []))
    messages_list.append({"role": "user", "content": user_message})

    # --- منطق Truncation (حذف پیام‌های قدیمی برای ماندن در محدوده توکن) ---
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

    # --- فراخوانی API ---
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

    # --- ذخیره گفتگو در سشن و آرشیو ---
    session["conversation"].append({"role": "user", "content": user_message})
    session["conversation"].append({"role": "assistant", "content": ai_message})

    if session.get('user_email') and session.get('user_id'):
        save_conversation(session['user_email'], session['current_chat_id'], session["conversation"], user_message)
    # ----------------------

    if len(session["conversation"]) > 50:
        session["conversation"] = session["conversation"][-50:]

    return jsonify({"reply": ai_message})


@app.route("/clear_history", methods=["POST"])
def clear_history():
    """شروع چت جدید با پاک کردن تاریخچه سشن و ID چت قبلی."""
    session["conversation"] = []
    session.pop('current_chat_id', None) # 💡 مهم: ID چت قبلی را پاک می‌کند
    return jsonify({"status": "History cleared successfully"})


# =========================================================
# 🖼️ مسیر تولید تصویر
# =========================================================

@app.route("/image_generator", methods=["POST"])
def image_generator():
    persian_prompt = request.json.get("prompt", "").strip()
    
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
    # فایل: index.html
    return render_template("index.html", logged_in=session.get('user_id') is not None)

@app.route("/image")
def image_page():
    """مسیر مربوط به image.html"""
    # فایل: image.html
    return render_template("image.html", logged_in=session.get('user_id') is not None)


# =========================================================
# 🎮 مسیرهای بازی
# =========================================================
@app.route("/game")
def game_center():
    """مسیر صفحه اصلی بازی و سرگرمی (منوی بازی‌ها)"""
    # فایل: game.html
    return render_template("game.html", logged_in=session.get('user_id') is not None)

@app.route("/game/car")
def car_game():
    """مسیر بازی ماشین (Drive Mad)"""
    # فایل: car_game.html
    return render_template("car_game.html", logged_in=session.get('user_id') is not None)

@app.route("/game/guess")
def guess_game():
    """مسیر بازی حدس عدد"""
    # فایل: number_guess_game.html
    return render_template("number_guess_game.html", logged_in=session.get('user_id') is not None)


# --- مسیرهای احراز هویت ---

@app.route("/login")
def login():
    """مسیر صفحه ورود سفارشی (account_login.html)."""
    if session.get('user_id'):
        return redirect(url_for('account'))
    # فایل: account_login.html
    return render_template("account_login.html") 

@app.route("/login_google")
def login_google():
    """مسیر ورود با گوگل - اضافه شد تا خطای 404 برطرف شود."""
    # فایل: account_login.html
    return redirect(url_for('login')) 
    
@app.route("/account")
def account():
    """مسیر صفحه حساب کاربری (account.html).
    ⭐️ اصلاح شد: پس از تکمیل پروفایل، به profile هدایت می‌شود."""
    if not session.get('user_id'):
        return redirect(url_for('login'))
        
    if session.get('needs_profile_info'):
        # اگر پرچم needs_profile_info هنوز وجود دارد، کاربر را به فرم تکمیل پروفایل بفرست
        return redirect(url_for('complete_profile_mock')) 
        
    # ⭐️ اصلاح: اگر پروفایل تکمیل شده باشد، مستقیماً به صفحه جزئیات پروفایل (account_profile.html) هدایت می‌شود.
    return redirect(url_for('profile'))


@app.route("/verify_page")
def verify_page():
    """مسیر صفحه وارد کردن کد تایید (account_verify.html)."""
    # فایل: account_verify.html
    return render_template("account_verify.html")

# --- مسیرهای تک صفحه‌ای ---

@app.route("/support")
def support():
    """مسیر صفحه پشتیبانی (support.html)."""
    # فایل: support.html
    return render_template("support.html")

@app.route("/about")
def about():
    """مسیر صفحه درباره ما (about.html)."""
    # فایل: about.html
    return render_template("about.html")

@app.route("/profile")
def profile():
    """مسیر صفحه جزئیات پروفایل (account_profile.html)."""
    if not session.get('user_id'):
        return redirect(url_for('login'))
        
    user_data = {
        'email': session.get('user_email', 'ایمیل پیدا نشد'),
    }
    # فایل: account_profile.html
    return render_template("account_profile.html", user_data=user_data)
    
@app.route("/complete_profile", methods=['GET', 'POST']) 
def complete_profile_mock():
    """صفحه تکمیل پروفایل (account_form.html)."""
    if not session.get('user_id'):
        return redirect(url_for('login'))
    
    user_email = session.get('user_email', 'ایمیل پیدا نشد')
    user_data = {
        'email': user_email,
    }
    
    if request.method == 'POST':
        # منطق پردازش فرم تکمیل پروفایل (POST)
        user_name = request.form.get('user_name') 
        user_phone = request.form.get('user_phone') 
        
        # پاک کردن پرچم نیاز به تکمیل پروفایل پس از ارسال موفقیت‌آمیز
        session.pop('needs_profile_info', None) 
        
        # هدایت کاربر به صفحه حساب کاربری (که اکنون به profile هدایت می‌شود)
        return redirect(url_for('account')) 

    # اگر متد GET باشد (برای نمایش فرم)
    # فایل: account_form.html
    return render_template("account_form.html", user_data=user_data) 

@app.route("/logout")
def logout():
    """مسیر خروج از حساب کاربری."""
    session.clear()
    return redirect(url_for('index')) 
    
# =========================================================
# 💾 مسیرهای آرشیو گفتگو 
# =========================================================

@app.route("/my_conversations")
def my_conversations():
    """نمایش صفحه آرشیو گفتگوها (my_conversations.html)."""
    if not session.get('user_id'):
        return redirect(url_for('login'))
    # فایل: my_conversations.html
    return render_template("my_conversations.html")

@app.route("/get_conversations_list", methods=["GET"])
def get_conversations_list():
    """API برای دریافت لیست گفتگوهای کاربر جاری."""
    if not session.get('user_email'):
        return jsonify({"status": "error", "message": "لطفاً ابتدا وارد حساب کاربری خود شوید."}), 403

    user_email = session['user_email']
    conversations = USER_CONVERSATIONS.get(user_email, [])
    
    formatted_list = []
    for chat in conversations:
        # تبدیل timestamp به تاریخ و زمان
        date_str = time.strftime('%Y/%m/%d - %H:%M', time.localtime(chat['last_update']))
        
        # نمایش پیش‌نمایش (پاسخ اول ربات)
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
    if not session.get('user_email'):
        return jsonify({"status": "error", "message": "مجوز دسترسی ندارید."}), 403

    user_email = session['user_email']
    conversations = USER_CONVERSATIONS.get(user_email, [])
    
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
        
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)