"""Logika status meter kWh - murni, tanpa I/O Modbus.

Semua fungsi di sini hanya menerima nilai (hasil pembacaan / exception) dan
mengembalikan status. Tidak ada import pymodbus, socket, atau database, sehingga
bagian ini bisa dipindah apa adanya ke Node-RED tanpa mengubah jalur pembacaan.

Alur singkat:
    respons sukses  -> classify_values()  -> ONLINE / ONLINE_ABNORMAL
    gagal membaca   -> classify_exception() -> gateway | meter | device
    status koneksi  -> link_status() (dari umur data terakhir)

Keadaan meter:
    UNKNOWN          belum pernah berhasil dibaca
    ONLINE           data valid dan wajar
    ONLINE_ABNORMAL  meter menjawab, tapi nilai tidak wajar (mis. tegangan 0 V)
    OFFLINE_METER    TCP ke gateway nyambung, slave tidak menjawab (RS485/alamat)
    OFFLINE_GATEWAY  TCP ke gateway gagal (gateway mati / kabel LAN / IP salah)
    DEVICE_ERROR     meter menjawab tapi permintaan ditolak (0x02/0x03, decode gagal)
    DISABLED         polling dimatikan admin
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

UNKNOWN = "UNKNOWN"
ONLINE = "ONLINE"
ONLINE_ABNORMAL = "ONLINE_ABNORMAL"
OFFLINE_METER = "OFFLINE_METER"
OFFLINE_GATEWAY = "OFFLINE_GATEWAY"
DEVICE_ERROR = "DEVICE_ERROR"
DISABLED = "DISABLED"

# Status yang dianggap "meter hidup" (datanya boleh dipakai/disimpan).
# Nilai tidak wajar (ONLINE_ABNORMAL) sengaja TIDAK dianggap hidup: meter yang
# menjawab tapi tegangannya 0 V / di luar rentang tidak boleh mengisi laporan energi.
LIVE_STATES = (ONLINE,)
# Status yang berarti tidak ada data valid yang boleh dipakai.
OFFLINE_STATES = (ONLINE_ABNORMAL, OFFLINE_METER, OFFLINE_GATEWAY, DEVICE_ERROR)
# Status yang dipoll lebih jarang supaya meter lain tidak ikut lambat.
SLOW_POLL_STATES = (ONLINE_ABNORMAL, OFFLINE_METER, OFFLINE_GATEWAY)

# Jenis kegagalan hasil klasifikasi.
KIND_GATEWAY = "gateway"
KIND_METER = "meter"
KIND_DEVICE = "device"

# Dicocokkan lewat nama kelas supaya modul ini tidak perlu mengimpor pymodbus
# (ConnectionError = bawaan Python, ConnectionException = pymodbus, gaierror = DNS).
_CONNECTION_ERRORS = ("ConnectionError", "ConnectionException", "NewConnectionError", "gaierror")
_TIMEOUT_ERRORS = ("ModbusIOException", "TimeoutError", "Timeout", "OSError", "socket.timeout")


class ModbusResponseError(Exception):
    """Meter menjawab tapi menolak permintaan (Modbus exception response)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class StatusConfig:
    """Nilai yang bisa diatur lewat config.json (dipisah dari kode I/O)."""

    timeout_s: float = 5.0
    retries: int = 1
    fail_threshold: int = 3
    offline_interval_s: float = 30.0
    voltage_min_v: float = 180.0
    voltage_max_v: float = 260.0
    age_online_factor: float = 2.0
    age_offline_factor: float = 5.0


def classify_exception(exc) -> str:
    """Klasifikasikan exception pembacaan Modbus.

    gateway : TCP ke gateway gagal -> gateway mati / kabel LAN / IP-port salah
    meter   : TCP nyambung tapi tidak ada jawaban -> RS485 putus / slave ID salah
              (pymodbus melaporkan ini sebagai ModbusIOException)
    device  : meter menjawab tapi menolak permintaan, atau data tidak bisa di-decode
    """
    if isinstance(exc, ModbusResponseError):
        # 0x0A (10) = gateway path unavailable, 0x0B (11) = target device tidak menjawab
        return KIND_METER if exc.code in (10, 11) else KIND_DEVICE
    name = exc.__class__.__name__
    if name in _CONNECTION_ERRORS:
        return KIND_GATEWAY
    if name in _TIMEOUT_ERRORS:
        return KIND_METER
    return KIND_DEVICE


