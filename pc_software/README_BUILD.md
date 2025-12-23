# Building Card Detection System Executable

## Method 1: Using the Build Script (Easiest)

1. Open Command Prompt in the `pc_software` folder
2. Run: `build_exe.bat`
3. The executable will be created in the `dist` folder

## Method 2: Manual PyInstaller Build

### Install PyInstaller:
```bash
pip install pyinstaller
```

### Build executable:
```bash
pyinstaller --name="CardDetectionSystem" --onefile --windowed main.py
```

### Options explained:
- `--onefile`: Creates a single .exe file
- `--windowed`: No console window (GUI only)
- `--name`: Sets the executable name
- `--icon=myicon.ico`: Add custom icon (optional)

## Method 3: Auto-py-to-exe (GUI Tool)

### Install:
```bash
pip install auto-py-to-exe
```

### Run:
```bash
auto-py-to-exe
```

Then use the GUI to:
1. Select `main.py`
2. Choose "One File"
3. Choose "Window Based (hide console)"
4. Click "Convert"

## Running the Executable

After building, the executable will be in the `dist` folder:
```
dist/CardDetectionSystem.exe
```

**Note**: The first run may be slow as PyInstaller unpacks files. Subsequent runs will be faster.

## Troubleshooting

### Missing config.json
If the executable can't find config.json, copy it to the same folder as the .exe

### Antivirus False Positives
Some antivirus software may flag PyInstaller executables. Add an exception if needed.

### Serial Port Issues
Make sure you have the correct COM port drivers installed.
