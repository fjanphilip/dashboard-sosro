#!/usr/bin/env python3
"""Backend dashboard listrik: CRUD perangkat Modbus TCP + polling realtime 3 fasa.

Browser tidak bisa bicara Modbus TCP, jadi server ini yang polling meter lalu
menyajikan hasilnya lewat /api/devices. Konfigurasi perangkat disimpan di SQLite.
"""
import argparse
import contextlib
import hashlib
import hmac
import inspect
import json
import logging
import logging.handlers
import math
import mimetypes
import os
import re
import secrets
import socket
import sqlite3
import struct
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import mysql.connector
from pymodbus.client import ModbusTcpClient

from meter_status import (
    DEVICE_ERROR, DISABLED, LIVE_STATES, MeterHealth, ModbusResponseError, OFFLINE_GATEWAY,
    OFFLINE_METER, OFFLINE_STATES, UNKNOWN, StatusConfig, classify_exception, classify_values,
    endpoint_suspect, link_status,
)

logging.getLogger("pymodbus").setLevel(logging.CRITICAL)

BASE = Path(__file__).parent
STATIC = BASE / "static"
DB_PATH = BASE / "meters.db"
DB_CONFIG_PATH = BASE / "database.json"  # koneksi MySQL (berisi password), ditulis oleh setup_db.py
try:
    _SESSION_HOURS = float(os.environ.get("DASHBOARD_SESSION_HOURS") or 12)
except ValueError:
    _SESSION_HOURS = 12.0
SESSION_TTL_S = _SESSION_HOURS * 3600  # masa idle; diperpanjang otomatis selama dipakai
ROLES = ("admin", "user")

# Peta register per model meter (float32, alamat 0-based). Dibaca per blok supaya satu siklus
# hanya beberapa request per meter. "scale" mengubah satuan meter ke W / VA yang dipakai dashboard.
PROFILES = {
    # Eastron SDM630 (input register, satuan W / VA). Hasil scan meter 192.168.1.110.
    "sdm630": {
        "label": "Eastron SDM630",
        "blocks": [(0, 24), (52, 6), (70, 2)],
        "phase": {"v": (0, 2, 4), "i": (6, 8, 10), "p": (12, 14, 16), "s": (18, 20, 22)},
        "total": {"p_total": 52, "s_total": 56, "freq": 70},
        "scale": 1,
    },
    # Schneider PM5330 (holding register, alamat = nomor manual - 1, satuan kW / kVA).
    # Arus 3000-3005, tegangan L-N 3028-3033, kW 3054-3060, kVA 3070-3076, frekuensi 3110.
    "pm5330": {
        "label": "Schneider PM5330",
        "blocks": [(2999, 112)],
        "phase": {"v": (3027, 3029, 3031), "i": (2999, 3001, 3003), "p": (3053, 3055, 3057), "s": (3069, 3071, 3073)},
        "total": {"p_total": 3059, "s_total": 3075, "freq": 3109},
        "scale": 1000,
    },
}
DEFAULT_MODEL = "sdm630"
SCALED_KEYS = ("p", "s", "p_total", "s_total")  # besaran daya; v, i, freq tidak diskalakan

FUNCTIONS = ("input", "holding")
BYTE_ORDERS = ("big", "swap", "little", "badc")

FIELDS = ("name", "site", "area", "model", "host", "port", "unit_id", "function", "byte_order",
          "interval_s", "enabled")

DEFAULT_DB_NAME = "ois_sosro_pw"
CONN_FIELDS = ("host", "port", "db_user", "db_password", "db_name", "store_interval_s")
DB_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")  # dipakai sebagai identifier SQL, jadi dibatasi ketat
MAX_RETRY_S = 30
HEALTH_CHECK_S = 5  # interval cek koneksi MySQL walau tidak ada data untuk dikirim
BATCH_SIZE = 500            # baris outbox per kiriman ke MySQL
OUTBOX_MAX_ROWS = 500_000   # batas buffer lokal (~7 hari untuk 40 meter @60 dtk); terlama dibuang
MAX_BODY_BYTES = 64 * 1024

# Riwayat tren: dicatat ke SQLite oleh thread TrendRecorder, dibaca ulang oleh grafik.
# Dengan begitu semua browser melihat deret yang sama (sinkron) dan grafik sudah terisi
# begitu halaman dibuka - tidak lagi diakumulasi di memori masing-masing browser.
TREND_POINTS = 90
TREND_INTERVAL_S = 2.0
BACKFILL_SOURCE_ROWS = 4000  # baris power_readings terakhir yang dibaca saat mengisi awal grafik
try:
    TREND_INTERVAL_S = float(os.environ.get("TREND_INTERVAL_S") or TREND_INTERVAL_S)
except ValueError:
    pass
PHASE_MIN_V = 10  # fasa di bawah ini dianggap tidak bertegangan (samakan dengan app.js)
MYSQL_NO_PRIVILEGE = (1044, 1142, 1227)  # user penulis (hanya INSERT) tidak boleh CREATE/ALTER

