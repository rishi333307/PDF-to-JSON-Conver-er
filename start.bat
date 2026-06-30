@echo off
REM ===========================================================
REM  One-click starter for Windows.
REM  Just double-click this file every time you want to run the app.
REM  First run: creates a virtual environment and installs everything.
REM  Every run after that: just starts the server (fast).
REM ===========================================================

cd %~dp0backend

IF NOT EXIST venv (
    echo [Setup] First time setup - creating virtual environment...
    python -m venv venv

    echo [Setup] Installing required libraries... this may take a minute.
    call venv\Scripts\activate
    pip install -r requirements.txt
) ELSE (
    call venv\Scripts\activate
)

echo.
echo ===========================================================
echo   Backend server starting at http://127.0.0.1:5000
echo   Opening the upload page in your browser...
echo   (Keep this window open while you use the app)
echo ===========================================================
echo.

REM Open the frontend through the Flask server itself (not as a local
REM file) -- app.py now serves the upload page directly at "/", and the
REM page's JavaScript expects to be loaded this way so it can correctly
REM detect its own API address. Opening index.html directly as a file
REM would break the upload button.
start "" cmd /c "timeout /t 2 >nul && start http://127.0.0.1:5000"

REM Start the Flask backend (this keeps running until you close this window)
python app.py

pause
