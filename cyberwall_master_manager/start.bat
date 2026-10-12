@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
pushd "%~dp0"
if errorlevel 1 goto folder_error

if not exist "cyberwall_master_manager.py" (
    echo ОШИБКА: рядом со start.bat нет cyberwall_master_manager.py.
    goto failed
)
if not exist "requirements.txt" (
    echo ОШИБКА: рядом со start.bat нет requirements.txt.
    goto failed
)

call :find_python
if defined PYTHON_EXE goto python_ready

echo Python 3.11 или новее не найден.
echo Устанавливаем Python 3.13 для текущего пользователя через winget...
winget --version >nul 2>&1
if errorlevel 1 (
    echo ОШИБКА: winget недоступен.
    echo Установи или обнови «Установщик приложений» в Microsoft Store.
    echo Либо установи Python 3.13 с https://www.python.org/downloads/windows/
    echo После установки запусти start.bat снова.
    goto failed
)

winget install --id Python.Python.3.13 --exact --source winget --scope user --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
set "INSTALL_RESULT=%ERRORLEVEL%"

rem Повторно проверяем даже при ненулевом коде установщика.
rem Прямые пути работают без обновления PATH текущего окна.
call :find_python
if defined PYTHON_EXE goto python_ready

echo ОШИБКА: после установки подходящий Python не найден.
echo Код завершения winget: %INSTALL_RESULT%
echo Проверь сообщения выше и подключение к интернету.
echo Если установка завершена, закрой окно и запусти start.bat снова.
goto failed

:python_ready
echo.
echo Используем Python:
"%PYTHON_EXE%" %PYTHON_ARGS% --version

"%PYTHON_EXE%" %PYTHON_ARGS% -m pip --version >nul 2>&1
if not errorlevel 1 goto dependencies

echo Устанавливаем pip...
"%PYTHON_EXE%" %PYTHON_ARGS% -m ensurepip --upgrade
if errorlevel 1 (
    echo ОШИБКА: не удалось установить pip. Проверь установку Python.
    goto failed
)

:dependencies
echo.
echo Проверяем и устанавливаем зависимости...
"%PYTHON_EXE%" %PYTHON_ARGS% -m pip --disable-pip-version-check install --no-input -r "requirements.txt"
if errorlevel 1 (
    echo.
    echo ОШИБКА: зависимости не установлены. Программа не запущена.
    echo Проверь подключение к интернету и сообщения выше.
    goto failed
)

echo.
"%PYTHON_EXE%" %PYTHON_ARGS% "cyberwall_master_manager.py"
set "APP_RESULT=%ERRORLEVEL%"
echo.
if not "%APP_RESULT%"=="0" echo Программа завершилась с кодом ошибки %APP_RESULT%.
pause
popd
exit /b %APP_RESULT%

:failed
echo.
pause
popd
exit /b 1

:folder_error
echo ОШИБКА: не удалось открыть папку программы.
pause
exit /b 1

:find_python
set "PYTHON_EXE="
set "PYTHON_ARGS="
call :try_python "py" "-3"
if defined PYTHON_EXE exit /b 0

rem Проверяем стандартные места установки, включая только что установленный Python.
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*" "%ProgramFiles%\Python3*") do call :try_python "%%~fD\python.exe" ""
if defined PYTHON_EXE exit /b 0

rem Не запускаем заглушку Microsoft Store из WindowsApps.
for /f "delims=" %%P in ('where python.exe 2^>nul ^| findstr /v /i /c:"WindowsApps"') do call :try_python "%%P" ""
exit /b 0

:try_python
if defined PYTHON_EXE exit /b 0
"%~1" %~2 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not "%ERRORLEVEL%"=="0" exit /b 0
set "PYTHON_EXE=%~1"
set "PYTHON_ARGS=%~2"
exit /b 0
