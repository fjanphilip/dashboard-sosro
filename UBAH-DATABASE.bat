@echo off
setlocal
cd /d "%~dp0"
title SOSRO Dashboard - Ubah Koneksi Database
echo ==============================================
echo  UBAH KONEKSI DATABASE SOSRO Dashboard
echo ==============================================
echo Koneksi lama di app\database.json akan ditimpa. Tutup START.bat dulu.
echo User MySQL harus punya hak CREATE/ALTER.
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
if errorlevel 1 (
    echo.
    echo Gagal. Koneksi tidak diubah. Periksa pesan di atas.
    pause
    exit /b 1
)
echo.
echo Selesai. Jalankan START.bat untuk memakai koneksi baru.
pause
