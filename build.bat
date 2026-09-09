@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo === Song Downloader build script ===

if not exist "ffmpeg.exe" (
    echo.
    echo ERROR: ffmpeg.exe not found in this folder.
    echo Download it from https://www.gyan.dev/ffmpeg/builds/
    echo then copy ffmpeg.exe from its bin folder into this folder.
    echo.
    pause
    exit /b 1
)

echo Installing Python dependencies...
pip install -r requirements.txt

echo Building SongDownloader.exe ...
pyinstaller --clean --onefile --windowed --name SongDownloader --add-binary "ffmpeg.exe;." app.py

echo.
echo Done. Your exe is at dist\SongDownloader.exe
echo (ffmpeg is bundled inside it - the exe is portable on its own)
pause