from flask import Flask, request, jsonify
import os
import json
import urllib.request
import psycopg

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# =========================================================
# Database
# =========================================================

def get_db():
    return psycopg.connect(DATABASE_URL)


def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS salla_tokens (
                    merchant_id TEXT PRIMARY KEY,
                    access_token TEXT NOT NULL,
                    refresh_token TEXT,
                    expires BIGINT
                )
            """)
        conn.commit()


# =========================================================
# Helpers
# =========================================================

def salla_headers(access_token):
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/152.0.0.0 Safari/537.36"
        ),
        "Accept-Language": (
            "ar-SA,ar;q=0.9,en-US;q=0.8,en;q=0.7"
        )
    }


def get_connected_store():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT merchant_id, access_token
                FROM salla_tokens
                LIMIT 1
            """)
            return cur.fetchone()


def get_store_info(access_token):
    req = urllib.request.Request(
        "https://api.salla.dev/admin/v2/store/info",
        headers=salla_headers(access_token)
    )

    with urllib.request.urlopen(req, timeout=15) as response:
        result = json.loads(
            response.read().decode("utf-8")
        )

    return result.get("data", {})


def extract_openai_text(result):
    # Some Responses API responses include output_text
    if result.get("output_text"):
        return result["output_text"]

    # Fallback: extract text from output blocks
    texts = []

    for item in result.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                text = content.get("text")
                if text:
                    texts.append(text)

    if texts:
        return "\n".join(texts)

    return "ما قدرت أستخرج رد الذكاء الاصطناعي."


# =========================================================
# Basic routes
# =========================================================

@app.route("/", methods=["GET"])
def home():
    return "AI Agent is running"


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok"
    }), 200


# =========================================================
# Salla Webhook
# =========================================================

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}

    event = data.get("event")
    merchant = data.get("merchant")

    print(
        f"Salla webhook received: "
        f"event={event}, merchant={merchant}"
    )

    if event == "app.store.authorize":
        payload = data.get("data", {})

        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        expires = payload.get("expires")

        if access_token and merchant:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO salla_tokens
                        (
                            merchant_id,
                            access_token,
                            refresh_token,
                            expires
                        )
                        VALUES (%s, %s, %s, %s)

                        ON CONFLICT (merchant_id)
                        DO UPDATE SET
                            access_token = EXCLUDED.access_token,
                            refresh_token = EXCLUDED.refresh_token,
                            expires = EXCLUDED.expires
                    """, (
                        str(merchant),
                        access_token,
                        refresh_token,
                        expires
                    ))

                conn.commit()

            print(
                f"Salla authorization saved "
                f"for merchant={merchant}"
            )

    elif event == "app.uninstalled":
        if merchant:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        DELETE FROM salla_tokens
                        WHERE merchant_id = %s
                        """,
                        (str(merchant),)
                    )

                conn.commit()

            print(
                f"Salla authorization removed "
                f"for merchant={merchant}"
            )

    return jsonify({
        "success": True
    }), 200


# =========================================================
# Connected stores
# =========================================================

