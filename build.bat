@echo off
rem Build a standalone dist\PCCleaner.exe (needs: python -m pip install -r requirements.txt pyinstaller)
cd /d "%~dp0"
python -m unittest discover -s tests -t . || (echo Tests failed - not building. & exit /b 1)
python -m PyInstaller --noconfirm --clean --onefile --windowed --name PCCleaner ^
  --icon assets\pccleaner.ico --add-data "assets\pccleaner.ico;assets" --add-data "assets\pccleaner.png;assets" ^
  --collect-all ttkbootstrap --version-file tools\version_info.txt run_pccleaner.py
