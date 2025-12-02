import os
from flask import Flask, render_template, request, jsonify, session
import requests
import tiktoken
import re

app = Flask(__name__)
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

def count_tokens(messages):
    return sum(len(encoder.encode(m["content"])) for m in messages)

def fix_rtl_ltr(text):
    """
    ترکیب فارسی و انگلیسی را درست می‌کند.
    انگلیسی و Markdown مثل ** یا ` را LTR و فارسی را RTL قرار می‌دهد.
    """
    def replacer(match):
        content = match.group(0)
        if re.search(r'[a-zA-Z0-9]', content):
            return f"\u200E{content}"  # LTR برای متن انگلیسی
        return f"\u200F{content}"      # RTL برای متن فارسی
    # جدا کردن خطوط و اعمال LTR/RTL
    lines = text.split("\n")
    fixed_lines = []
    for line in lines:
        line = line.strip()
        if line:
            # جدا کردن کلمات انگلیسی داخل متن فارسی
            fixed_line = re.sub(r'[\w\*\`]+', replacer, line)
            fixed_lines.append(fixed_line)
    return "\n".join(fixed_lines)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/chat", methods=["POST"])
def chat():
    user_message = request.json.get("message", "")
    lower_msg = user_message.lower()

    # پاسخ ثابت برای سوالات سازنده و لیدر
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

    # تریم conversation تا prompt < 500 توکن شود
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
        max_tokens = 300  # حداقل 300 توکن برای پاسخ خلاصه

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

@app.route("/clear", methods=["POST"])
def clear():
    session["conversation"] = []
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
