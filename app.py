import base64
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
import pyotp
import qrcode
from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.utils import secure_filename

import config


APP_ROOT = Path(__file__).resolve().parent


def utc_now():
    return datetime.utcnow()


def to_timestamp(dt):
    return int(dt.timestamp())


def from_timestamp(ts):
    return datetime.utcfromtimestamp(ts)


def get_db():
    conn = sqlite3.connect(config.DATABASE_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            notice TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS qr_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            original_path TEXT NOT NULL,
            qr_path TEXT NOT NULL,
            qr_text TEXT NOT NULL,
            expire_at INTEGER NOT NULL,
            visit_count INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL,
            FOREIGN KEY (group_id) REFERENCES groups (id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            backup_original_path TEXT,
            backup_qr_path TEXT,
            backup_qr_text TEXT,
            updated_at INTEGER NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL UNIQUE,
            last_sent_at INTEGER NOT NULL
        )
        """
    )
    cur.execute("SELECT id FROM settings WHERE id = 1")
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO settings (id, updated_at) VALUES (1, ?)",
            (to_timestamp(utc_now()),),
        )
    conn.commit()
    conn.close()


def ensure_storage_dirs():
    base = Path(config.STORAGE_DIR)
    (base / "originals").mkdir(parents=True, exist_ok=True)
    (base / "qr_cache").mkdir(parents=True, exist_ok=True)


def normalize_storage_path(path_value):
    if not path_value:
        return None
    file_path = Path(path_value)
    if not file_path.is_absolute():
        file_path = APP_ROOT / file_path
    try:
        return file_path.resolve()
    except FileNotFoundError:
        return file_path


def storage_path_in_use(conn, path_value):
    if not path_value:
        return False
    cur = conn.cursor()
    cur.execute(
        """
        SELECT 1 FROM qr_codes
        WHERE original_path = ? OR qr_path = ?
        LIMIT 1
        """,
        (path_value, path_value),
    )
    if cur.fetchone() is not None:
        return True
    cur.execute(
        """
        SELECT 1 FROM settings
        WHERE backup_original_path = ? OR backup_qr_path = ?
        LIMIT 1
        """,
        (path_value, path_value),
    )
    return cur.fetchone() is not None


def remove_storage_file_if_unused(conn, path_value):
    if not path_value or storage_path_in_use(conn, path_value):
        return
    file_path = normalize_storage_path(path_value)
    if not file_path:
        return
    storage_root = (APP_ROOT / config.STORAGE_DIR).resolve()
    if storage_root not in file_path.parents:
        return
    file_path.unlink(missing_ok=True)


def decode_qr(image_path):
    image_bytes = Path(image_path).read_bytes()
    img = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        try:
            with Image.open(image_path) as pil_img:
                pil_img = pil_img.convert("RGB")
                img = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
        except Exception:
            img = None
    if img is None:
        raise ValueError("Unable to read image")

    detector = cv2.QRCodeDetector()
    candidates = []
    candidates.append(img)
    try:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        candidates.append(gray)
        thresh = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 2
        )
        candidates.append(thresh)
        candidates.append(cv2.bitwise_not(thresh))
    except cv2.error:
        gray = None

    scales = [0.6, 0.8, 1.0, 1.2, 1.5]
    for candidate in list(candidates):
        if candidate is None:
            continue
        for scale in scales:
            if scale == 1.0:
                resized = candidate
            else:
                try:
                    resized = cv2.resize(
                        candidate,
                        None,
                        fx=scale,
                        fy=scale,
                        interpolation=cv2.INTER_CUBIC,
                    )
                except cv2.error:
                    continue

            data, _, _ = detector.detectAndDecode(resized)
            if data:
                return data
            try:
                ok, decoded_info, _, _ = detector.detectAndDecodeMulti(resized)
            except cv2.error:
                ok, decoded_info = False, []
            if ok:
                for item in decoded_info:
                    if item:
                        return item

    raise ValueError("No QR code detected")


def generate_qr(data, output_path):
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(output_path)


def is_logged_in():
    return session.get("is_admin") is True


def require_login():
    if not is_logged_in():
        return "403 forbidden", 403
    return None


def send_notification(title, content, event_key):
    if not config.SERVER_CHAN_SEND_URL:
        return
    now_ts = to_timestamp(utc_now())
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT last_sent_at FROM notifications WHERE event_key = ?",
        (event_key,),
    )
    row = cur.fetchone()
    if row and now_ts - row["last_sent_at"] < config.NOTIFY_DEDUP_SECONDS:
        conn.close()
        return

    try:
        import requests

        requests.get(
            config.SERVER_CHAN_SEND_URL,
            params={"title": title, "desp": content},
            timeout=10,
        )
    except Exception:
        conn.close()
        return

    if row:
        cur.execute(
            "UPDATE notifications SET last_sent_at = ? WHERE event_key = ?",
            (now_ts, event_key),
        )
    else:
        cur.execute(
            "INSERT INTO notifications (event_key, last_sent_at) VALUES (?, ?)",
            (event_key, now_ts),
        )
    conn.commit()
    conn.close()


def mark_expired_qr(conn, group_id=None):
    now_ts = to_timestamp(utc_now())
    cur = conn.cursor()
    if group_id:
        cur.execute(
            "SELECT id, name, expire_at, group_id FROM qr_codes WHERE group_id = ? AND active = 1 AND expire_at <= ?",
            (group_id, now_ts),
        )
    else:
        cur.execute(
            "SELECT id, name, expire_at, group_id FROM qr_codes WHERE active = 1 AND expire_at <= ?",
            (now_ts,),
        )
    expired = cur.fetchall()
    notifications = []
    for row in expired:
        cur.execute("UPDATE qr_codes SET active = 0 WHERE id = ?", (row["id"],))
        has_replacement = False
        if row["group_id"]:
            cur.execute(
                """
                SELECT 1 FROM qr_codes
                WHERE group_id = ? AND active = 1 AND expire_at > ?
                LIMIT 1
                """,
                (row["group_id"], now_ts),
            )
            has_replacement = cur.fetchone() is not None
        if has_replacement:
            continue
        title = "QR expired"
        content = f"QR {row['name']} expired at {from_timestamp(row['expire_at']).isoformat()}"
        notifications.append((title, content, f"expired:{row['id']}"))
    conn.commit()
    for title, content, event_key in notifications:
        send_notification(title, content, event_key)


def select_best_qr(conn, group_code):
    cur = conn.cursor()
    cur.execute("SELECT id, name, notice FROM groups WHERE code = ?", (group_code,))
    group = cur.fetchone()
    if not group:
        return None, None

    mark_expired_qr(conn, group_id=group["id"])

    cur.execute(
        """
        SELECT id, name, qr_path, expire_at, visit_count
        FROM qr_codes
        WHERE group_id = ? AND active = 1 AND expire_at > ?
        ORDER BY visit_count ASC, expire_at ASC
        LIMIT 1
        """,
        (group["id"], to_timestamp(utc_now())),
    )
    qr = cur.fetchone()
    return group, qr


def increment_visit(conn, qr_id, current_count=None):
    cur = conn.cursor()
    if current_count is None:
        cur.execute("SELECT visit_count FROM qr_codes WHERE id = ?", (qr_id,))
        row = cur.fetchone()
        if not row:
            return
        current_count = row["visit_count"]

    new_count = current_count + 1
    cur.execute("UPDATE qr_codes SET visit_count = ? WHERE id = ?", (new_count, qr_id))
    conn.commit()

    threshold = getattr(config, "REMIND_VISIT_THRESHOLD", None)
    if isinstance(threshold, int) and threshold > 0 and new_count >= threshold:
        title = "QR visit threshold"
        content = f"QR {qr_id} reached {new_count} visits"
        send_notification(title, content, f"visit_threshold:{qr_id}")


def get_backup_qr(conn):
    cur = conn.cursor()
    cur.execute(
        "SELECT backup_qr_path, backup_original_path FROM settings WHERE id = 1"
    )
    row = cur.fetchone()
    if row and row["backup_qr_path"]:
        return row["backup_qr_path"], row["backup_original_path"]
    return None, None


app = Flask(__name__)
app.config["SECRET_KEY"] = config.SECRET_KEY

ensure_storage_dirs()
init_db()


_cache = {}


def get_cached_qr(group_code):
    entry = _cache.get(group_code)
    if not entry:
        return None
    if entry["expires_at"] < time.time():
        _cache.pop(group_code, None)
        return None
    qr_info = entry.get("qr_info") or {}
    if not qr_info.get("fallback"):
        expire_at = qr_info.get("expire_at")
        if expire_at and expire_at <= to_timestamp(utc_now()):
            _cache.pop(group_code, None)
            return None
    return entry


def set_cached_qr(group_code, qr_info):
    _cache[group_code] = {
        "expires_at": time.time() + config.CACHE_TTL_SECONDS,
        "qr_info": qr_info,
    }


@app.route("/")
def index():
    if not is_logged_in():
        return "403 forbidden", 403
    return redirect(url_for("admin"))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        otp = request.form.get("otp", "").strip()
        if username == config.ADMIN_USERNAME:
            totp = pyotp.TOTP(config.TOTP_SECRET)
            if totp.verify(otp, valid_window=1):
                session["is_admin"] = True
                return redirect(url_for("admin"))
        return render_template("login.html", error="Invalid credentials")
    return render_template("login.html")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin")
def admin():
    guard = require_login()
    if guard:
        return guard

    conn = get_db()
    mark_expired_qr(conn)
    cur = conn.cursor()
    cur.execute("SELECT * FROM groups ORDER BY created_at DESC")
    groups = cur.fetchall()
    cur.execute(
        """
        SELECT qr_codes.*, groups.code AS group_code, groups.name AS group_name
        FROM qr_codes
        JOIN groups ON groups.id = qr_codes.group_id
        ORDER BY qr_codes.created_at DESC
        """
    )
    qr_codes = cur.fetchall()
    cur.execute("SELECT * FROM settings WHERE id = 1")
    settings = cur.fetchone()
    conn.close()
    return render_template(
        "admin.html",
        groups=groups,
        qr_codes=qr_codes,
        settings=settings,
        default_expire_days=config.DEFAULT_EXPIRE_DAYS,
    )


@app.route("/admin/qr", methods=["POST"])
def admin_upload_qr():
    guard = require_login()
    if guard:
        return guard

    group_code = request.form.get("group_code", "").strip()
    group_name = request.form.get("group_name", "").strip()
    notice = request.form.get("notice", "").strip() or None
    qr_name = request.form.get("qr_name", "").strip()
    expire_days = request.form.get("expire_days", "").strip()
    qr_text_input = request.form.get("qr_text", "").strip()
    pasted_image_data = request.form.get("qr_image_data", "").strip()

    if not group_code or not group_name or not qr_name:
        return "Missing required fields", 400

    try:
        expire_days_int = int(expire_days) if expire_days else config.DEFAULT_EXPIRE_DAYS
    except ValueError:
        expire_days_int = config.DEFAULT_EXPIRE_DAYS

    file = request.files.get("image")
    if not file and not qr_text_input and not pasted_image_data:
        return "Missing image or QR text", 400

    ensure_storage_dirs()
    unique_id = f"{int(time.time())}_{os.urandom(4).hex()}"
    original_path = None
    qr_text = None
    if qr_text_input:
        qr_text = qr_text_input
    elif pasted_image_data:
        try:
            header, encoded = pasted_image_data.split(",", 1)
        except ValueError:
            return "Invalid pasted image data", 400
        if "base64" not in header:
            return "Invalid pasted image data", 400
        image_bytes = base64.b64decode(encoded)
        original_path = Path(config.STORAGE_DIR) / "originals" / f"{unique_id}_paste.png"
        original_path.write_bytes(image_bytes)
        try:
            qr_text = decode_qr(original_path)
        except ValueError as exc:
            qr_text = None
    else:
        assert file is not None
        filename = secure_filename(file.filename or "upload.png")
        if not filename:
            return "Invalid filename", 400
        original_path = Path(config.STORAGE_DIR) / "originals" / f"{unique_id}_{filename}"
        file.save(original_path)
        try:
            qr_text = decode_qr(original_path)
        except ValueError as exc:
            qr_text = None

    if qr_text:
        qr_cache_path = Path(config.STORAGE_DIR) / "qr_cache" / f"{unique_id}.png"
        generate_qr(qr_text, qr_cache_path)
    else:
        # Fallback: use original image directly when decode fails
        if not original_path:
            return "QR decode failed and no original image", 400
        qr_cache_path = original_path
        qr_text = ""

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM groups WHERE code = ?", (group_code,))
    row = cur.fetchone()
    if row:
        group_id = row["id"]
        cur.execute(
            "UPDATE groups SET name = ?, notice = ? WHERE id = ?",
            (group_name, notice, group_id),
        )
    else:
        cur.execute(
            "INSERT INTO groups (code, name, notice, created_at) VALUES (?, ?, ?, ?)",
            (group_code, group_name, notice, to_timestamp(utc_now())),
        )
        group_id = cur.lastrowid

    qr_text = qr_text or ""
    expire_at = to_timestamp(utc_now() + timedelta(days=expire_days_int))
    cur.execute(
        """
        INSERT INTO qr_codes
            (group_id, name, original_path, qr_path, qr_text, expire_at, visit_count, active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 0, 1, ?)
        """,
        (
            group_id,
            qr_name,
            str(original_path) if original_path else "",
            str(qr_cache_path),
            qr_text,
            expire_at,
            to_timestamp(utc_now()),
        ),
    )
    conn.commit()
    conn.close()
    _cache.pop(group_code, None)
    return redirect(url_for("admin"))


@app.route("/admin/backup", methods=["POST"])
def admin_upload_backup():
    guard = require_login()
    if guard:
        return guard

    file = request.files.get("image")
    qr_text_input = request.form.get("qr_text", "").strip()
    pasted_image_data = request.form.get("qr_image_data", "").strip()
    if not file and not qr_text_input and not pasted_image_data:
        return "Missing image or QR text", 400

    ensure_storage_dirs()
    unique_id = f"backup_{int(time.time())}_{os.urandom(4).hex()}"
    original_path = None
    qr_text = None
    if qr_text_input:
        qr_text = qr_text_input
    elif pasted_image_data:
        try:
            header, encoded = pasted_image_data.split(",", 1)
        except ValueError:
            return "Invalid pasted image data", 400
        if "base64" not in header:
            return "Invalid pasted image data", 400
        image_bytes = base64.b64decode(encoded)
        original_path = Path(config.STORAGE_DIR) / "originals" / f"{unique_id}_paste.png"
        original_path.write_bytes(image_bytes)
        try:
            qr_text = decode_qr(original_path)
        except ValueError as exc:
            qr_text = None
    else:
        assert file is not None
        filename = secure_filename(file.filename or "backup.png")
        if not filename:
            return "Invalid filename", 400
        original_path = Path(config.STORAGE_DIR) / "originals" / f"{unique_id}_{filename}"
        file.save(original_path)
        try:
            qr_text = decode_qr(original_path)
        except ValueError as exc:
            qr_text = None

    if qr_text:
        qr_cache_path = Path(config.STORAGE_DIR) / "qr_cache" / f"{unique_id}.png"
        generate_qr(qr_text, qr_cache_path)
    else:
        if not original_path:
            return "QR decode failed and no original image", 400
        qr_cache_path = original_path

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE settings
        SET backup_original_path = ?, backup_qr_path = ?, backup_qr_text = ?, updated_at = ?
        WHERE id = 1
        """,
        (str(original_path) if original_path else "", str(qr_cache_path), qr_text, to_timestamp(utc_now())),
    )
    conn.commit()
    conn.close()
    _cache.clear()
    return redirect(url_for("admin"))