PHASE_COLUMNS = [f"{k}{n}" for k in ("v", "i", "p", "s") for n in (1, 2, 3)]
READINGS_DDL = f"""CREATE TABLE IF NOT EXISTS power_readings (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    recorded_at DATETIME(3) NOT NULL,
    device_id INT NOT NULL,
    device_name VARCHAR(100) NOT NULL,
    host VARCHAR(255) NOT NULL,
    unit_id SMALLINT NOT NULL,
    {', '.join(f'{c} FLOAT NULL' for c in PHASE_COLUMNS)},
    p_total FLOAT NULL, s_total FLOAT NULL, freq FLOAT NULL,
    site VARCHAR(100) NULL,
    UNIQUE KEY uq_sample (device_id, recorded_at),
    INDEX idx_device_time (device_id, recorded_at),
    INDEX idx_time (recorded_at),
    INDEX idx_site_time (site, recorded_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
# Kolom baris outbox/MySQL (tanpa id). Urutan sama di SQLite outbox, INSERT, dan _rows().
SAMPLE_COLUMNS = ["recorded_at", "device_id", "device_name", "host", "unit_id", *PHASE_COLUMNS,
                  "p_total", "s_total", "freq", "site"]
# INSERT IGNORE + kunci unik (device_id, recorded_at): kirim ulang batch setelah putus tidak menggandakan data.
READINGS_INSERT = (
    f"INSERT IGNORE INTO power_readings ({', '.join(SAMPLE_COLUMNS)}) "
    f"VALUES ({', '.join(['%s'] * len(SAMPLE_COLUMNS))})"
)

log = logging.getLogger("dashboard")

# ---------- konfigurasi polling (config.json) ----------
CONFIG_PATH = BASE / "config.json"
DEFAULT_CONFIG = {
    "modbus_timeout_s": 5.0,          # D: harus > response timeout RS485 di gateway
    "gateway_response_timeout_s": 3.0,
    "modbus_retries": 1,              # B: retry per pembacaan
    "fail_threshold": 3,              # B: gagal berturut sebelum dinyatakan OFFLINE
    "offline_interval_s": 30.0,       # C: meter offline dipoll lebih jarang
    "voltage_min_v": 180.0,           # rentang validasi tegangan fasa
    "voltage_max_v": 260.0,
    "age_online_factor": 2.0,         # H: umur data < 2x interval = online
    "age_offline_factor": 5.0,        # H: umur data >= 5x interval = offline
    "event_keep_days": 90,
}


def load_config():
    """Baca config.json; nilai yang hilang/tidak valid memakai default di atas."""
    cfg = dict(DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.warning("config.json tidak ditemukan, memakai nilai default")
        data = {}
    except (OSError, json.JSONDecodeError) as exc:
        log.error("config.json tidak bisa dibaca (%s), memakai nilai default", exc)
        data = {}
    if not isinstance(data, dict):
        data = {}
    for key, default in DEFAULT_CONFIG.items():
        if key not in data:
            continue
        try:
            cfg[key] = type(default)(data[key])
        except (TypeError, ValueError):
            log.warning("config %s=%r tidak valid, pakai default %r", key, data[key], default)

    # D: timeout Modbus di PC harus lebih besar dari response timeout RS485 gateway.
    if cfg["modbus_timeout_s"] <= cfg["gateway_response_timeout_s"]:
        fixed = cfg["gateway_response_timeout_s"] + 1.0
        log.warning("modbus_timeout_s (%.1fs) harus > gateway_response_timeout_s (%.1fs); "
                    "dipakai %.1fs", cfg["modbus_timeout_s"], cfg["gateway_response_timeout_s"], fixed)
        cfg["modbus_timeout_s"] = fixed
    if cfg["voltage_min_v"] >= cfg["voltage_max_v"]:
        log.warning("rentang tegangan tidak valid, pakai default %s-%s V",
                    DEFAULT_CONFIG["voltage_min_v"], DEFAULT_CONFIG["voltage_max_v"])
        cfg["voltage_min_v"] = DEFAULT_CONFIG["voltage_min_v"]
        cfg["voltage_max_v"] = DEFAULT_CONFIG["voltage_max_v"]
    cfg["modbus_retries"] = max(0, cfg["modbus_retries"])
    cfg["fail_threshold"] = max(1, cfg["fail_threshold"])
    cfg["offline_interval_s"] = max(1.0, cfg["offline_interval_s"])
    return cfg


CFG = load_config()
TIMEOUT_S = CFG["modbus_timeout_s"]
STATUS_CFG = StatusConfig(
    timeout_s=CFG["modbus_timeout_s"],
    retries=int(CFG["modbus_retries"]),
    fail_threshold=int(CFG["fail_threshold"]),
    offline_interval_s=CFG["offline_interval_s"],
    voltage_min_v=CFG["voltage_min_v"],
    voltage_max_v=CFG["voltage_max_v"],
    age_online_factor=CFG["age_online_factor"],
    age_offline_factor=CFG["age_offline_factor"],
)


# ---------- database ----------
@contextlib.contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=10000")
    try:
        con.execute("PRAGMA journal_mode=WAL")  # pembaca tidak memblokir penulis
    except sqlite3.OperationalError:
        pass  # filesystem tidak mendukung WAL (mis. share jaringan): pakai mode default
    con.execute("PRAGMA synchronous=NORMAL")
    try:
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


_INSTANCE_LOCK = None


def acquire_instance_lock():
    """Cegah dua server memakai file database yang sama. Lock dilepas otomatis saat proses berhenti."""
    lock_path = Path(str(DB_PATH) + ".lock")
    handle = open(lock_path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write("x")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def init_db():
    with db() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, site TEXT NOT NULL DEFAULT '', area TEXT NOT NULL DEFAULT '',
                host TEXT NOT NULL, port INTEGER NOT NULL DEFAULT 502,
                unit_id INTEGER NOT NULL DEFAULT 1,
                function TEXT NOT NULL DEFAULT 'input',
                byte_order TEXT NOT NULL DEFAULT 'big',
                interval_s REAL NOT NULL DEFAULT 2,
                enabled INTEGER NOT NULL DEFAULT 1)"""
        )
        if "model" not in {r["name"] for r in con.execute("PRAGMA table_info(devices)")}:
            con.execute(f"ALTER TABLE devices ADD COLUMN model TEXT NOT NULL DEFAULT '{DEFAULT_MODEL}'")
        # Site (lokasi) adalah tingkat di atas area: device lama otomatis '' = "Semua site".
        if "site" not in {r["name"] for r in con.execute("PRAGMA table_info(devices)")}:
            con.execute("ALTER TABLE devices ADD COLUMN site TEXT NOT NULL DEFAULT ''")
        con.execute(
            "CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, "
            "role TEXT NOT NULL DEFAULT 'user')"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, "
            "role TEXT NOT NULL, expires REAL NOT NULL)"
        )
        # Riwayat tren untuk grafik: satu baris per (detik pencatatan, perangkat).
        con.execute(
            """CREATE TABLE IF NOT EXISTS trend_samples (
                ts INTEGER NOT NULL,
                device_id INTEGER NOT NULL,
                unit_id INTEGER,
                area TEXT NOT NULL DEFAULT '',
                site TEXT NOT NULL DEFAULT '',
                kw REAL, kva REAL, v REAL, a REAL, freq REAL,
                PRIMARY KEY (ts, device_id))"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_trend_ts ON trend_samples(ts)")
        if "site" not in {r["name"] for r in con.execute("PRAGMA table_info(trend_samples)")}:
            con.execute("ALTER TABLE trend_samples ADD COLUMN site TEXT NOT NULL DEFAULT ''")
        # ID slave (unit_id) ikut disimpan supaya riwayat lokal bisa ditelusuri per slave.
        if "unit_id" not in {r["name"] for r in con.execute("PRAGMA table_info(trend_samples)")}:
            con.execute("ALTER TABLE trend_samples ADD COLUMN unit_id INTEGER")
        # Riwayat perubahan status meter (aturan G): dipakai untuk downtime & availability.
        con.execute(
            """CREATE TABLE IF NOT EXISTS device_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL,
                ts REAL NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                kind TEXT,
                message TEXT)"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_device_events ON device_events(device_id, ts)")
        # Buffer lokal: sampel ditulis ke sini dulu, baru dikirim ke MySQL. Aman saat jaringan/MySQL putus.
        con.execute(
            "CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT NOT NULL, "
            "device_id INTEGER NOT NULL, device_name TEXT NOT NULL, host TEXT NOT NULL, unit_id INTEGER NOT NULL, "
            f"{', '.join(f'{c} REAL' for c in PHASE_COLUMNS)}, p_total REAL, s_total REAL, freq REAL, "
            "site TEXT NOT NULL DEFAULT '')"
        )
        # Migrasi wadah buffer lama: site ikut disimpan supaya bisa dikirim ke MySQL.
        if "site" not in {r["name"] for r in con.execute("PRAGMA table_info(outbox)")}:
            con.execute("ALTER TABLE outbox ADD COLUMN site TEXT NOT NULL DEFAULT ''")
    seed_users()
    migrate_legacy_connection()
    with db() as con:
        con.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))


# ---------- akun & sesi ----------
def hash_password(password, iterations=200_000):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    try:
        algo, iterations, salt_hex, digest_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def seed_users():
    """Buat akun awal sekali saja. Password dari env atau acak (dicetak di console)."""
    with db() as con:
        if con.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            return
        created = []
        for username, env_name, role in (("admin", "DASHBOARD_ADMIN_PASSWORD", "admin"),
                                         ("user", "DASHBOARD_USER_PASSWORD", "user")):
            password = os.environ.get(env_name) or ("admin123" if role == "admin" else "user123")
            con.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                        (username, hash_password(password), role))
            created.append((username, password, role))
    for username, password, role in created:
        print(f"[akun] {role}: {username} / {password}  (ganti lewat menu Akun)")


def authenticate(username, password):
    with db() as con:
        row = con.execute("SELECT username, password_hash, role FROM users WHERE username=?",
                          (username,)).fetchone()
    if row and verify_password(password, row["password_hash"]):
        return {"username": row["username"], "role": row["role"]}
    return None


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(username, role):
    token = secrets.token_urlsafe(32)
    with db() as con:
        con.execute("INSERT INTO sessions (token_hash, username, role, expires) VALUES (?, ?, ?, ?)",
                    (_token_hash(token), username, role, time.time() + SESSION_TTL_S))
    return token


def get_session(token):
    """Sesi disimpan di DB (token di-hash) sehingga tahan restart. Masa berlaku diperpanjang selama dipakai."""
    if not token:
        return None
    token_hash = _token_hash(token)
    now = time.time()
    with db() as con:
        row = con.execute("SELECT username, role, expires FROM sessions WHERE token_hash=?",
                          (token_hash,)).fetchone()
        if not row:
            return None
        if row["expires"] < now:
            con.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
            return None
        if row["expires"] - now < SESSION_TTL_S * 0.9:  # sliding: tulis ulang tiap ~10% TTL terpakai
            con.execute("UPDATE sessions SET expires=? WHERE token_hash=?", (now + SESSION_TTL_S, token_hash))
    return {"username": row["username"], "role": row["role"]}