def classify_values(data, cfg: StatusConfig):
    """Nilai satu pembacaan sukses -> (state, alasan).

    Instalasi di sini 3 fasa, jadi tiap fasa wajib ada dan berada di rentang
    tegangan yang dikonfigurasi; fasa 0 V dianggap ABNORMAL, bukan "tidak terpasang".
    """
    problems = []
    volts = list(data.get("v") or [])[:3]
    if not volts:
        problems.append("data tegangan kosong")
    for index, value in enumerate(volts, start=1):
        if value is None:
            problems.append(f"fasa {index} tidak terbaca")
        elif not cfg.voltage_min_v <= value <= cfg.voltage_max_v:
            problems.append(
                f"fasa {index} {value:.1f} V di luar {cfg.voltage_min_v:.0f}-{cfg.voltage_max_v:.0f} V")
    for key in ("i", "p", "s"):
        for index, value in enumerate(list(data.get(key) or [])[:3], start=1):
            if value is None:
                problems.append(f"{key}{index} tidak terbaca")
    if data.get("freq") is None:
        problems.append("frekuensi tidak terbaca")
    if problems:
        return ONLINE_ABNORMAL, "; ".join(problems[:3])
    return ONLINE, None


def link_status(age_s, interval_s, cfg: StatusConfig) -> str:
    """Status dari umur data terakhir - mendeteksi aplikasi polling yang berhenti.

    age < 2x interval  -> online
    age < 5x interval  -> warning (data terlambat)
    selain itu         -> offline
    """
    if age_s is None:
        return "offline"
    base = max(1.0, float(interval_s or 1.0))
    if age_s < base * cfg.age_online_factor:
        return "online"
    if age_s < base * cfg.age_offline_factor:
        return "warning"
    return "offline"


def endpoint_suspect(states) -> bool:
    """True bila semua slave aktif di satu endpoint sedang offline.

    Dipakai untuk aturan I: penyebabnya kemungkinan gateway / kabel RS485 / kabel
    utama, bukan masing-masing meter.
    """
    active = [state for state in states if state != DISABLED]
    if not active:
        return False
    return all(state in (OFFLINE_METER, OFFLINE_GATEWAY) for state in active)


@dataclass
class MeterHealth:
    """Fail counter + status satu meter. Semua transisi dikembalikan ke pemanggil
    sebagai (state_lama, state_baru) supaya pencatatan event tetap di luar modul ini.
    """

    state: str = UNKNOWN
    fail_count: int = 0
    last_seen: float | None = None   # epoch pembacaan sukses terakhir
    last_error: str | None = None
    kind: str | None = None
    state_since: float = field(default_factory=time.time)

    def on_success(self, value_state: str, reason: str | None = None, now: float | None = None):
        now = time.time() if now is None else now
        old = self.state
        self.fail_count = 0
        self.last_seen = now
        self.last_error = reason
        self.kind = None
        self.state = value_state
        return self._transition(old, now)

    def on_failure(self, kind: str, message: str, cfg: StatusConfig, now: float | None = None):
        now = time.time() if now is None else now
        old = self.state
        self.fail_count += 1
        self.last_error = message
        self.kind = kind
        if self.fail_count >= max(1, cfg.fail_threshold):
            if kind == KIND_GATEWAY:
                self.state = OFFLINE_GATEWAY
            elif kind == KIND_DEVICE:
                self.state = DEVICE_ERROR
            else:
                self.state = OFFLINE_METER
        return self._transition(old, now)

    def _transition(self, old: str, now: float):
        if old == self.state:
            return None
        self.state_since = now
        return old, self.state

    def next_interval(self, base_interval: float, cfg: StatusConfig) -> float:
        """Meter yang tidak menghasilkan data valid dipoll lebih jarang."""
        if self.state in SLOW_POLL_STATES:
            return max(float(base_interval), cfg.offline_interval_s)
        return float(base_interval)

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "since": self.state_since,
            "last_seen": self.last_seen,
            "fail_count": self.fail_count,
            "kind": self.kind,
            "message": self.last_error,
        }
