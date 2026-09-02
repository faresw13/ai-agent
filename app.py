from flask import Flask, request, jsonify

app = Flask(__name__)

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


@app.route("/test-salla", methods=["GET"])
def test_salla():
    import urllib.request
    import json

    if not tokens:
        return jsonify({
            "success": False,
            "message": "No authorized Salla store"
        }), 404

    merchant = next(iter(tokens))
    access_token = tokens[merchant]["access_token"]

    req = urllib.request.Request(
        "https://api.salla.dev/admin/v2/store/info",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json"
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))

        store = result.get("data", {})

        return jsonify({
            "success": True,
            "store_id": store.get("id"),
            "store_name": store.get("name")
        }), 200

    except Exception as e:
        print(f"Salla API error: {type(e).__name__}")

        if hasattr(e, "code"):
            print(f"Salla HTTP status: {e.code}")

        if hasattr(e, "read"):
            try:
                error_body = e.read().decode("utf-8")
                print(f"Salla error body: {error_body}")
            except Exception:
                pass

        return jsonify({
            "success": False,
            "message": "Salla API request failed"
        }), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
