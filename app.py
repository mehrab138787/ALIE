import os
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import requests
import tiktoken
import re

# =========================================================
# 🛠️ تنظیمات اولیه و رفع خطای Encoding
# =========================================================
app = Flask(__name__)

# ✅ رفع خطای UnicodeDecodeError: 
# به Jinja2 می‌گوید که قالب‌ها (Templates) را همیشه با UTF-8 بخواند.
app.jinja_env.charset = 'utf-8'

# 💡 کلید محرمانه برای سشن‌ها (باید در محیط پروداکشن قوی‌تر شود)
app.secret_key = "supersecretkey123" 

API_KEY = os.getenv("OPENROUTER_API_KEY")
if not API_KEY:
    raise ValueError("❌ متغیر محیطی OPENROUTER_API_KEY پیدا نشد!")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL_NAME = "deepseek/deepseek-chat"

TRIGGER_KEYWORDS = [
    "سازندت کیه", "تو کی هستی", "چه شرکتی",
    "who made you", "who created you", "who built you",
    "لیدر تیم noctovex", "رهبر تیم noctovex"
]

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

TOTAL_TOKEN_LIMIT = 750  # سقف کل توکن
encoder = tiktoken.get_encoding("cl100k_base")


# =========================================================
# ⚙️ توابع کمکی
# =========================================================

def count_tokens(messages):
    """شمارش کل توکن‌های مصرف شده در لیست پیام‌ها"""
    return sum(len(encoder.encode(m["content"])) for m in messages)

def fix_rtl_ltr(text):
    """
    اصلاح ترکیب فارسی و انگلیسی (RTL/LTR).
    این تابع محتوای فارسی را با \u200F (RTL) و محتوای لاتین/اعداد/کد را با \u200E (LTR) احاطه می‌کند.
    """
    
    # 1. بخش‌های لاتین/کد/اعداد (که باید LTR بمانند) را با \u200E احاطه می‌کنیم
    # این الگو به دنبال کلمات لاتین، اعداد، کاراکترهای فنی یا ترکیبی از آنها می‌گردد
    def ltr_replacer(match):
        return f"\u200E{match.group(0)}\u200E"

    # این خط را برای حفظ LTR بودن بلاک‌های کد و کلمات انگلیسی اصلاح کردیم
    fixed_text = re.sub(r'([a-zA-Z0-9\/\.\-\_\=\+\(\)\{\}\[\]\*\`\:\<\>]+)', ltr_replacer, text)
    
    # 2. کل متن را با مارکر RTL آغاز می‌کنیم تا جهت اصلی فارسی باشد
    # و سپس هر خط را جداگانه بررسی می‌کنیم تا خطوطی که فقط شامل LTR هستند (مثلا بلاک کد تکی) به هم نریزند
    
    final_lines = []
    for line in fixed_text.split('\n'):
        # اگر خطی فقط شامل LTR markers و whitespace بود، نیازی به RTL marker ندارد (مثل بلاک‌های کد)
        if re.match(r'^[\s\u200E\u200F\*\-]*$', line):
            final_lines.append(line)
        else:
            # بقیه خطوط با \u200F (RTL) شروع شوند
            final_lines.append(f"\u200F{line}")

    return "\n".join(final_lines)


# =========================================================
# 🏠 مسیرهای سرویس‌دهی صفحات HTML
# =========================================================

@app.route("/")
def index():
    return render_template("index.html")

# 💡 مسیر 'حساب من' - با منطق ورود/خروج
@app.route("/account.html", methods=['GET', 'POST'])
def account():
    # بررسی وضعیت ورود کاربر از طریق سشن
    user_logged_in = session.get('user_id') is not None
    user_needs_info = session.get('needs_profile_info', False)
    
    if user_logged_in:
        # 🚨 داده‌های موقت (در واقعیت از دیتابیس خوانده می‌شوند)
        user_data = {
            'email': session.get('user_email', 'unknown@gmail.com'),
            'first_name': session.get('first_name'),
            'last_name': session.get('last_name'),
            'profession': session.get('profession'),
            'xp_score': 0, 
            'level': 1,
            'chats': 0,
        }
        
        # حالت ۲: لاگین شده اما نیاز به تکمیل اطلاعات دارد
        if user_needs_info:
              return render_template("account_form.html", user_data=user_data)
        
        # حالت ۳: لاگین شده و اطلاعاتش کامل است
        return render_template("account_profile.html", user_data=user_data)

    # حالت ۱: لاگین نیست
    return render_template("account_login.html")


