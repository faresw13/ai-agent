
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "AI Agent is running"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)

    print("SALLA WEBHOOK:")
    print(data)

    return jsonify({
        "success": True
    }), 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok"
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
