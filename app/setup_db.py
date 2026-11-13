#!/usr/bin/env python3
"""Setup sekali jalan: buat meters.db (SQLite) dan, opsional, database + tabel MySQL.

Memakai fungsi di server.py supaya skema tidak terduplikasi. Aman dijalankan ulang
(idempotent): database.json ditimpa dengan nilai terbaru.

Satu user MySQL dipakai untuk setup maupun runtime. User ini butuh hak CREATE/ALTER pada
database dashboard, dan password-nya disimpan di database.json (sisi server) untuk dipakai server.py.
Koneksi database TIDAK bisa diubah dari browser; ubah dengan menjalankan setup ini lagi.

Contoh:
    python setup_db.py
    python setup_db.py --mysql-host 127.0.0.1 --mysql-user root
Password: env MYSQL_PASSWORD, atau ditanya (tidak tampil di layar).
"""
import argparse
import getpass
import os
import sys

import mysql.connector

import server


def ask_password(env_name, prompt):
    value = os.environ.get(env_name)
    return value if value is not None else getpass.getpass(prompt)


def main():
    p = argparse.ArgumentParser(description="Setup database dashboard")
    p.add_argument("--mysql-host")
    p.add_argument("--mysql-port", type=int, default=3306)
    p.add_argument("--mysql-user", help="user MySQL (hak CREATE/ALTER) yang dipakai untuk setup dan server.py")
    p.add_argument("--mysql-db", default=server.DEFAULT_DB_NAME)
    p.add_argument("--interval", type=float, default=60, help="interval simpan ke MySQL (detik)")
    p.add_argument("--admin-password", help="set password akun admin (min. 6 karakter)")
    p.add_argument("--user-password", help="set password akun user (min. 6 karakter)")
    args = p.parse_args()

    server.init_db()
    print(f"[OK] SQLite siap: {server.DB_PATH}")

    for username, password, role in (("admin", args.admin_password, "admin"),
                                     ("user", args.user_password, "user")):
        if not password:
            continue
        if len(password) < 6:
            print(f"[ERROR] Password {username} minimal 6 karakter.")
            return 1
        server.upsert_user(username, password, role)
        print(f"[OK] Password akun '{username}' diperbarui.")

    if not args.mysql_host:
        print("MySQL dilewati (tidak ada --mysql-host). Jalankan ulang dengan --mysql-host untuk mengaturnya.")
        return 0
    if not args.mysql_user:
        print("[ERROR] --mysql-user (admin) wajib diisi bersama --mysql-host.")
        return 1

    admin_pw = ask_password("MYSQL_PASSWORD", f"Password ADMIN MySQL {args.mysql_user}@{args.mysql_host}: ")
    admin, err = server.validate_conn({
        "host": args.mysql_host, "port": args.mysql_port,
        "db_user": args.mysql_user, "db_password": admin_pw,
        "db_name": args.mysql_db, "store_interval_s": args.interval,
    })
    if err:
        print(f"[ERROR] {err}")
        return 1

    try:
        server.ensure_schema(admin)
        print(f"[OK] Database `{admin['db_name']}` dan tabel power_readings siap di {admin['host']}:{admin['port']}")
        runtime = dict(admin)
    except mysql.connector.Error as exc:
        print(f"[ERROR] Gagal menyiapkan MySQL: {exc}")
        return 1

    server.save_db_config(runtime)
    print(f"[OK] Koneksi {runtime['db_user']}@{runtime['host']}:{runtime['port']} tersimpan di {server.DB_CONFIG_PATH.name}. "
          "Restart server bila sedang berjalan.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
