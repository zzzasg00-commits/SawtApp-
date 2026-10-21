"""Gmail OTP API plus an optional Telegram admin bot.

Client API:
  POST /send-otp   {"email": "person@example.com"}
  POST /verify-otp {"email": "person@example.com", "code": "123456"}

Do not put SMTP credentials or Telegram bot tokens in an APK.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import smtplib
import sqlite3
import threading
import time
from email.message import EmailMessage
from contextlib import closing
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID", "").strip()
SMTP_EMAIL = os.getenv("SMTP_EMAIL", "").strip()
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD", "").replace(" ", "").strip()
APP_API_KEY = os.getenv("APP_API_KEY", "").strip()
APP_NAME = os.getenv("APP_NAME", "My App").strip() or "My App"
PORT = int(os.getenv("PORT", "8080"))
DATABASE_PATH = os.getenv("DATABASE_PATH", "mailbot.sqlite3").strip() or "mailbot.sqlite3"
# Render can generate APP_API_KEY for us. A random process secret is a fallback for local tests.
OTP_PEPPER = APP_API_KEY or secrets.token_urlsafe(32)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
OTP_TTL_SECONDS = 10 * 60
EMAIL_COOLDOWN_SECONDS = 60
MAX_EMAIL_SENDS_PER_HOUR = 3
MAX_IP_SENDS_PER_HOUR = 10
MAX_OTP_ATTEMPTS = 5

app = Flask(__name__)
# Render sits behind a trusted reverse proxy; use its forwarded client IP for rate limiting.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def connect_db() -> sqlite3.Connection:
    path = Path(DATABASE_PATH)
    if str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 15000")
    return db


def init_db() -> None:
    with closing(connect_db()) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS otp_codes (
                email TEXT PRIMARY KEY,
                code_hash TEXT NOT NULL,
                expires_at REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS send_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                ip TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_send_events_email_time
              ON send_events(email, created_at);
            CREATE INDEX IF NOT EXISTS idx_send_events_ip_time
              ON send_events(ip, created_at);
            """
        )


def config_ok() -> None:
    missing = [name for name, value in {
        "SMTP_EMAIL": SMTP_EMAIL,
        "SMTP_APP_PASSWORD": SMTP_APP_PASSWORD,
    }.items() if not value]
    if missing:
        raise RuntimeError("Missing required environment settings: " + ", ".join(missing))
    if BOT_TOKEN and not ADMIN_TELEGRAM_ID:
        raise RuntimeError("ADMIN_TELEGRAM_ID is required when BOT_TOKEN is configured")


def send_email(to_email: str, subject: str, body: str) -> None:
    if not EMAIL_RE.fullmatch(to_email) or len(to_email) > 254:
        raise ValueError("Invalid email address")
    message = EmailMessage()
    message["From"] = f"{APP_NAME} <{SMTP_EMAIL}>"
    message["To"] = to_email
    message["Subject"] = subject[:180]
    message.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=25) as server:
        server.login(SMTP_EMAIL, SMTP_APP_PASSWORD)
        server.send_message(message)


def otp_digest(email: str, code: str) -> str:
    value = f"{email.lower()}:{code}".encode("utf-8")
    return hmac.new(OTP_PEPPER.encode("utf-8"), value, hashlib.sha256).hexdigest()


def client_ip() -> str:
    return request.remote_addr or "unknown"