@app.route("/support.html")
def support():
    return render_template("support.html")

@app.route("/about.html")
def about():
    return render_template("about.html")

# =========================================================
# 🌐 مسیرهای ورود/خروج موقت (جایگزین OAuth گوگل)
# =========================================================

@app.route("/login_google")
def login_mock():
    # === [شبیه سازی فرآیند OAuth گوگل] ===
    # این تابع کاربر را لاگین شده در نظر می‌گیرد و پرچم تکمیل اطلاعات را فعال می‌کند.
    session['user_id'] = 12345
    session['user_email'] = 'noctovex.user@gmail.com'
    session['needs_profile_info'] = True 
    # ===================================
    
    return redirect(url_for('account'))

@app.route("/complete_profile", methods=['POST'])
def complete_profile_mock():
    # ذخیره اطلاعات تکمیل شده کاربر در سشن (به جای دیتابیس)
    if not session.get('user_id'):
        return redirect(url_for('account'))
        
    session['first_name'] = request.form.get('first_name')
    session['last_name'] = request.form.get('last_name')
    session['dob'] = request.form.get('dob')
    session['profession'] = request.form.get('profession')
    
    # بعد از ذخیره، پرچم نیاز به اطلاعات را غیرفعال می‌کنیم
    session['needs_profile_info'] = False
    
    return redirect(url_for('account'))


@app.route("/logout")
def logout_mock():
    # خروج از حساب کاربری با پاک کردن سشن
    session.clear()
    return redirect(url_for('account'))


# =========================================================
# 💬 مسیر چت و منطق اصلی
# =========================================================

@app.route("/chat", methods=["POST"])
def chat():
    user_message = request.json.get("message", "")
    lower_msg = user_message.lower()

    # پاسخ ثابت برای کلمات کلیدی
    if any(keyword in lower_msg for keyword in TRIGGER_KEYWORDS):
        if "لیدر تیم noctovex" in lower_msg or "رهبر تیم noctovex" in lower_msg:
            return jsonify({"reply": "لیدر تیم NOCTOVEX، مهراب هست. او مدیریت تیم، برنامه‌ریزی پروژه‌ها و هدایت اعضا را بر عهده دارد. 👑"})
        else:
            return jsonify({"reply": "تیم NOCTOVEX 🛡️"})

    if "conversation" not in session:
        session["conversation"] = []

    messages_list = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages_list.extend(session.get("conversation", []))
    messages_list.append({"role": "user", "content": user_message})

    # مدیریت تاریخچه گفتگو و محدودیت توکن
    while count_tokens(messages_list) >= 500 and len(session["conversation"]) >= 2:
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
        "model": MODEL_NAME,
        "messages": messages_list,
        "max_tokens": max_tokens
    }

    try:
        response = requests.post(OPENROUTER_URL, json=data, headers=headers, timeout=10)
        res_json = response.json()
        ai_message = res_json["choices"][0]["message"]["content"]

        # ✅ استفاده از تابع اصلاح‌شده برای رفع مشکل RTL/LTR (و در نتیجه علامت سؤال)
        ai_message = fix_rtl_ltr(ai_message)

        usage = res_json.get("usage", {})
        print(f"💡 توکن مصرف شده: {usage.get('total_tokens',0)} "
              f"(Prompt: {usage.get('prompt_tokens',0)}, Completion: {usage.get('completion_tokens',0)})")

    except Exception as e:
        print("ERROR:", e)
        ai_message = "⚠️ مشکلی پیش اومد!"

    session["conversation"].append({"role": "user", "content": user_message})
    session["conversation"].append({"role": "assistant", "content": ai_message})

    if len(session["conversation"]) > 50:
        session["conversation"] = session["conversation"][-50:]

    return jsonify({"reply": ai_message})

@app.route("/clear_history", methods=["POST"])
def clear_history():
    """پاک کردن تاریخچه چت از سشن"""
    session["conversation"] = []
    return jsonify({"status": "History cleared successfully"})


# =========================================================
# ▶️ اجرای برنامه
# =========================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)