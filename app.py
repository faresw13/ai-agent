from flask import Flask, request, jsonify
import os

app = Flask(__name__)

# Temporary storage for testing.
# We'll move this to a real database before production.
tokens = {}


@app.route("/", methods=["GET"])
def home():
    return "AI Agent is running"


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}

    event = data.get("event")
    merchant = data.get("merchant")

    # Never print access_token or refresh_token.
    print(f"Salla webhook received: event={event}, merchant={merchant}")

    if event == "app.store.authorize":
        payload = data.get("data", {})

        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        expires = payload.get("expires")

        if access_token and merchant:
            tokens[str(merchant)] = {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires": expires,
            }

            print(f"Salla authorization saved for merchant={merchant}")

    elif event == "app.uninstalled":
        if merchant:
            tokens.pop(str(merchant), None)
            print(f"Salla authorization removed for merchant={merchant}")

    return jsonify({"success": True}), 200


@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "connected_merchants": list(tokens.keys())
    }), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