def reserve_send(email: str, ip: str, code: str, now: float) -> tuple[bool, str]:
    """Atomically rate-limit and record an OTP request before sending email."""
    db = connect_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM send_events WHERE created_at < ?", (now - 48 * 3600,))
        since_hour = now - 3600
        email_count = db.execute(
            "SELECT COUNT(*) FROM send_events WHERE email = ? AND created_at > ?",
            (email, since_hour),
        ).fetchone()[0]
        ip_count = db.execute(
            "SELECT COUNT(*) FROM send_events WHERE ip = ? AND created_at > ?",
            (ip, since_hour),
        ).fetchone()[0]
        last = db.execute(
            "SELECT MAX(created_at) FROM send_events WHERE email = ?",
            (email,),
        ).fetchone()[0]
        if last is not None and now - float(last) < EMAIL_COOLDOWN_SECONDS:
            db.rollback()
            return False, "wait_before_retry"
        if email_count >= MAX_EMAIL_SENDS_PER_HOUR:
            db.rollback()
            return False, "email_rate_limit"
        if ip_count >= MAX_IP_SENDS_PER_HOUR:
            db.rollback()
            return False, "ip_rate_limit"
        db.execute(
            "INSERT INTO send_events(email, ip, created_at) VALUES (?, ?, ?)",
            (email, ip, now),
        )
        db.execute(
            "INSERT INTO otp_codes(email, code_hash, expires_at, attempts, created_at) "
            "VALUES (?, ?, ?, 0, ?) ON CONFLICT(email) DO UPDATE SET "
            "code_hash=excluded.code_hash, expires_at=excluded.expires_at, "
            "attempts=0, created_at=excluded.created_at",
            (email, otp_digest(email, code), now + OTP_TTL_SECONDS, now),
        )
        db.commit()
        return True, "ok"
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def delete_otp(email: str) -> None:
    with closing(connect_db()) as db:
        db.execute("DELETE FROM otp_codes WHERE email = ?", (email,))


def is_admin(update) -> bool:
    return bool(
        update.effective_user
        and ADMIN_TELEGRAM_ID
        and str(update.effective_user.id) == ADMIN_TELEGRAM_ID
    )


# Optional Telegram admin bot. OTP sending from the app does not require a Telegram bot.
async def start_command(update, context) -> None:
    if not is_admin(update):
        await update.message.reply_text("هذا البوت خاص بالمسؤول.")
        return
    await update.message.reply_text(
        "مرحبًا. لإرسال بريد يدوي استخدم:\n"
        "/send email@example.com | العنوان | محتوى الرسالة\n\n/status لفحص الحالة"
    )


async def status_command(update, context) -> None:
    if not is_admin(update):
        await update.message.reply_text("غير مصرح.")
        return
    await update.message.reply_text(f"الخدمة تعمل. بريد الإرسال: {SMTP_EMAIL}")


async def send_command(update, context) -> None:
    if not is_admin(update):
        await update.message.reply_text("غير مصرح.")
        return
    parts = [part.strip() for part in " ".join(context.args).split("|", 2)]
    if len(parts) != 3:
        await update.message.reply_text(
            "/send email@example.com | العنوان | محتوى الرسالة"
        )
        return
    try:
        send_email(parts[0], parts[1], parts[2])
        await update.message.reply_text(f"تم إرسال الرسالة إلى {parts[0]}")
    except Exception:
        app.logger.exception("Manual email send failed")
        await update.message.reply_text("تعذر إرسال الرسالة. راجع سجلات الخدمة وإعدادات Gmail.")


@app.after_request
def add_api_headers(response):
    # Needed if the app is a WebView/web-based wrapper. No cookies or credentials are used.
    if request.path in ("/send-otp", "/verify-otp"):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
def index():
    return jsonify({
        "service": "email-verification-api",
        "ok": True,
        "endpoints": {"health": "/health", "send_code": "POST /send-otp", "verify_code": "POST /verify-otp"},
    })


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "email-verification-api"}), 200