@app.route("/admin/qr/<int:qr_id>/delete", methods=["POST"])
def admin_delete_qr(qr_id):
    guard = require_login()
    if guard:
        return guard

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT qr_codes.group_id, qr_codes.original_path, qr_codes.qr_path, groups.code AS group_code
        FROM qr_codes
        JOIN groups ON groups.id = qr_codes.group_id
        WHERE qr_codes.id = ?
        """,
        (qr_id,),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        return redirect(url_for("admin"))

    group_code = row["group_code"]
    paths_to_cleanup = {row["original_path"], row["qr_path"]}
    cur.execute("DELETE FROM qr_codes WHERE id = ?", (qr_id,))
    conn.commit()
    for path_value in paths_to_cleanup:
        remove_storage_file_if_unused(conn, path_value)
    conn.close()
    _cache.pop(group_code, None)
    return redirect(url_for("admin"))


@app.route("/admin/backup/delete", methods=["POST"])
def admin_delete_backup():
    guard = require_login()
    if guard:
        return guard

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT backup_original_path, backup_qr_path FROM settings WHERE id = 1"
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        return redirect(url_for("admin"))

    paths_to_cleanup = {row["backup_original_path"], row["backup_qr_path"]}
    cur.execute(
        """
        UPDATE settings
        SET backup_original_path = NULL, backup_qr_path = NULL, backup_qr_text = NULL, updated_at = ?
        WHERE id = 1
        """,
        (to_timestamp(utc_now()),),
    )
    conn.commit()
    for path_value in paths_to_cleanup:
        remove_storage_file_if_unused(conn, path_value)
    conn.close()
    _cache.clear()
    return redirect(url_for("admin"))


@app.route("/admin/test_notify", methods=["POST"])
def admin_test_notify():
    guard = require_login()
    if guard:
        return guard

    title = request.form.get("title", "").strip() or "测试通知"
    content = request.form.get("content", "").strip() or "这是一条测试通知"
    event_key = f"test:{int(time.time())}"
    send_notification(title, content, event_key)
    return redirect(url_for("admin"))


@app.route("/api/qr/<group_code>")
def api_qr(group_code):
    conn = get_db()

    cached = get_cached_qr(group_code)
    if cached:
        qr_info = cached["qr_info"]
        if not qr_info.get("fallback"):
            increment_visit(conn, qr_info["qr_id"])
        conn.close()
        return jsonify(qr_info)

    group, qr = select_best_qr(conn, group_code)
    if not group or not qr:
        backup_qr, _ = get_backup_qr(conn)
        conn.close()
        if backup_qr:
            send_notification(
                "No available QR",
                f"No active QR for group {group_code}",
                f"none:{group_code}",
            )
            qr_info = {
                "group": group_code,
                "group_name": group["name"] if group else group_code,
                "notice": (group["notice"] if group else None) or config.DEFAULT_NOTICE,
                "qr_url": url_for("serve_file", path=backup_qr),
                "fallback": True,
            }
            set_cached_qr(group_code, qr_info)
            return jsonify(qr_info)
        return jsonify({"error": "No QR available"}), 404

    increment_visit(conn, qr["id"], qr["visit_count"])
    conn.close()

    qr_info = {
        "qr_id": qr["id"],
        "group": group_code,
        "group_name": group["name"],
        "notice": group["notice"] or config.DEFAULT_NOTICE,
        "qr_url": url_for("serve_file", path=qr["qr_path"]),
        "expire_at": qr["expire_at"],
        "fallback": False,
    }
    set_cached_qr(group_code, qr_info)
    return jsonify(qr_info)


@app.route("/api/qr-image/<group_code>")
def api_qr_image(group_code):
    conn = get_db()

    group, qr = select_best_qr(conn, group_code)
    if not group or not qr:
        backup_qr, _ = get_backup_qr(conn)
        conn.close()
        if backup_qr:
            send_notification(
                "No available QR",
                f"No active QR for group {group_code}",
                f"none:{group_code}",
            )
            qr_url = url_for("serve_file", path=backup_qr)
            qr_info = {
                "group": group_code,
                "group_name": group["name"] if group else group_code,
                "notice": (group["notice"] if group else None) or config.DEFAULT_NOTICE,
                "qr_url": qr_url,
                "fallback": True,
            }
            set_cached_qr(group_code, qr_info)
            return redirect(qr_url)
        return "No QR available", 404

    increment_visit(conn, qr["id"], qr["visit_count"])
    conn.close()

    qr_url = url_for("serve_file", path=qr["qr_path"])
    qr_info = {
        "qr_id": qr["id"],
        "group": group_code,
        "group_name": group["name"],
        "notice": group["notice"] or config.DEFAULT_NOTICE,
        "qr_url": qr_url,
        "fallback": False,
    }
    set_cached_qr(group_code, qr_info)
    return redirect(qr_url)


@app.route("/invite/<group_code>")
def invite(group_code):
    conn = get_db()
    cached = get_cached_qr(group_code)
    if cached:
        qr_info = cached["qr_info"]
        if not qr_info.get("fallback"):
            increment_visit(conn, qr_info["qr_id"])
        conn.close()
        return render_template(
            "invite.html",
            group_name=qr_info.get("group_name") or group_code,
            notice=qr_info.get("notice") or config.DEFAULT_NOTICE,
            qr_url=qr_info["qr_url"],
            fallback=qr_info.get("fallback", False),
            is_mobile=is_mobile_request(),
        )

    group, qr = select_best_qr(conn, group_code)
    if not group or not qr:
        backup_qr, _ = get_backup_qr(conn)
        conn.close()
        if backup_qr:
            send_notification(
                "No available QR",
                f"No active QR for group {group_code}",
                f"none:{group_code}",
            )
            qr_url = url_for("serve_file", path=backup_qr)
            qr_info = {
                "group": group_code,
                "group_name": group["name"] if group else group_code,
                "notice": (group["notice"] if group else None) or config.DEFAULT_NOTICE,
                "qr_url": qr_url,
                "fallback": True,
            }
            set_cached_qr(group_code, qr_info)
            return render_template(
                "invite.html",
                group_name=group["name"] if group else group_code,
                notice=(group["notice"] if group else None) or config.DEFAULT_NOTICE,
                qr_url=qr_url,
                fallback=True,
                is_mobile=is_mobile_request(),
            )
        return "No QR available", 404

    increment_visit(conn, qr["id"], qr["visit_count"])
    conn.close()

    qr_url = url_for("serve_file", path=qr["qr_path"])
    qr_info = {
        "qr_id": qr["id"],
        "group": group_code,
        "group_name": group["name"],
        "notice": group["notice"] or config.DEFAULT_NOTICE,
        "qr_url": qr_url,
        "fallback": False,
    }
    set_cached_qr(group_code, qr_info)
    return render_template(
        "invite.html",
        group_name=group["name"],
        notice=group["notice"] or config.DEFAULT_NOTICE,
        qr_url=qr_url,
        fallback=False,
        is_mobile=is_mobile_request(),
    )


def is_mobile_request():
    ua = request.headers.get("User-Agent", "").lower()
    keywords = ["iphone", "android", "ipad", "mobile", "micromessenger"]
    return any(word in ua for word in keywords)


@app.route("/files")
def serve_file():
    path = request.args.get("path", "")
    if not path:
        return "Not found", 404
    file_path = Path(path)
    if not file_path.is_absolute():
        file_path = APP_ROOT / file_path
    file_path = file_path.resolve()
    storage_root = (APP_ROOT / config.STORAGE_DIR).resolve()
    if storage_root not in file_path.parents:
        return "Not found", 404
    if not file_path.exists():
        return "Not found", 404
    return send_file(file_path)


if __name__ == "__main__":
    ensure_storage_dirs()
    init_db()
    app.run(host="0.0.0.0", port=5002, debug=True)
