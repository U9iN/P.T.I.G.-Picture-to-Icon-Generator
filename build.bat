@echo off
rem ------------------------------------------------------------------------
rem  P.T.I.G. - Picture-to-Icon Generator: автономный сборщик PyInstaller.
rem    build.bat           обычная сборка в папку (dist\P.T.I.G. - Picture-to-Icon Generator)
rem    build.bat onefile   сборка в один автономный .exe
rem    build.bat debug     сборка с открытой консолью (для отладки ошибок)
rem  Python берется из .venv / ..\.venv или из системы; можно задать через IM_PYTHON или PG_PYTHON
rem ------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "APP_NAME=P.T.I.G. - Picture-to-Icon Generator"

set "PYEXE="
set "PYARGS="
if defined IM_PYTHON set "PYEXE=%IM_PYTHON%"
if not defined PYEXE if defined PG_PYTHON set "PYEXE=%PG_PYTHON%"
if not defined PYEXE if exist ".venv\Scripts\python.exe" set "PYEXE=.venv\Scripts\python.exe"
if not defined PYEXE if exist "..\.venv\Scripts\python.exe" set "PYEXE=..\.venv\Scripts\python.exe"
if not defined PYEXE if exist "..\Scripts\python.exe" set "PYEXE=..\Scripts\python.exe"
if not defined PYEXE (
    where py >nul 2>nul
    if not errorlevel 1 (
        set "PYEXE=py"
        set "PYARGS=-3"
    )
)
if not defined PYEXE (
    where python >nul 2>nul
    if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE (
    echo Python not found. Install Python 3 or set IM_PYTHON to the full path of python.exe
    pause
    exit /b 1
)

echo Using Python: %PYEXE% %PYARGS%

rem Проверка наличия обязательных пакетов (Pillow, pywebview, PyInstaller)
"%PYEXE%" %PYARGS% -c "import PIL, webview" >nul 2>nul
if errorlevel 1 (
    echo Installing dependencies ^(pillow, pywebview^)...
    "%PYEXE%" %PYARGS% -m pip install pillow pywebview
)

"%PYEXE%" %PYARGS% -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo PyInstaller not found. Installing...
    "%PYEXE%" %PYARGS% -m pip install pyinstaller
)

rem Разбор режимов запуска (по умолчанию: папка без консоли; можно комбинировать onefile debug)
set "MODE_FLAG=--onedir"
set "CONSOLE_FLAG=--windowed"

for %%A in (%*) do (
    if /i "%%~A"=="onefile" set "MODE_FLAG=--onefile"
    if /i "%%~A"=="debug"   set "CONSOLE_FLAG=--console"
)

rem Автоподхват иконки: если icon.ico нет в папке, генерируем его самим движком make_icon.py
set "ICON_FLAG="
if not exist "icon.ico" if exist "make_icon.py" (
    echo Generating default icon.ico via make_icon.py...
    "%PYEXE%" %PYARGS% -c "from make_icon import IconSettings, build_frames, make_demo_image, write_ico; f, _ = build_frames(make_demo_image(512), IconSettings()); write_ico(f, 'icon.ico')" >nul 2>nul
)
if exist "icon.ico" (
    set "ICON_FLAG=--icon icon.ico"
    echo Using icon: icon.ico
) else (
    echo Note: icon.ico not found, building with default icon.
)

echo.
echo Building "%APP_NAME%" (%MODE_FLAG% %CONSOLE_FLAG%)...

"%PYEXE%" %PYARGS% -m PyInstaller --noconfirm --clean ^
    %MODE_FLAG% %CONSOLE_FLAG% %ICON_FLAG% ^
    --name "%APP_NAME%" ^
    --add-data "icon_gui.html;." ^
    --hidden-import make_icon ^
    icon_launcher.py

set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
    echo BUILD FAILED, code %RC%.
    pause
    exit /b %RC%
)

rem Страховка для режима --onedir: копируем HTML рядом с .exe
if "%MODE_FLAG%"=="--onedir" (
    if exist "icon_gui.html" (
        copy /y "icon_gui.html" "dist\%APP_NAME%\icon_gui.html" >nul
    )
    echo BUILD SUCCESSFUL! Result is in "dist\%APP_NAME%".
) else (
    echo BUILD SUCCESSFUL! Result is "dist\%APP_NAME%.exe".
)
pause
exit /b 0