@app.route("/status", methods=["GET"])
def status():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT merchant_id
                FROM salla_tokens
            """)

            merchants = [
                row[0]
                for row in cur.fetchall()
            ]

    return jsonify({
        "connected_merchants": merchants
    }), 200


# =========================================================
# Test Salla connection
# =========================================================

@app.route("/test-salla", methods=["GET"])
def test_salla():
    row = get_connected_store()

    if not row:
        return jsonify({
            "success": False,
            "message": "No authorized Salla store"
        }), 404

    merchant, access_token = row

    try:
        store = get_store_info(access_token)

        return jsonify({
            "success": True,
            "store_id": store.get("id"),
            "store_name": store.get("name")
        }), 200

    except Exception as e:
        print(
            f"Salla API error: "
            f"{type(e).__name__}"
        )

        return jsonify({
            "success": False,
            "message": "Salla API request failed"
        }), 500


# =========================================================
# AI AGENT - Store Information
# =========================================================

@app.route("/agent/store-info", methods=["GET"])
def agent_store_info():
    row = get_connected_store()

    if not row:
        return jsonify({
            "success": False,
            "message": "No authorized Salla store"
        }), 404

    merchant_id, access_token = row

    try:
        store = get_store_info(access_token)

        return jsonify({
            "success": True,
            "merchant_id": merchant_id,
            "store": store
        }), 200

    except Exception as e:
        print(
            f"Agent store info error: "
            f"{type(e).__name__}"
        )

        return jsonify({
            "success": False,
            "message": (
                "Failed to read "
                "Salla store information"
            )
        }), 500


# =========================================================
# AI AGENT - Chat
# =========================================================

@app.route("/agent/chat", methods=["POST"])
def agent_chat():

    if not OPENAI_API_KEY:
        return jsonify({
            "success": False,
            "message": "OPENAI_API_KEY is not configured"
        }), 500

    row = get_connected_store()

    if not row:
        return jsonify({
            "success": False,
            "message": "No authorized Salla store"
        }), 404

    merchant_id, access_token = row

    data = request.get_json(silent=True) or {}

    messages = data.get("messages", [])

    if not isinstance(messages, list):
        return jsonify({
            "success": False,
            "message": "messages must be an array"
        }), 400

    try:
        store = get_store_info(access_token)

        store_context = {
            "id": store.get("id"),
            "name": store.get("name"),
            "description": store.get("description"),
            "currency": store.get("currency"),
            "domain": store.get("domain"),
            "email": store.get("email"),
            "status": store.get("status"),
            "plan": store.get("plan"),
        }

        system_instructions = """
أنت Fares AI، وكيل ذكي متخصص في إدارة وتطوير متاجر سلة.

مهمتك الأساسية مساعدة صاحب المتجر في:
- تصميم المتجر
- ترتيب الصفحة الرئيسية
- إنشاء الأقسام
- تجهيز المنتجات
- كتابة أسماء المنتجات
- كتابة أوصاف المنتجات
- كتابة المحتوى التسويقي
- اقتراح الهوية والألوان
- تجهيز البنرات
- تطوير تجربة المتجر

أنت تعمل داخل لوحة تحكم سلة.

في الوقت الحالي أنت في مرحلة المحادثة والتخطيط.
لا تدّعي أنك نفذت أي تعديل إذا لم يتم تنفيذه فعليًا.

إذا طلب المستخدم تنفيذ شيء:
1. افهم المطلوب.
2. حلل حالة المتجر المتاحة لك.
3. اقترح خطة واضحة.
4. لاحقًا سيتم استخدام أدوات Salla لتنفيذ الخطة.

تحدث بالعربية السعودية وبأسلوب واضح ومباشر.

بيانات المتجر الحالية:
""" + json.dumps(
            store_context,
            ensure_ascii=False
        )

        conversation = []

        for message in messages[-20:]:
            role = message.get("role")

            if role not in ["user", "assistant"]:
                continue

            content = message.get("content", "")

            if not content:
                continue

            conversation.append({
                "role": role,
                "content": content
            })

        payload = {
            "model": "gpt-5.6-luna",
            "instructions": system_instructions,
            "input": conversation
        }

        req = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(
                payload,
                ensure_ascii=False
            ).encode("utf-8"),
            headers={
                "Authorization": (
                    f"Bearer {OPENAI_API_KEY}"
                ),
                "Content-Type": "application/json",
                "Accept": "application/json"
            },
            method="POST"
        )

        with urllib.request.urlopen(
            req,
            timeout=60
        ) as response:

            result = json.loads(
                response.read().decode("utf-8")
            )

        ai_text = extract_openai_text(result)

        return jsonify({
            "success": True,
            "merchant_id": merchant_id,
            "message": ai_text
        }), 200

    except urllib.error.HTTPError as e:

        error_body = ""

        try:
            error_body = (
                e.read()
                .decode("utf-8")
            )
        except Exception:
            pass

        print(
            f"OpenAI HTTP error: "
            f"{e.code} {error_body}"
        )

        return jsonify({
            "success": False,
            "message": "AI request failed"
        }), 500

    except Exception as e:

        print(
            f"AI chat error: "
            f"{type(e).__name__}: {e}"
        )

        return jsonify({
            "success": False,
            "message": "AI request failed"
        }), 500


# =========================================================
# Start application
# =========================================================

init_db()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=10000
    )
