@echo off
setlocal EnableDelayedExpansion

REM ============================================================================
REM  build-and-run.bat
REM  Builds Jupiter-Engine + SevensCore-Predator, deploys build artifacts,
REM  and (after confirmation) launches the game.
REM  Run this script from the project root directory.
REM ============================================================================

REM -------- Configuration -----------------------------------------------------
set "ENGINE_SLN=Jupiter-Engine.slnx"
set "GAME_SLN=SevensPredator\SevensCore-Predator.slnx"
set "CONFIG=Release"
set "PLATFORM=win32"

set "SRC_DIR=SevensPredator\bin\Release"
set "DST_DIR=bin\Release\SevensPredator"
set "LAUNCH_DIR=bin\Release"
set "GAME_EXE=LithTech.exe"
set "GAME_ARGS=-rez Engine.REZ -rez .\SevensPredator"

REM Extensions to copy and to clean (no leading dot, space-separated)
set "EXTENSIONS=dll lib exp lto pdb"

REM -------- Sanity check: are we in the project root? -------------------------
if not exist "%ENGINE_SLN%" (
    echo [ERROR] "%ENGINE_SLN%" not found in current directory.
    echo         Run this script from the project root.
    goto :error
)
if not exist "%GAME_SLN%" (
    echo [ERROR] "%GAME_SLN%" not found.
    echo         Run this script from the project root.
    goto :error
)

REM -------- Locate MSBuild via vswhere ----------------------------------------
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" (
    echo [ERROR] vswhere.exe not found. Is Visual Studio 2022 installed?
    goto :error
)

set "MSBUILD="
for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -requires Microsoft.Component.MSBuild -find MSBuild\**\Bin\MSBuild.exe`) do (
    set "MSBUILD=%%i"
)

if not defined MSBUILD (
    echo [ERROR] MSBuild.exe not found via vswhere.
    goto :error
)

echo Using MSBuild: %MSBUILD%
echo.

REM -------- Clean previously-deployed files (only the listed extensions) ------
echo === Cleaning previously deployed files in "%DST_DIR%" ===
if exist "%DST_DIR%" (
    for %%E in (%EXTENSIONS%) do (
        if exist "%DST_DIR%\*.%%E" (
            del /Q "%DST_DIR%\*.%%E" 2>nul
            echo   Removed *.%%E
        )
    )
) else (
    mkdir "%DST_DIR%"
)
echo.

REM -------- Build Engine ------------------------------------------------------
echo === [1/2] Building %ENGINE_SLN% ===
"%MSBUILD%" "%ENGINE_SLN%" /p:Configuration=%CONFIG% /p:Platform=%PLATFORM% /m /nologo /v:minimal
if errorlevel 1 (
    echo.
    echo [ERROR] Engine build FAILED.
    goto :error
)
echo Engine build succeeded.
echo.

REM -------- Build Game --------------------------------------------------------
echo === [2/2] Building %GAME_SLN% ===
"%MSBUILD%" "%GAME_SLN%" /p:Configuration=%CONFIG% /p:Platform=%PLATFORM% /m /nologo /v:minimal
if errorlevel 1 (
    echo.
    echo [ERROR] Game build FAILED.
    goto :error
)
echo Game build succeeded.
echo.

REM -------- Deploy build artifacts --------------------------------------------
echo === Deploying artifacts from "%SRC_DIR%" to "%DST_DIR%" ===
if not exist "%SRC_DIR%" (
    echo [ERROR] Source directory "%SRC_DIR%" does not exist.
    goto :error
)
if not exist "%DST_DIR%" mkdir "%DST_DIR%"

for %%E in (%EXTENSIONS%) do (
    if exist "%SRC_DIR%\*.%%E" (
        copy /Y "%SRC_DIR%\*.%%E" "%DST_DIR%\" >nul
        if errorlevel 1 (
            echo [ERROR] Failed copying *.%%E
            goto :error
        ) else (
            echo   Copied *.%%E
        )
    )
)
echo Deployment complete.
echo.

REM -------- Prompt before launching -------------------------------------------
echo === Build and deploy successful ===
choice /C YN /N /M "Launch the game now? [Y/N] "
if errorlevel 2 goto :done
if errorlevel 1 goto :launch

:launch
if not exist "%LAUNCH_DIR%\%GAME_EXE%" (
    echo [ERROR] "%LAUNCH_DIR%\%GAME_EXE%" not found.
    goto :error
)
echo Launching %GAME_EXE% ...
pushd "%LAUNCH_DIR%"
start "" "%GAME_EXE%" %GAME_ARGS%
popd
goto :done

REM -------- Exit handlers -----------------------------------------------------
:error
echo.
echo *** Script aborted due to an error ***
endlocal
exit /b 1

:done
echo.
echo Done.
endlocal
exit /b 0
