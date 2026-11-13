-- OPSIONAL - retensi otomatis power_readings. Tidak dijalankan oleh SETUP.bat.
-- Jalankan manual oleh DBA setelah client menentukan lama simpan data (contoh di bawah: 24 bulan).
-- Butuh hak EVENT dan event_scheduler aktif (SET GLOBAL event_scheduler = ON; atau di my.ini: event_scheduler=ON).
USE `ois_sosro_pw`;

DROP EVENT IF EXISTS ev_purge_power_readings;
CREATE EVENT ev_purge_power_readings
  ON SCHEDULE EVERY 1 DAY STARTS (CURRENT_DATE + INTERVAL 1 DAY + INTERVAL 2 HOUR)
  DO DELETE FROM power_readings WHERE recorded_at < NOW() - INTERVAL 24 MONTH LIMIT 100000;
-- LIMIT menjaga tiap hapus tetap ringan; sisa dihapus pada hari berikutnya.
