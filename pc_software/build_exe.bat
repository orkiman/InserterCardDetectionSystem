@echo off
REM Build script for Card Detection System
echo Building executable...

REM Install PyInstaller if not already installed
pip install pyinstaller

REM Build the executable
pyinstaller --name="CardDetectionSystem" ^
    --onefile ^
    --windowed ^
    --icon=NONE ^
    --add-data "config.json;." ^
    main.py

echo.
echo Build complete! Executable is in the 'dist' folder.
pause