def drop_session(token):
    if not token:
        return
    with db() as con:
        con.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))


def drop_sessions_for(username):
    with db() as con:
        con.execute("DELETE FROM sessions WHERE username=?", (username,))


def get_user(user_id):
    with db() as con:
        row = con.execute("SELECT id, username, role FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def upsert_user(username, password, role="user"):
    """Buat akun baru atau perbarui password+role akun yang sudah ada."""
    with db() as con:
        row = con.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if row:
            con.execute("UPDATE users SET password_hash=?, role=? WHERE id=?",
                        (hash_password(password), role, row["id"]))
        else:
            con.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                        (username, hash_password(password), role))
    drop_sessions_for(username)


def canonical_area(area):
    """Selaraskan ejaan area dengan yang sudah ada (tanpa membedakan huruf besar/kecil)."""
    return canonical_group("area", area)


def canonical_site(site):
    """Selaraskan ejaan site (lokasi) seperti canonical_area."""
    return canonical_group("site", site)


def canonical_group(column, value):
    if not value:
        return value
    with db() as con:
        row = con.execute(
            f"SELECT {column} AS value, COUNT(*) AS n FROM devices "
            f"WHERE {column} <> '' AND lower({column}) = lower(?) "
            f"GROUP BY {column} ORDER BY n DESC, {column} LIMIT 1", (value,)).fetchone()
    return row["value"] if row else value


def validate(body):
    """Return (clean_dict, error_message)."""
    try:
        clean = {
            "name": str(body.get("name", "")).strip(),
            "site": str(body.get("site", "")).strip(),
            "area": str(body.get("area", "")).strip(),
            "model": str(body.get("model", DEFAULT_MODEL)),
            "host": str(body.get("host", "")).strip(),
            "port": int(body.get("port", 502)),
            "unit_id": int(body.get("unit_id", 1)),
            "function": str(body.get("function", "input")),
            "byte_order": str(body.get("byte_order", "big")),
            "interval_s": float(body.get("interval_s", 2)),
            "enabled": 1 if body.get("enabled", True) else 0,
        }
    except (TypeError, ValueError):
        return None, "Port, Unit ID, dan interval harus berupa angka."
    if not clean["name"]:
        return None, "Nama titik ukur wajib diisi."
    if clean["model"] not in PROFILES:
        return None, "Model meter tidak dikenal."
    if not clean["host"]:
        return None, "IP / host wajib diisi."
    if not 1 <= clean["port"] <= 65535:
        return None, "Port harus 1–65535."
    if not 0 <= clean["unit_id"] <= 247:
        return None, "Unit ID harus 0–247."
    if clean["function"] not in FUNCTIONS:
        return None, "Function harus input atau holding."
    if clean["byte_order"] not in BYTE_ORDERS:
        return None, "Urutan byte tidak dikenal."
    if not 0.5 <= clean["interval_s"] <= 3600:
        return None, "Interval harus 0,5–3600 detik."
    return clean, None


# ---------- modbus ----------
def decode_float32(w0, w1, order):
    if order == "swap":
        raw = w1.to_bytes(2, "big") + w0.to_bytes(2, "big")
    elif order == "little":
        raw = w1.to_bytes(2, "little") + w0.to_bytes(2, "little")
    elif order == "badc":
        raw = w0.to_bytes(2, "little") + w1.to_bytes(2, "little")
    else:
        raw = w0.to_bytes(2, "big") + w1.to_bytes(2, "big")
    value = struct.unpack(">f", raw)[0]
    return value if math.isfinite(value) else None  # NaN/inf tidak valid di JSON


def make_reader(client, function):
    method = getattr(client, "read_input_registers" if function == "input" else "read_holding_registers")
    params = inspect.signature(method).parameters
    unit_key = next((k for k in ("device_id", "slave", "unit") if k in params), None)

    def read(address, count, unit):
        kwargs = {unit_key: unit} if unit_key else {}
        result = method(address, count=count, **kwargs)
        if result.isError():
            # Bawa kode exception (0x0A/0x0B dst) supaya bisa diklasifikasikan:
            # 0x0B = slave tidak menjawab (meter), 0x02/0x03 = register/konfigurasi salah.
            raise ModbusResponseError(getattr(result, "exception_code", None),
                                      f"Modbus error @{address}: {result}")
        return result.registers

    return read


def read_meter(client, dev):
    if not client.connected and not client.connect():
        raise ConnectionError(f"gagal konek ke {dev['host']}:{dev['port']}")
    profile = PROFILES[dev.get("model") or DEFAULT_MODEL]
    read = make_reader(client, dev["function"])
    words = {}
    for start, count in profile["blocks"]:
        for offset, word in enumerate(read(start, count, dev["unit_id"])):
            words[start + offset] = word

    def f32(addr):
        return decode_float32(words[addr], words[addr + 1], dev["byte_order"])

    data = {key: [f32(a) for a in regs] for key, regs in profile["phase"].items()}
    data.update({key: f32(addr) for key, addr in profile["total"].items()})
    scale = profile["scale"]
    if scale != 1:
        for key in SCALED_KEYS:
            v = data[key]
            data[key] = [x * scale if x is not None else None for x in v] if isinstance(v, list) else (
                v * scale if v is not None else None)
    return data