@app.route("/send-otp", methods=["POST", "OPTIONS"])
def send_otp():
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    if len(email) > 254 or not EMAIL_RE.fullmatch(email):
        return jsonify({"ok": False, "error": "invalid_email", "message": "أدخل بريدًا إلكترونيًا صحيحًا."}), 400

    code = f"{secrets.randbelow(1_000_000):06d}"
    now = time.time()
    try:
        allowed, reason = reserve_send(email, client_ip(), code, now)
        if not allowed:
            if reason == "wait_before_retry":
                message = "انتظر دقيقة قبل طلب رمز جديد."
            elif reason == "email_rate_limit":
                message = "تم تجاوز عدد طلبات هذا البريد. حاول لاحقًا."
            else:
                message = "طلبات كثيرة من هذا الاتصال. حاول لاحقًا."
            return jsonify({"ok": False, "error": reason, "message": message}), 429

        subject = f"رمز التحقق من {APP_NAME}"
        body = (
            f"مرحبًا،\n\nرمز التحقق الخاص بك هو: {code}\n"
            "تنتهي صلاحية الرمز خلال 10 دقائق. لا تشاركه مع أي شخص.\n\n"
            f"إذا لم تطلب هذا الرمز، فتجاهل الرسالة.\n\n— {APP_NAME}"
        )
        try:
            send_email(email, subject, body)
        except Exception:
            delete_otp(email)
            app.logger.exception("OTP email send failed")
            return jsonify({"ok": False, "error": "email_send_failed", "message": "تعذر إرسال الرسالة الآن. حاول لاحقًا."}), 502
        return jsonify({"ok": True, "message": "otp_sent", "expires_in_seconds": OTP_TTL_SECONDS}), 200
    except Exception:
        app.logger.exception("Send OTP request failed")
        return jsonify({"ok": False, "error": "server_error", "message": "حدث خطأ بالخدمة."}), 500


@app.route("/verify-otp", methods=["POST", "OPTIONS"])
def verify_otp():
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    code = str(data.get("code", "")).strip()
    if len(email) > 254 or not EMAIL_RE.fullmatch(email) or not re.fullmatch(r"\d{6}", code):
        return jsonify({"ok": False, "error": "invalid_input", "message": "البريد أو الرمز غير صحيح."}), 400

    now = time.time()
    db = connect_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT code_hash, expires_at, attempts FROM otp_codes WHERE email = ?",
            (email,),
        ).fetchone()
        if row is None:
            db.rollback()
            return jsonify({"ok": False, "error": "no_active_code", "message": "اطلب رمز تحقق جديدًا."}), 400
        if now > float(row["expires_at"]):
            db.execute("DELETE FROM otp_codes WHERE email = ?", (email,))
            db.commit()
            return jsonify({"ok": False, "error": "code_expired", "message": "انتهت صلاحية الرمز. اطلب رمزًا جديدًا."}), 400
        attempts = int(row["attempts"]) + 1
        correct = secrets.compare_digest(otp_digest(email, code), str(row["code_hash"]))
        if correct:
            db.execute("DELETE FROM otp_codes WHERE email = ?", (email,))
            db.commit()
            return jsonify({"ok": True, "verified": True, "message": "تم التحقق من البريد بنجاح."}), 200
        if attempts >= MAX_OTP_ATTEMPTS:
            db.execute("DELETE FROM otp_codes WHERE email = ?", (email,))
            db.commit()
            return jsonify({"ok": False, "error": "too_many_attempts", "message": "تجاوزت عدد المحاولات. اطلب رمزًا جديدًا."}), 429
        db.execute("UPDATE otp_codes SET attempts = ? WHERE email = ?", (attempts, email))
        db.commit()
        return jsonify({"ok": False, "error": "wrong_code", "message": "الرمز غير صحيح.", "attempts_remaining": MAX_OTP_ATTEMPTS - attempts}), 400
    except Exception:
        db.rollback()
        app.logger.exception("OTP verification failed")
        return jsonify({"ok": False, "error": "server_error", "message": "حدث خطأ بالخدمة."}), 500
    finally:
        db.close()


def run_api() -> None:
    from waitress import serve
    serve(app, host="0.0.0.0", port=PORT, threads=8)


def main() -> None:
    config_ok()
    init_db()
    print("Email OTP API starting on port", PORT)
    if BOT_TOKEN:
        # When Telegram is configured, the API runs alongside polling.
        threading.Thread(target=run_api, daemon=True).start()
        from telegram import Update
        from telegram.ext import Application, CommandHandler
        telegram_app = Application.builder().token(BOT_TOKEN).build()
        telegram_app.add_handler(CommandHandler("start", start_command))
        telegram_app.add_handler(CommandHandler("status", status_command))
        telegram_app.add_handler(CommandHandler("send", send_command))
        telegram_app.run_polling()
    else:
        # API-only mode is enough for email verification inside the APK.
        run_api()


if __name__ == "__main__":
    main()
