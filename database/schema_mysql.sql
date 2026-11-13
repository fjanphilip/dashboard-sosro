-- SOSRO Dashboard - skema MySQL/MariaDB (database pabrik / client)
-- Sumber: server.py (READINGS_DDL + ensure_schema). Hanya SATU tabel yang dipakai dashboard.
-- SETUP.bat membuat ini otomatis; file ini untuk DBA yang ingin membuatnya manual
-- atau membuat user dengan hak minimal (lihat bagian paling bawah).

CREATE DATABASE IF NOT EXISTS `ois_sosro_pw` CHARACTER SET utf8mb4;
USE `ois_sosro_pw`;

CREATE TABLE IF NOT EXISTS power_readings (
    id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    recorded_at DATETIME(3)  NOT NULL COMMENT 'waktu sampel (jam mini PC)',
    device_id   INT          NOT NULL COMMENT 'id perangkat di SQLite dashboard (devices.id)',
    device_name VARCHAR(100) NOT NULL,
    host        VARCHAR(255) NOT NULL COMMENT 'IP/host gateway Modbus',
    unit_id     SMALLINT     NOT NULL COMMENT 'ID slave Modbus',
    -- Tegangan L-N (V)
    v1 FLOAT NULL, v2 FLOAT NULL, v3 FLOAT NULL,
    -- Arus (A)
    i1 FLOAT NULL, i2 FLOAT NULL, i3 FLOAT NULL,
    -- Daya aktif per fasa (W)
    p1 FLOAT NULL, p2 FLOAT NULL, p3 FLOAT NULL,
    -- Daya semu per fasa (VA)
    s1 FLOAT NULL, s2 FLOAT NULL, s3 FLOAT NULL,
    p_total FLOAT NULL COMMENT 'W',
    s_total FLOAT NULL COMMENT 'VA',
    freq    FLOAT NULL COMMENT 'Hz',
    site    VARCHAR(100) NULL COMMENT 'lokasi; NULL pada baris lama sebelum kolom ini ada',
    UNIQUE KEY uq_sample (device_id, recorded_at),   -- INSERT IGNORE: kirim ulang batch tidak menggandakan data
    INDEX idx_device_time (device_id, recorded_at),
    INDEX idx_time (recorded_at),
    INDEX idx_site_time (site, recorded_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Catatan: meter OFFLINE disimpan sebagai NULL (bukan 0) agar rata-rata/total kWh tidak rusak.
-- Tidak ada purge otomatis di MySQL; atur retensi sendiri bila perlu, contoh:
--   DELETE FROM power_readings WHERE recorded_at < NOW() - INTERVAL 2 YEAR;

-- User dashboard: aplikasi menjalankan CREATE DATABASE/CREATE TABLE/ALTER (dan, hanya untuk migrasi
-- tabel lama, DELETE + tabel power_readings_dup_backup) setiap koneksi diaktifkan. Paling mudah:
--   CREATE USER 'sosro'@'%' IDENTIFIED BY '<password-kuat>';
--   GRANT ALL PRIVILEGES ON `ois_sosro_pw`.* TO 'sosro'@'%';
-- (CREATE DATABASE hanya berhasil bila DB belum ada; jika DB sudah dibuat DBA, hak di atas cukup.)
