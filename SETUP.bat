@echo off
setlocal
cd /d "%~dp0"
title SOSRO Dashboard - Setup
echo ==============================================
echo  SETUP SOSRO Dashboard (jalankan sekali)
echo ==============================================
echo Setup akan membuat database dan tabel di MySQL server Anda.
echo User MySQL harus punya hak CREATE/ALTER (user ini juga dipakai saat dashboard berjalan).
echo.
set /p MYHOST=Alamat MySQL (contoh 127.0.0.1 atau IP server) : 
set /p MYPORT=Port MySQL [3306] : 
if "%MYPORT%"=="" set MYPORT=3306
set /p MYUSER=User MySQL : 
set /p MYDB=Nama database [ois_sosro_pw] : 
if "%MYDB%"=="" set MYDB=ois_sosro_pw
echo.
echo Password MySQL akan ditanya berikutnya (tidak tampil di layar).
echo.
python\python.exe app\setup_db.py --mysql-host %MYHOST% --mysql-port %MYPORT% --mysql-user %MYUSER% --mysql-db %MYDB%
if errorlevel 1 goto :err

if not exist port.txt echo 3389>port.txt
set /p PORT=<port.txt
netsh advfirewall firewall add rule name="SOSRO Dashboard" dir=in action=allow protocol=TCP localport=%PORT% >nul 2>&1
if errorlevel 1 (
    echo [INFO] Rule firewall port %PORT% tidak dibuat ^(butuh Run as administrator^). Abaikan bila firewall sudah diatur.
) else (
    echo [OK] Rule firewall port %PORT% dibuat.
)
echo.
echo Setup selesai. Jalankan START.bat. Login awal: admin / admin123 ^(segera ganti di menu Akun^).
pause
exit /b 0
:err
echo.
echo Setup GAGAL. Periksa pesan di atas lalu jalankan SETUP.bat lagi.
pause
exit /b 1
