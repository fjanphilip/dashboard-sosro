@echo off
setlocal
cd /d "%~dp0"
title SOSRO Dashboard
if not exist app\meters.db (
    echo Belum di-setup. Jalankan SETUP.bat dulu.
    pause
    exit /b 1
)
if not exist port.txt echo 3389>port.txt
set /p PORT=<port.txt
echo Menjalankan dashboard di port %PORT% ... (tutup jendela ini untuk menghentikan)
start "" /min cmd /c "timeout /t 4 /nobreak >nul & start http://127.0.0.1:%PORT%"
python\python.exe app\server.py --host 0.0.0.0 --port %PORT%
echo.
echo Server berhenti. Jika ada error "Address already in use"/"Permission": port %PORT% dipakai program lain
echo (port 3389 = Remote Desktop). Ubah angka di port.txt lalu jalankan START.bat lagi.
pause
