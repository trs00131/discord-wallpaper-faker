@echo off
chcp 65001 >nul
title Discord Wallpaper Faker build
cd /d %~dp0
echo Discord Wallpaper Faker を作ります。
echo.
python --version
if errorlevel 1 (
  echo.
  echo Python が見つかりませんでした。
  echo Python を入れてから、もう一度 build.bat を押してください。
  pause
  exit /b 1
)
echo.
echo [1/5] 必要な部品を入れます (pyinstaller)...
python -m pip install --upgrade pyinstaller
if errorlevel 1 (
  echo.
  echo 部品のインストールで止まりました。上の表示を妖精に送ってください。
  pause
  exit /b 1
)
echo.
echo [2/5] 本体 DiscordWallpaperFaker.exe を作業フォルダに作ります...
if exist build_work rmdir /s /q build_work
python -m PyInstaller --onefile --windowed --name DiscordWallpaperFaker --icon "%CD%\assets\app.ico" --add-data "%CD%\assets\tray_idle.ico;." --add-data "%CD%\assets\tray_active.ico;." --add-data "%CD%\assets\app.ico;." --distpath build_work --workpath build_tmp --specpath build_tmp discord_wallpaper_faker.py
if errorlevel 1 (
  echo.
  echo 本体の exe 化で止まりました。上の表示を妖精に送ってください。
  pause
  exit /b 1
)
if not exist build_work\DiscordWallpaperFaker.exe (
  echo.
  echo 本体 DiscordWallpaperFaker.exe が作られていませんでした。上の表示を妖精に送ってください。
  pause
  exit /b 1
)
echo.
echo [3/5] インストーラー discord-wallpaper-faker-installer.exe を作ります (本体と利用規約を埋め込みます)...
if exist dist rmdir /s /q dist
python -m PyInstaller --onefile --windowed --uac-admin --name discord-wallpaper-faker-installer --icon "%CD%\assets\app.ico" --add-data "%CD%\build_work\DiscordWallpaperFaker.exe;." --add-data "%CD%\terms.txt;." --workpath build_tmp --specpath build_tmp installer.py
if errorlevel 1 (
  echo.
  echo インストーラーの exe 化で止まりました。上の表示を妖精に送ってください。
  pause
  exit /b 1
)
echo.
echo [4/5] dist に README と利用規約を入れます...
copy /y README.md dist\README.md >nul
copy /y terms.txt dist\terms.txt >nul
echo.
echo [5/5] 作業ファイルを片付けます...
rmdir /s /q build_work
rmdir /s /q build_tmp
echo.
echo 完成！
echo dist フォルダの中身が配布セットです:
echo   discord-wallpaper-faker-installer.exe  (配るのはこれだけ。本体は中に埋め込み済み)
echo   README.md / terms.txt
pause
exit /b 0