# ---------- poller ----------
def log_device_event(device_id, from_state, to_state, kind=None, message=None):
    """Catat perubahan status meter ke tabel device_events + dashboard.log (aturan G)."""
    try:
        with db() as con:
            con.execute(
                "INSERT INTO device_events (device_id, ts, from_state, to_state, kind, message) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (device_id, time.time(), from_state, to_state, kind, message))
    except Exception:
        log.exception("gagal mencatat event status meter %s", device_id)
    logging.getLogger("dashboard").log(
        logging.INFO if to_state in LIVE_STATES else logging.WARNING,
        "status meter %s: %s -> %s (%s) %s", device_id, from_state, to_state,
        kind or "-", message or "")

class Poller:
    """Satu thread per perangkat aktif; hasil terakhir disimpan di memori."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = {}    # id -> {"data", "ts", "error", "failed", "state", "health"}
        self.threads = {}  # id -> (thread, stop_event)

    def start(self, dev):
        stop = threading.Event()
        thread = threading.Thread(target=self._loop, args=(dev, stop), daemon=True)
        self.threads[dev["id"]] = (thread, stop)
        thread.start()

    def stop(self, dev_id):
        entry = self.threads.pop(dev_id, None)
        if entry:
            entry[1].set()
        with self.lock:
            self.state.pop(dev_id, None)

    def restart(self, dev):
        self.stop(dev["id"])
        if dev["enabled"]:
            self.start(dev)

    def snapshot(self, dev_id):
        with self.lock:
            return dict(self.state.get(dev_id, {}))

    def _set(self, dev_id, stop, **kv):
        with self.lock:
            if not stop.is_set():  # jangan menulis ulang state perangkat yang sudah dihapus
                self.state.setdefault(dev_id, {}).update(kv)

    @staticmethod
    def _new_client(dev):
        # retries & timeout dari config.json (aturan B dan D).
        return ModbusTcpClient(dev["host"], port=dev["port"],
                               timeout=STATUS_CFG.timeout_s, retries=STATUS_CFG.retries)

    def _loop(self, dev, stop):
        health = MeterHealth()
        self._set(dev["id"], stop, health=health, state=UNKNOWN, failed=0)
        client = None
        while not stop.is_set():
            began = time.monotonic()
            if client is None:
                client = self._new_client(dev)
            try:
                data = read_meter(client, dev)
            except Exception as exc:
                # Tutup socket dan buat koneksi baru di siklus berikutnya: reconnect bersih.
                client.close()
                client = None
                kind = classify_exception(exc)
                changed = health.on_failure(
                    kind, str(exc) or exc.__class__.__name__, STATUS_CFG)
                patch = {"error": health.last_error, "failed": health.fail_count,
                         "state": health.state}
                if health.state in OFFLINE_STATES:
                    # F: jangan simpan 0 atau nilai lama saat offline - kosongkan saja,
                    # supaya grafik & total kWh tidak menganggap meter ini mengukur.
                    patch["data"] = None
                    patch["ts"] = None
                self._set(dev["id"], stop, **patch)
                if changed:
                    log_device_event(dev["id"], changed[0], changed[1], kind, health.last_error)
            else:
                value_state, reason = classify_values(data, STATUS_CFG)
                changed = health.on_success(value_state, reason)
                self._set(dev["id"], stop, data=data, ts=health.last_seen,
                          error=reason, failed=0, state=health.state)
                if changed:
                    log_device_event(dev["id"], changed[0], changed[1], health.kind, reason)
            interval = health.next_interval(dev["interval_s"], STATUS_CFG)
            stop.wait(max(0.2, interval - (time.monotonic() - began)))
        if client is not None:
            client.close()


POLLER = Poller()


def list_devices():
    """Konfigurasi device + status hasil Poller (health, umur data, endpoint).

    Status yang dikirim dipakai dashboard; nilai `live` hanya diisi kalau meter
    ONLINE/ONLINE_ABNORMAL dan datanya belum basi (aturan H), sehingga aplikasi
    polling yang mati ikut terdeteksi sebagai offline.
    """
    with db() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM devices ORDER BY id")]
    now = time.time()
    for row in rows:
        row["enabled"] = bool(row["enabled"])
        snap = POLLER.snapshot(row["id"])
        health = snap.get("health") or MeterHealth()
        state = health.state if row["enabled"] else DISABLED
        age = (now - health.last_seen) if health.last_seen else None
        row["health"] = {**health.snapshot(), "state": state}
        row["state"] = state
        row["last_seen"] = health.last_seen
        row["data_age_s"] = None if age is None else round(age, 1)
        row["link"] = link_status(age, row["interval_s"], STATUS_CFG)
        row["failed"] = health.fail_count
        row["ts"] = health.last_seen
        row["error"] = health.last_error if row["enabled"] else None
        row["live"] = (snap.get("data")
                       if state in LIVE_STATES and row["link"] != "offline" else None)
    # Aturan I: kalau SEMUA slave di satu endpoint gagal, penyebabnya kemungkinan
    # gateway / kabel RS485 / kabel utama - bukan masing-masing meter.
    endpoints = {}
    for row in rows:
        endpoints.setdefault((row["host"], row["port"]), []).append(row)
    for (host, port), group in endpoints.items():
        suspect = endpoint_suspect([r["state"] for r in group if r["enabled"]])
        for row in group:
            row["endpoint"] = {"host": host, "port": port,
                               "slaves": len(group), "suspect": suspect}
    return rows


def get_device(dev_id):
    with db() as con:
        row = con.execute("SELECT * FROM devices WHERE id=?", (dev_id,)).fetchone()
    return dict(row) if row else None


# ---------- riwayat tren (grafik) ----------
def _sum_values(values):
    cleaned = [v for v in (values or []) if v is not None]
    return sum(cleaned) if cleaned else None


def device_metrics(dev):
    """Ringkas satu pembacaan jadi angka yang dipakai grafik (kW, kVA, V, A, Hz).

    Aturannya sama dengan `summary()` di app.js: daya dan arus dijumlahkan,
    tegangan dirata-rata hanya dari fasa yang bertegangan.
    """
    live = dev.get("live")
    if not live:
        return None
    volts = [v for v in (live.get("v") or []) if v is not None and v > PHASE_MIN_V]
    kw = live.get("p_total")
    kva = live.get("s_total")
    if kw is None:
        kw = _sum_values(live.get("p"))
    if kva is None:
        kva = _sum_values(live.get("s"))
    return {
        "kw": (kw or 0) / 1000,
        "kva": (kva or 0) / 1000,
        "v": (sum(volts) / len(volts)) if volts else None,
        "a": _sum_values(live.get("i")),
        "freq": live.get("freq"),
    }


def record_trend():
    """Catat pembacaan terkini semua perangkat aktif ke tabel trend_samples."""
    ts = int(time.time())
    rows = []
    for dev in list_devices():
        if not dev["enabled"]:
            continue
        metrics = device_metrics(dev)
        if not metrics:
            continue
        rows.append((ts, dev["id"], dev.get("unit_id"), (dev.get("area") or "").strip().lower(),
                     (dev.get("site") or "").strip().lower(),
                     metrics["kw"], metrics["kva"], metrics["v"], metrics["a"], metrics["freq"]))
    if not rows:
        return 0
    with db() as con:
        con.executemany(
            "INSERT OR REPLACE INTO trend_samples "
            "(ts, device_id, unit_id, area, site, kw, kva, v, a, freq) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def prune_trend():
    """Sisakan hanya TREND_POINTS bucket terakhir.

    Berbasis urutan bucket, bukan umur: hasil backfill dari riwayat MySQL boleh
    berumur berhari-hari dan tetap dipertahankan sampai sampel live yang baru
    menggesernya keluar dari 90 terakhir.
    """
    with db() as con:
        cutoff = con.execute(
            "SELECT MIN(ts) FROM (SELECT DISTINCT ts FROM trend_samples ORDER BY ts DESC LIMIT ?)",
            (TREND_POINTS,)).fetchone()[0]
        if cutoff is None:
            return
        dropped = con.execute("DELETE FROM trend_samples WHERE ts < ?", (cutoff,)).rowcount
    if dropped:
        log.info("prune tren: %d baris lama dibuang", dropped)


def trend_bucket_count():
    with db() as con:
        return con.execute("SELECT COUNT(DISTINCT ts) FROM trend_samples").fetchone()[0]


def prune_events():
    """Buang riwayat event yang lebih tua dari event_keep_days (config.json)."""
    cutoff = time.time() - float(CFG["event_keep_days"]) * 86400
    with db() as con:
        con.execute("DELETE FROM device_events WHERE ts < ?", (cutoff,))


def backfill_trend():
    """Isi trend_samples dari riwayat `power_readings` (MySQL) yang sudah ada.

    Tujuannya supaya grafik langsung penuh dengan 90 data terakhir, bukan menunggu
    akumulasi dari nol. Yang diambil hanya TREND_POINTS bucket terbaru; setelah itu
    tren baru tetap ditambahkan sendiri oleh TrendRecorder.
    """
    cfg = get_active_connection()
    if not cfg:
        return 0
    try:
        conn = mysql_connect(cfg)
    except Exception as exc:
        log.warning("backfill tren dilewati: MySQL tidak bisa dihubungi (%s)", exc)
        return 0
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT recorded_at, device_id, unit_id, v1, v2, v3, i1, i2, i3, p_total, s_total, freq "
            "FROM power_readings ORDER BY recorded_at DESC LIMIT %s", (BACKFILL_SOURCE_ROWS,))
        rows = cur.fetchall()
        cur.close()
    except Exception as exc:
        log.warning("backfill tren gagal membaca power_readings: %s", exc)
        return 0
    finally:
        conn.close()

    if not rows:
        return 0

    with db() as con:
        areas = {row["id"]: (row["area"] or "").strip().lower()
                 for row in con.execute("SELECT id, area FROM devices")}
        sites = {row["id"]: (row["site"] or "").strip().lower()
                 for row in con.execute("SELECT id, site FROM devices")}

    buckets = {}
    for row in rows:
        buckets.setdefault(int(row["recorded_at"].timestamp()), []).append(row)

    values = []
    for ts in sorted(buckets, reverse=True)[:TREND_POINTS]:
        for row in buckets[ts]:
            volts = [v for v in (row["v1"], row["v2"], row["v3"])
                     if v is not None and v > PHASE_MIN_V]
            amps = [x for x in (row["i1"], row["i2"], row["i3"]) if x is not None]
            values.append((ts, row["device_id"], row.get("unit_id"),
                           areas.get(row["device_id"], ""), sites.get(row["device_id"], ""),
                           (row["p_total"] or 0) / 1000, (row["s_total"] or 0) / 1000,
                           sum(volts) / len(volts) if volts else None,
                           sum(amps) if amps else None, row["freq"]))
    if not values:
        return 0
    with db() as con:
        con.executemany(
            "INSERT OR REPLACE INTO trend_samples "
            "(ts, device_id, unit_id, area, site, kw, kva, v, a, freq) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", values)
    log.info("backfill tren: %d baris dari %d bucket riwayat MySQL", len(values), len(buckets))
    return len(values)


TREND_METRICS = {"kw": "SUM(kw)", "kva": "SUM(kva)", "v": "AVG(v)", "a": "SUM(a)"}


def trend_series(area="all", metric="kw", limit=TREND_POINTS, site="all"):
    """Titik tren terakhir untuk satu site/area + metrik, diagregasi per detik pencatatan.

    'all' menjumlahkan semua perangkat; nilai lain difilter lewat kolom `site`/`area`
    yang sudah dinormalkan (huruf kecil) seperti `areaKey()` di app.js. Site adalah
    tingkat di atas area, jadi keduanya bisa digabung.
    """
    expr = TREND_METRICS[metric]
    where, params = [], []
    if area and area != "all":
        where.append("area = ?")
        params.append(str(area).strip().lower())
    if site and site != "all":
        where.append("site = ?")
        params.append(str(site).strip().lower())
    where = ("WHERE " + " AND ".join(where)) if where else ""
    sql = (f"SELECT ts, {expr} AS value FROM trend_samples {where} "
           f"GROUP BY ts ORDER BY ts DESC LIMIT ?")
    with db() as con:
        rows = con.execute(sql, (*params, int(limit))).fetchall()
    return [{"t": row["ts"], "v": None if row["value"] is None else round(row["value"], 3)}
            for row in reversed(rows)]


# ---------- riwayat harian/mingguan/bulanan (MySQL power_readings) ----------
HISTORY_PERIODS = {"daily": 1, "weekly": 7, "monthly": 30}
HISTORY_UNITS = {"kw": ("kW", 2), "kva": ("kVA", 2), "v": ("V", 1), "a": ("A", 2)}


def history_buckets(period):
    """(mulai, selesai, daftar awal bucket, format, label) dalam waktu lokal server."""
    now = datetime.now()
    if period == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        buckets = [start + timedelta(hours=h) for h in range(24)]
        labels = [bucket.strftime("%H:00") for bucket in buckets]
        return start, start + timedelta(days=1), buckets, "%Y-%m-%d %H", labels
    days = HISTORY_PERIODS[period]
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    buckets = [start + timedelta(days=d) for d in range(days)]
    labels = [bucket.strftime("%d/%m") for bucket in buckets]
    return start, start + timedelta(days=days), buckets, "%Y-%m-%d", labels


def history_row_value(row, metric):
    """Nilai satu baris agregat per detik untuk metrik yang diminta."""
    if metric == "kw":
        return row.get("kw")
    if metric == "kva":
        return row.get("kva")
    if metric == "a":
        return row.get("amp")
    volts = [v for v in (row.get("v1"), row.get("v2"), row.get("v3"))
             if v is not None and v > PHASE_MIN_V]
    return sum(volts) / len(volts) if volts else None


def history_empty(period, metric, buckets, labels, start, end):
    unit, dec = HISTORY_UNITS[metric]
    return {
        "period": period,
        "metric": metric,
        "unit": unit,
        "dec": dec,
        "bucket": "hour" if period == "daily" else "day",
        "labels": labels,
        "points": [{"t": int(bucket.timestamp()), "v": None, "peak": None, "meters": 0}
                   for bucket in buckets],
        "summary": {"avg": None, "peak": None, "meters": 0, "buckets": len(buckets),
                    "with_data": 0, "from": start.isoformat(timespec="minutes"),
                    "to": end.isoformat(timespec="minutes")},
    }


def _bucket_values(rows, index, metric, count):
    """Kumpulkan nilai per bucket dari baris agregat per detik."""
    values = [[] for _ in range(count)]
    meters = [0] * count
    for row in rows:
        i = index.get(row["bucket"])
        if i is None:
            continue
        value = history_row_value(row, metric)
        if value is not None:
            values[i].append(value)
        meters[i] = max(meters[i], int(row["meters"] or 0))
    return values, meters


def _series_points(values, meters, buckets):
    return [{"t": int(bucket.timestamp()),
             "v": round(sum(values[i]) / len(values[i]), 3) if values[i] else None,
             "peak": round(max(values[i]), 3) if values[i] else None,
             "meters": meters[i]} for i, bucket in enumerate(buckets)]


def history_series(period, metric, area="all", site="all"):
    """Agregasi riwayat MySQL per jam (harian) atau per hari (mingguan/bulanan).

    Return (payload, error). Satu titik = rata-rata total semua meter pada bucket
    itu; bucket tanpa data dikembalikan null (bukan 0) supaya celah terlihat.
    Filter site dan area digabung (site = tingkat di atas area).
    """
    cfg = get_active_connection()
    if not cfg:
        return None, "Belum ada koneksi database aktif."

    start, end, buckets, fmt, labels = history_buckets(period)
    index = {bucket.strftime(fmt): i for i, bucket in enumerate(buckets)}

    filters, filter_params = [], []
    if area and area != "all":
        filters.append("lower(trim(area)) = ?")
        filter_params.append(str(area).strip().lower())
    if site and site != "all":
        filters.append("lower(trim(site)) = ?")
        filter_params.append(str(site).strip().lower())
    device_ids = None
    if filters:
        with db() as con:
            device_ids = [row["id"] for row in con.execute(
                "SELECT id FROM devices WHERE " + " AND ".join(filters), filter_params)]
        if not device_ids:
            return history_empty(period, metric, buckets, labels, start, end), None

    where = "recorded_at >= %s AND recorded_at < %s"
    params = [start, end]
    if device_ids:
        where += " AND device_id IN (%s)" % ",".join(["%s"] * len(device_ids))
        params.extend(device_ids)

    sql = (
        "SELECT DATE_FORMAT(recorded_at, %s) AS bucket, "
        "       SUM(p_total) / 1000 AS kw, "
        "       SUM(s_total) / 1000 AS kva, "
        "       SUM(COALESCE(i1, 0) + COALESCE(i2, 0) + COALESCE(i3, 0)) AS amp, "
        "       AVG(v1) AS v1, AVG(v2) AS v2, AVG(v3) AS v3, "
        "       COUNT(DISTINCT device_id) AS meters "
        "FROM power_readings WHERE " + where +
        " GROUP BY FLOOR(UNIX_TIMESTAMP(recorded_at)), bucket")
    count_sql = "SELECT COUNT(DISTINCT device_id) AS meters FROM power_readings WHERE " + where

    try:
        conn = mysql_connect(cfg)
    except Exception as exc:
        return None, f"MySQL tidak bisa dihubungi: {exc}"
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(sql, [fmt, *params])
        rows = cur.fetchall()
        cur.execute(count_sql, params)
        total_meters = int((cur.fetchone() or {}).get("meters") or 0)
        cur.close()
    except Exception as exc:
        return None, f"gagal membaca power_readings: {exc}"
    finally:
        conn.close()

    values, meters = _bucket_values(rows, index, metric, len(buckets))
    points = _series_points(values, meters, buckets)
    filled = [p["v"] for p in points if p["v"] is not None]
    peaks = [p["peak"] for p in points if p["peak"] is not None]
    unit, dec = HISTORY_UNITS[metric]

    return {
        "period": period,
        "metric": metric,
        "unit": unit,
        "dec": dec,
        "bucket": "hour" if period == "daily" else "day",
        "labels": labels,
        "points": points,
        "summary": {
            "avg": round(sum(filled) / len(filled), 3) if filled else None,
            "peak": round(max(peaks), 3) if peaks else None,
            "meters": total_meters,
            "buckets": len(buckets),
            "with_data": len(filled),
            "from": start.isoformat(timespec="minutes"),
            "to": end.isoformat(timespec="minutes"),
        },
    }, None


class TrendRecorder:
    """Thread pencatat tren.
    dicoba ulang tiap 60 detik selama riwayat belum mencapai TREND_POINTS bucket
    (mis. saat MySQL belum siap ketika server baru start).
    """

    def __init__(self):
        self.stop_event = threading.Event()

    def run(self):
        prune_due = backfill_due = 0.0
        while not self.stop_event.is_set():
            try:
                if time.monotonic() >= backfill_due:
                    backfill_due = time.monotonic() + 60
                    if trend_bucket_count() < TREND_POINTS:
                        backfill_trend()
                record_trend()
                if time.monotonic() >= prune_due:
                    prune_trend()
                    prune_events()
                    prune_due = time.monotonic() + 60
            except Exception:
                log.exception("gagal mencatat tren")
            self.stop_event.wait(TREND_INTERVAL_S)

    def stop(self):
        self.stop_event.set()


TREND = TrendRecorder()


# ---------- koneksi database (MySQL) ----------
# Koneksi diatur di sisi server (database.json, ditulis oleh setup_db.py / SETUP.bat), bukan lewat browser.
def validate_conn(body):
    """Return (clean_dict, error_message)."""
    try:
        clean = {
            "host": str(body.get("host", "")).strip(),
            "port": int(body.get("port", 3306)),
            "db_user": str(body.get("db_user", "")).strip(),
            "db_password": str(body.get("db_password", "")),
            "db_name": str(body.get("db_name") or DEFAULT_DB_NAME).strip(),
            "store_interval_s": float(body.get("store_interval_s", 60)),
        }
    except (TypeError, ValueError):
        return None, "Port dan interval simpan harus berupa angka."
    if not clean["host"]:
        return None, "Host server wajib diisi."
    if not 1 <= clean["port"] <= 65535:
        return None, "Port harus 1–65535."
    if not clean["db_user"]:
        return None, "User database wajib diisi."
    if not DB_NAME_RE.match(clean["db_name"]):
        return None, "Nama database hanya boleh huruf, angka, dan underscore (maks. 64 karakter)."
    if not 1 <= clean["store_interval_s"] <= 86400:
        return None, "Interval simpan harus 1–86400 detik."
    return clean, None


def save_db_config(cfg):
    """Tulis database.json secara atomik (berisi password MySQL -> jangan dibagikan)."""
    tmp = DB_CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({k: cfg[k] for k in CONN_FIELDS}, indent=2), encoding="utf-8")
    os.replace(tmp, DB_CONFIG_PATH)


def get_active_connection():
    """Koneksi MySQL dari database.json; None bila belum di-setup atau isinya tidak valid."""
    try:
        raw = json.loads(DB_CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.error("database.json tidak bisa dibaca: %s", exc)
        return None
    clean, err = validate_conn(raw if isinstance(raw, dict) else {})
    if err:
        log.error("database.json tidak valid: %s", err)
    return clean


def migrate_legacy_connection():
    """Upgrade dari versi lama: pindahkan koneksi aktif di meters.db ke database.json (sekali)."""
    if DB_CONFIG_PATH.exists():
        return
    with db() as con:
        try:
            row = con.execute("SELECT * FROM db_connections WHERE active=1").fetchone()
        except sqlite3.OperationalError:
            return  # tabel lama tidak ada = instalasi baru
    if not row:
        return
    clean, err = validate_conn(dict(row))
    if err:
        log.warning("koneksi lama tidak dipindahkan ke database.json: %s", err)
        return
    save_db_config(clean)
    log.info("koneksi MySQL lama dipindahkan ke database.json")


def mysql_connect(cfg, with_db=True):
    kwargs = dict(host=cfg["host"], port=cfg["port"], user=cfg["db_user"], password=cfg["db_password"],
                  connection_timeout=5)
    if with_db:
        kwargs["database"] = cfg["db_name"]
    return mysql.connector.connect(**kwargs)


def test_connection(cfg):
    """Cek login ke server dan keberadaan database tanpa mengubah apa pun."""
    try:
        con = mysql_connect(cfg, with_db=False)
    except mysql.connector.Error as exc:
        return {"ok": False, "error": str(exc)}
    try:
        cur = con.cursor()
        cur.execute("SELECT VERSION()")
        version = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s", (cfg["db_name"],))
        return {"ok": True, "version": version, "database_exists": cur.fetchone()[0] > 0}
    except mysql.connector.Error as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        con.close()


def ensure_schema(cfg):
    """Buat database (kalau belum ada) dan tabel penyimpanan. Dipanggil saat koneksi diaktifkan."""
    con = mysql_connect(cfg, with_db=False)
    try:
        cur = con.cursor()
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{cfg['db_name']}` CHARACTER SET utf8mb4")
        cur.execute(f"USE `{cfg['db_name']}`")
        cur.execute(READINGS_DDL)
        # Migrasi tabel lama: kolom source tidak dipakai lagi (identitas kolektor tidak
        # dibutuhkan) -> hapus kolom + rapikan kunci unik menjadi (device_id, recorded_at).
        cur.execute("SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s "
                    "AND TABLE_NAME='power_readings' AND COLUMN_NAME='source'", (cfg["db_name"],))
        if cur.fetchone()[0]:
            cur.execute("SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s "
                        "AND TABLE_NAME='power_readings' AND INDEX_NAME='uq_sample'", (cfg["db_name"],))
            if cur.fetchone()[0]:
                cur.execute("ALTER TABLE power_readings DROP INDEX uq_sample")
            cur.execute("ALTER TABLE power_readings DROP COLUMN source")
        # Kunci unik (device_id, recorded_at) membuat INSERT IGNORE idempotent saat batch
        # dikirim ulang. Duplikat lama disingkirkan dulu; salinannya disimpan di tabel backup.
        cur.execute("SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s "
                    "AND TABLE_NAME='power_readings' AND INDEX_NAME='uq_sample'", (cfg["db_name"],))
        if not cur.fetchone()[0]:
            cur.execute("CREATE TABLE IF NOT EXISTS power_readings_dup_backup LIKE power_readings")
            cur.execute(
                "INSERT INTO power_readings_dup_backup "
                "SELECT p.* FROM power_readings p JOIN ("
                "  SELECT device_id, recorded_at, MAX(id) AS keep_id FROM power_readings "
                "  GROUP BY device_id, recorded_at HAVING COUNT(*) > 1) d "
                "ON p.device_id = d.device_id AND p.recorded_at = d.recorded_at "
                "AND p.id <> d.keep_id")
            cur.execute(
                "DELETE p FROM power_readings p JOIN ("
                "  SELECT device_id, recorded_at, MAX(id) AS keep_id FROM power_readings "
                "  GROUP BY device_id, recorded_at HAVING COUNT(*) > 1) d "
                "ON p.device_id = d.device_id AND p.recorded_at = d.recorded_at "
                "AND p.id <> d.keep_id")
            cur.execute("ALTER TABLE power_readings ADD UNIQUE KEY uq_sample (device_id, recorded_at)")
        # Migrasi kolom site (lokasi) untuk pelaporan per site dari MySQL.
        cur.execute("SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s "
                    "AND TABLE_NAME='power_readings' AND COLUMN_NAME='site'", (cfg["db_name"],))
        if not cur.fetchone()[0]:
            cur.execute("ALTER TABLE power_readings ADD COLUMN site VARCHAR(100) NULL AFTER freq")
        cur.execute("SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s "
                    "AND TABLE_NAME='power_readings' AND INDEX_NAME='idx_site_time'", (cfg["db_name"],))
        if not cur.fetchone()[0]:
            cur.execute("ALTER TABLE power_readings ADD INDEX idx_site_time (site, recorded_at)")
        con.commit()
    finally:
        con.close()


def prepare_schema(cfg):
    """Siapkan DB/tabel di MySQL. User tanpa hak CREATE/ALTER dianggap skemanya sudah disiapkan DBA."""
    try:
        ensure_schema(cfg)
    except mysql.connector.Error as exc:
        if exc.errno not in MYSQL_NO_PRIVILEGE:
            raise


class Storer:
    """Dua tahap supaya data tidak hilang saat MySQL/jaringan putus:
    1) sampler mencatat pembacaan terakhir semua meter ke tabel outbox lokal tiap store_interval_s;
    2) sender mengirim outbox ke MySQL per batch dan menghapus baris yang sudah terkirim."""

    def __init__(self):
        self.lock = threading.Lock()
        self.wake = threading.Event()  # di-set saat koneksi aktif berubah
        self.interval = None           # interval sampling koneksi aktif; None = tidak ada koneksi aktif
        self.info = {"state": "idle", "error": None, "last_write": None, "rows_total": 0}

    def reload(self):
        self.wake.set()

    def status(self, with_pending=True):
        with self.lock:
            info = dict(self.info)
        if with_pending:
            with db() as con:
                info["pending"] = con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        return info

    def _set(self, **kv):
        with self.lock:
            self.info.update(kv)

    def _rows(self):
        rows = []
        for d in list_devices():
            live = d["live"]
            if not (d["enabled"] and live):
                continue
            values = [live[k][n] for k in ("v", "i", "p", "s") for n in range(3)]
            stamp = datetime.fromtimestamp(d["ts"]).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            rows.append((stamp, d["id"], d["name"], d["host"], d["unit_id"],
                         *values, live["p_total"], live["s_total"], live["freq"],
                         (d.get("site") or "").strip()))
        return rows

    def _sample(self):
        rows = self._rows()
        if not rows:
            return
        with db() as con:
            con.executemany(
                f"INSERT INTO outbox ({', '.join(SAMPLE_COLUMNS)}) VALUES ({', '.join('?' * len(SAMPLE_COLUMNS))})", rows)
            dropped = con.execute("DELETE FROM outbox WHERE id <= (SELECT MAX(id) FROM outbox) - ?",
                                  (OUTBOX_MAX_ROWS,)).rowcount
        if dropped:
            log.warning("outbox penuh (%d baris): %d sampel terlama dibuang", OUTBOX_MAX_ROWS, dropped)

    def _flush_batch(self, conn):
        """Kirim satu batch outbox. Return jumlah baris terkirim (0 = outbox kosong)."""
        with db() as con:
            batch = con.execute("SELECT * FROM outbox ORDER BY id LIMIT ?", (BATCH_SIZE,)).fetchall()
        if not batch:
            return 0
        cur = conn.cursor()
        cur.executemany(READINGS_INSERT, [tuple(r[c] for c in SAMPLE_COLUMNS) for r in batch])
        conn.commit()
        cur.close()
        with db() as con:
            con.execute("DELETE FROM outbox WHERE id <= ?", (batch[-1]["id"],))
        return len(batch)

    def run_sampler(self):
        next_at = 0.0
        while True:
            interval = self.interval
            if interval and time.monotonic() >= next_at:
                try:
                    self._sample()
                except Exception:
                    log.exception("gagal mencatat sampel ke outbox")
                next_at = time.monotonic() + interval
            time.sleep(1)

    def run(self):
        conn, cfg, next_at, health_at, schema_ok = None, None, 0.0, 0.0, False
        while True:
            if self.wake.is_set():
                self.wake.clear()
                if conn:
                    conn.close()
                conn, cfg, next_at, health_at, schema_ok = None, get_active_connection(), 0.0, 0.0, False
                self.interval = cfg["store_interval_s"] if cfg else None
                self._set(state="connecting" if cfg else "idle", error=None, last_write=None, rows_total=0)

            # Ping berkala: putusnya MySQL terdeteksi cepat walau outbox kosong / belum waktunya kirim.
            if cfg and time.monotonic() >= health_at:
                try:
                    if not schema_ok:
                        prepare_schema(cfg)
                        schema_ok = True
                    conn = conn or mysql_connect(cfg)
                    conn.ping(reconnect=False)
                    if self.info["state"] == "error":
                        log.info("koneksi MySQL pulih")
                        self._set(state="ok", error=None)
                except Exception as exc:
                    if conn:
                        try:
                            conn.close()
                        except Exception:
                            pass
                    conn = None
                    if self.info["state"] != "error":
                        log.warning("koneksi MySQL terputus (data ditahan di outbox): %s", exc)
                    self._set(state="error", error=str(exc) or exc.__class__.__name__)
                health_at = time.monotonic() + HEALTH_CHECK_S

            if cfg and time.monotonic() >= next_at:
                retry = cfg["store_interval_s"]
                try:
                    conn = conn or mysql_connect(cfg)
                    sent = 0
                    while True:
                        n = self._flush_batch(conn)
                        sent += n
                        if n < BATCH_SIZE:
                            break
                    if sent:
                        with self.lock:
                            self.info.update(state="ok", error=None, last_write=time.time(),
                                             rows_total=self.info["rows_total"] + sent)
                except Exception as exc:
                    if conn:
                        conn.close()
                    conn = None
                    if self.info["state"] != "error":
                        log.warning("gagal kirim ke MySQL (data ditahan di outbox): %s", exc)
                    self._set(state="error", error=str(exc) or exc.__class__.__name__)
                    retry = min(retry, MAX_RETRY_S)  # server pabrik putus: coba lagi lebih cepat
                next_at = time.monotonic() + retry
            self.wake.wait(1)


STORER = Storer()


# ---------- http ----------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # request biasa tidak perlu dicatat

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _token(self):
        auth = self.headers.get("Authorization") or ""
        return auth[7:] if auth.startswith("Bearer ") else None

    def _session(self):
        return get_session(self._token())

    def _login(self):
        """POST /api/login: tukar username+password dengan token sesi."""
        length = self.headers.get("Content-Length") or "0"
        if not length.isdigit() or int(length) > MAX_BODY_BYTES:
            return self._json(413, {"error": "body tidak valid atau terlalu besar"})
        body = self._body()
        if body is None:
            return self._json(400, {"error": "body harus object JSON"})
        user = authenticate(str(body.get("username", "")).strip(), str(body.get("password", "")))
        if not user:
            return self._json(401, {"error": "username atau password salah"})
        token = create_session(user["username"], user["role"])
        self._json(200, {"token": token, "username": user["username"], "role": user["role"]})

    def _guard(self, method, path):
        """Autentikasi + otorisasi + validasi dasar. Return True kalau request sudah ditolak.

        Static file tetap publik. Semua /api butuh sesi; /api/login pengecualian.
        Hanya role admin yang boleh menulis data atau mengakses koneksi database."""
        if not path.startswith("/api/"):
            return False
        if path == "/api/login":
            return False
        sess = self._session()
        if not sess:
            self._json(401, {"error": "belum login"})
            return True
        if sess["role"] != "admin":
            admin_only = (path.startswith("/api/db-status") or path.startswith("/api/users")
                          or (path.startswith("/api/devices") and method != "GET"))
            if admin_only:
                self._json(403, {"error": "butuh akses admin"})
                return True
        if method == "GET":
            return False
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            self._json(403, {"error": "origin ditolak"})
        elif not (self.headers.get("Content-Type") or "").startswith("application/json"):
            self._json(415, {"error": "Content-Type harus application/json"})
        elif not (self.headers.get("Content-Length") or "0").isdigit() or int(self.headers.get("Content-Length") or 0) > MAX_BODY_BYTES:
            self._json(413, {"error": "body tidak valid atau terlalu besar"})
        else:
            return False
        return True

    def _body(self):
        """Parse body JSON sekali. Return None bila bukan object (dipakai untuk balas 400)."""
        if not hasattr(self, "_body_cache"):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                data = None
            self._body_cache = data if isinstance(data, dict) else None
        return self._body_cache

    def _dev_id(self, path):
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "devices"] and parts[2].isdigit():
            return int(parts[2])
        return None

    def _users_route(self, method, path):
        """Rute /api/users (khusus admin): daftar akun & ubah password."""
        parts = path.strip("/").split("/")
        if parts[:2] != ["api", "users"]:
            return False
        rest = parts[2:]
        user_id = int(rest[0]) if rest and rest[0].isdigit() else None

        if method == "GET" and not rest:
            with db() as con:
                rows = [{"id": r["id"], "username": r["username"], "role": r["role"]}
                        for r in con.execute("SELECT id, username, role FROM users ORDER BY id")]
            self._json(200, rows)
            return True

        existing = get_user(user_id) if user_id else None
        if not existing:
            self._json(404, {"error": "akun tidak ditemukan"})
            return True

        if method == "PUT" and len(rest) == 1:
            password = str((self._body() or {}).get("password", ""))
            if len(password) < 6:
                self._json(400, {"error": "Password minimal 6 karakter"})
            else:
                with db() as con:
                    con.execute("UPDATE users SET password_hash=? WHERE id=?",
                                (hash_password(password), user_id))
                drop_sessions_for(existing["username"])
                self._json(200, {"ok": True})
            return True

        self._json(404, {"error": "tidak ditemukan"})
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        if self._guard("GET", path):
            return
        if self._users_route("GET", path):
            return
        if path == "/api/me":
            sess = self._session()
            return self._json(200, {"username": sess["username"], "role": sess["role"]})
        if path == "/api/db-status":  # admin; read-only, password tidak pernah dikirim
            cfg = get_active_connection()
            shown = {k: cfg[k] for k in ("host", "port", "db_user", "db_name", "store_interval_s")} if cfg else None
            return self._json(200, {"config": shown, "status": STORER.status()})
        if path == "/api/status":
            s = STORER.status(with_pending=False)
            return self._json(200, {"db": {"state": s["state"], "error": s["error"],
                                           "last_write": s["last_write"]}})
        if path == "/api/devices":
            return self._json(200, list_devices())
        if path == "/api/trend":
            query = parse_qs(urlparse(self.path).query)
            area = (query.get("area", ["all"])[0] or "all").strip()
            site = (query.get("site", ["all"])[0] or "all").strip()
            metric = query.get("metric", ["kw"])[0]
            if metric not in TREND_METRICS:
                return self._json(400, {"error": "metrik tidak dikenal"})
            try:
                limit = int(query.get("limit", [TREND_POINTS])[0])
            except ValueError:
                limit = TREND_POINTS
            limit = max(1, min(TREND_POINTS, limit))
            return self._json(200, {"area": area, "site": site, "metric": metric,
                                    "interval_s": TREND_INTERVAL_S,
                                    "points": trend_series(area, metric, limit, site)})
        if path == "/api/history":
            query = parse_qs(urlparse(self.path).query)
            period = query.get("period", ["daily"])[0]
            metric = query.get("metric", ["kw"])[0]
            area = (query.get("area", ["all"])[0] or "all").strip()
            site = (query.get("site", ["all"])[0] or "all").strip()
            if period not in HISTORY_PERIODS:
                return self._json(400, {"error": "periode tidak dikenal"})
            if metric not in HISTORY_UNITS:
                return self._json(400, {"error": "metrik tidak dikenal"})
            payload_data, error = history_series(period, metric, area, site)
            if error:
                return self._json(503, {"error": error})
            return self._json(200, payload_data)
        if path.startswith("/api/"):
            return self._json(404, {"error": "tidak ditemukan"})
        target = (STATIC / ("index.html" if path == "/" else path.lstrip("/"))).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            return self._json(404, {"error": "tidak ditemukan"})
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/login":
            return self._login()
        if self._guard("POST", path):
            return
        if self._body() is None:
            return self._json(400, {"error": "body harus object JSON"})
        if path == "/api/logout":
            drop_session(self._token())
            return self._json(200, {"ok": True})
        if path != "/api/devices":
            return self._json(404, {"error": "tidak ditemukan"})
        clean, err = validate(self._body() or {})
        if err:
            return self._json(400, {"error": err})
        clean["area"] = canonical_area(clean["area"])
        clean["site"] = canonical_site(clean["site"])
        with db() as con:
            cur = con.execute(
                f"INSERT INTO devices ({','.join(FIELDS)}) VALUES ({','.join('?' * len(FIELDS))})",
                [clean[f] for f in FIELDS],
            )
        dev = get_device(cur.lastrowid)
        if dev["enabled"]:
            POLLER.start(dev)
        self._json(201, dev)

    def do_PUT(self):
        path = urlparse(self.path).path
        if self._guard("PUT", path):
            return
        if self._body() is None:
            return self._json(400, {"error": "body harus object JSON"})
        if self._users_route("PUT", path):
            return
        dev_id = self._dev_id(path)
        if dev_id is None or not get_device(dev_id):
            return self._json(404, {"error": "perangkat tidak ditemukan"})
        clean, err = validate(self._body() or {})
        if err:
            return self._json(400, {"error": err})
        clean["area"] = canonical_area(clean["area"])
        clean["site"] = canonical_site(clean["site"])
        with db() as con:
            con.execute(
                f"UPDATE devices SET {','.join(f + '=?' for f in FIELDS)} WHERE id=?",
                [clean[f] for f in FIELDS] + [dev_id],
            )
        dev = get_device(dev_id)
        POLLER.restart(dev)
        self._json(200, dev)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if self._guard("DELETE", path):
            return
        if self._body() is None:
            return self._json(400, {"error": "body harus object JSON"})
        dev_id = self._dev_id(path)
        if dev_id is None or not get_device(dev_id):
            return self._json(404, {"error": "perangkat tidak ditemukan"})
        POLLER.stop(dev_id)
        with db() as con:
            con.execute("DELETE FROM devices WHERE id=?", (dev_id,))
        self._json(200, {"ok": True})


def setup_logging():
    """Log ke file berputar (hanya kejadian penting: error & perubahan status) untuk debugging tanpa operator."""
    handler = logging.handlers.RotatingFileHandler(BASE / "dashboard.log", maxBytes=1_000_000, backupCount=3,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def lan_addresses():
    """Alamat IPv4 non-loopback mesin ini, untuk ditampilkan saat bind ke 0.0.0.0."""
    addrs = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # tidak mengirim paket, hanya memilih rute keluar
            addrs.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(a for a in addrs if not a.startswith("127.") and a != "0.0.0.0")


def main():
    parser = argparse.ArgumentParser(description="Dashboard listrik Modbus TCP")
    parser.add_argument("--host", default="0.0.0.0",
                        help="alamat bind (default: 0.0.0.0 = bisa diakses dari jaringan)")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    setup_logging()
    print(f"Polling: timeout {STATUS_CFG.timeout_s:.1f}s (gateway "
          f"{CFG['gateway_response_timeout_s']:.1f}s), retry {STATUS_CFG.retries}, "
          f"offline setelah {STATUS_CFG.fail_threshold}x gagal "
          f"(poll tiap {STATUS_CFG.offline_interval_s:.0f}s), tegangan valid "
          f"{STATUS_CFG.voltage_min_v:.0f}-{STATUS_CFG.voltage_max_v:.0f} V")
    log.info("konfigurasi polling: timeout=%.1fs retries=%d fail_threshold=%d "
             "offline_interval=%.0fs tegangan=%.0f-%.0fV", STATUS_CFG.timeout_s,
             STATUS_CFG.retries, STATUS_CFG.fail_threshold, STATUS_CFG.offline_interval_s,
             STATUS_CFG.voltage_min_v, STATUS_CFG.voltage_max_v)

    global _INSTANCE_LOCK
    _INSTANCE_LOCK = acquire_instance_lock()
    if _INSTANCE_LOCK is None:
        print(f"Server lain sudah memakai {DB_PATH} (lock: {DB_PATH.name}.lock). Hentikan dulu prosesnya.")
        return 1

    init_db()
    for dev in list_devices():
        if dev["enabled"]:
            POLLER.start(dev)
    threading.Thread(target=STORER.run_sampler, daemon=True).start()
    threading.Thread(target=STORER.run, daemon=True).start()
    threading.Thread(target=TREND.run, daemon=True).start()
    STORER.reload()  # muat koneksi dari database.json
    if not get_active_connection():
        print("PERINGATAN: database.json belum ada/valid -> data tidak disimpan ke MySQL. Jalankan SETUP.bat.")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Dashboard (lokal):     http://127.0.0.1:{args.port}")
    if args.host in ("0.0.0.0", ""):
        for ip in lan_addresses():
            print(f"Dashboard (jaringan):  http://{ip}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDihentikan.")


if __name__ == "__main__":
    sys.exit(main())
