import importlib
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def app_module(tmp_path, monkeypatch):
    config = types.SimpleNamespace(
        ADMIN_USERNAME="admin",
        TOTP_SECRET="JBSWY3DPEHPK3PXP",
        SECRET_KEY="test-secret",
        DATABASE_PATH=str(tmp_path / "data.db"),
        STORAGE_DIR=str(tmp_path / "storage"),
        DEFAULT_EXPIRE_DAYS=7,
        CACHE_TTL_SECONDS=1800,
        REMIND_VISIT_THRESHOLD=0,
        NOTIFY_DEDUP_SECONDS=1800,
        SERVER_CHAN_SEND_URL="",
        DEFAULT_NOTICE="notice",
        MAX_CONTENT_LENGTH=1024 * 1024,
        MAX_PASTED_IMAGE_BYTES=512 * 1024,
        SESSION_COOKIE_SECURE=False,
    )
    monkeypatch.syspath_prepend(str(ROOT))
    monkeypatch.setitem(sys.modules, "config", config)
    sys.modules.pop("app", None)
    module = importlib.import_module("app")
    yield module
    sys.modules.pop("app", None)


def test_admin_post_without_csrf_token_is_rejected(app_module):
    client = app_module.app.test_client()

    response = client.post(
        "/admin/login",
        data={"username": "admin", "otp": "000000"},
    )

    assert response.status_code == 400
    assert b"Invalid CSRF token" in response.data


def test_increment_visit_uses_atomic_database_increment(app_module):
    conn = app_module.get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO groups (code, name, created_at) VALUES (?, ?, ?)",
        ("group-1", "Group 1", app_module.to_timestamp(app_module.utc_now())),
    )
    group_id = cur.lastrowid
    cur.execute(
        """
        INSERT INTO qr_codes
            (group_id, name, original_path, qr_path, qr_text, expire_at, visit_count, active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 0, 1, ?)
        """,
        (
            group_id,
            "QR",
            "",
            "storage/qr_cache/qr.png",
            "qr-text",
            app_module.to_timestamp(app_module.utc_now()),
            app_module.to_timestamp(app_module.utc_now()),
        ),
    )
    qr_id = cur.lastrowid
    conn.commit()

    app_module.increment_visit(conn, qr_id, current_count=0)
    app_module.increment_visit(conn, qr_id, current_count=0)

    cur.execute("SELECT visit_count FROM qr_codes WHERE id = ?", (qr_id,))
    assert cur.fetchone()["visit_count"] == 2
    conn.close()
