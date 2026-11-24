# -*- coding: utf-8 -*-
"""
Discord Wallpaper Faker インストーラー (Windows専用、管理者で実行される)

やること:
- DiscordWallpaperFaker.exe を、インストーラーと同じフォルダ (ZIP を展開したフォルダ) に展開する
- 利用規約 (terms.txt) も同じフォルダに置く。本体は同じフォルダの data の中で設定とログを持つ
- デスクトップにショートカットは作らない。代わりにスタートメニューへ登録して、
  Windows の検索バーで「discordwallpaperfaker」と調べれば本体を直接開けるようにする
- 自動起動は作らない (アプリの設定で自分で決める)
- アンインストールではタスク・自動起動・ファイルを消す
- 旧名 (Fake Wallpaper) のタスクが残っていれば一緒に片付ける

Discord 本体やレジストリの Discord 設定は一切変更しない。
"""

import os
import shutil
import subprocess
import sys
import time
import tkinter as tk
from tkinter import messagebox, ttk

# ポータブル方式: インストール先はインストーラー自身が置かれているフォルダ
# (＝ ZIP を展開したフォルダ)。本体・設定・ログは全部その中で完結する。
HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
APP_DIR = HERE
APP_EXE = os.path.join(APP_DIR, "DiscordWallpaperFaker.exe")
OLD_APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "DiscordWallpaperFaker")
TASK_NAME = "DiscordWallpaperFaker"
OLD_TASK_NAME = "FakeWallpaper"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_PATHS_KEY = r"Software\Microsoft\Windows\CurrentVersion\App Paths\DiscordWallpaperFaker.exe"
PROGRAMS_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                            "Microsoft", "Windows", "Start Menu", "Programs")
START_LINK = os.path.join(PROGRAMS_DIR, "DiscordWallpaperFaker.lnk")


def find_bundled(name):
    """同梱ファイルの場所を探す (インストーラーと同じフォルダ / 埋め込み済み)。"""
    beside = os.path.join(HERE, name)
    if os.path.isfile(beside):
        return beside
    bundled = os.path.join(getattr(sys, "_MEIPASS", ""), name)
    if bundled and os.path.isfile(bundled):
        return bundled
    return beside


SOURCE_EXE = find_bundled("DiscordWallpaperFaker.exe")
SOURCE_TERMS = find_bundled("terms.txt")


def run_cmd(args):
    return subprocess.run(args, capture_output=True, text=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def make_start_menu_shortcut():
    """Windows の検索バーで本体が出てくるように、スタートメニューにだけ登録する。
    デスクトップには作らない。"""
    try:
        os.makedirs(PROGRAMS_DIR, exist_ok=True)
        ps = (
            "$s = New-Object -ComObject WScript.Shell; "
            "$lnk = $s.CreateShortcut('" + START_LINK + "'); "
            "$lnk.TargetPath = '" + APP_EXE + "'; "
            "$lnk.WorkingDirectory = '" + APP_DIR + "'; "
            "$lnk.IconLocation = '" + APP_EXE + ",0'; "
            "$lnk.Description = 'Discord Wallpaper Faker'; "
            "$lnk.Save()"
        )
        run_cmd(["powershell", "-NoProfile", "-Command", ps])
    except Exception:
        pass
    # Win+R や検索の補助として App Paths にも登録する
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, APP_PATHS_KEY) as k:
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ, APP_EXE)
            winreg.SetValueEx(k, "Path", 0, winreg.REG_SZ, APP_DIR)
    except Exception:
        pass


def remove_start_menu_shortcut():
    try:
        if os.path.isfile(START_LINK):
            os.remove(START_LINK)
    except Exception:
        pass
    try:
        import winreg
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, APP_PATHS_KEY)
    except Exception:
        pass


def remove_run_key():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0,
                            winreg.KEY_SET_VALUE) as k:
            for name in ("DiscordWallpaperFaker", "FakeWallpaper"):
                try:
                    winreg.DeleteValue(k, name)
                except FileNotFoundError:
                    pass
    except Exception:
        pass


def do_install(ui):
    if not os.path.isfile(SOURCE_EXE):
        messagebox.showerror("Discord Wallpaper Faker",
                             "本体 (DiscordWallpaperFaker.exe) が見つかりません。build.bat で作り直してください。")
        return
    ui["install_btn"].config(state="disabled")
    ui["uninstall_btn"].config(state="disabled")

    def step(pct, text):
        ui["bar"]["value"] = pct
        ui["status"].set(text)
        ui["root"].update()
        time.sleep(0.3)

    try:
        step(5, "準備中...")
        os.makedirs(APP_DIR, exist_ok=True)
        run_cmd(["taskkill", "/IM", "DiscordWallpaperFaker.exe", "/F"])
        run_cmd(["taskkill", "/IM", "FakeWallpaper.exe", "/F"])
        run_cmd(["schtasks", "/Delete", "/TN", OLD_TASK_NAME, "/F"])
        step(25, "本体を展開しています...")
        shutil.copy2(SOURCE_EXE, APP_EXE)
        step(60, "本体を展開しました")
        step(75, "検索バーで探せるように登録しています...")
        make_start_menu_shortcut()
        step(88, "利用規約を置いています...")
        if os.path.isfile(SOURCE_TERMS):
            try:
                shutil.copy2(SOURCE_TERMS, os.path.join(APP_DIR, "terms.txt"))
            except Exception:
                pass
        try:
            if os.path.isdir(OLD_APP_DIR):
                shutil.rmtree(OLD_APP_DIR)
        except Exception:
            pass
        step(100, "インストール完了。今すぐ起動します。")
        try:
            os.startfile(APP_EXE)
        except Exception:
            pass
        messagebox.showinfo("Discord Wallpaper Faker",
                            "インストール完了。" + chr(10)
                            + "Windows の検索バーで discordwallpaperfaker と調べると、本体を直接開けます。" + chr(10)
                            + "このフォルダの中でソフトが動きます。フォルダごと移動する場合は、移動先でインストールし直してください。" + chr(10)
                            + "まず設定でフェイク壁紙のパスを設定してください。")
    finally:
        ui["install_btn"].config(state="normal")
        ui["uninstall_btn"].config(state="normal")


def do_uninstall(ui):
    ui["install_btn"].config(state="disabled")
    ui["uninstall_btn"].config(state="disabled")

    def step(pct, text):
        ui["bar"]["value"] = pct
        ui["status"].set(text)
        ui["root"].update()
        time.sleep(0.3)

    try:
        step(15, "本体を終了しています...")
        run_cmd(["taskkill", "/IM", "DiscordWallpaperFaker.exe", "/F"])
        run_cmd(["taskkill", "/IM", "FakeWallpaper.exe", "/F"])
        step(40, "自動起動と検索の登録を消しています...")
        run_cmd(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
        run_cmd(["schtasks", "/Delete", "/TN", OLD_TASK_NAME, "/F"])
        remove_run_key()
        remove_start_menu_shortcut()
        step(70, "ファイルを消しています...")
        link = os.path.join(os.path.expanduser("~"), "Desktop", "Fake Wallpaper.lnk")
        try:
            if os.path.isfile(link):
                os.remove(link)
        except Exception:
            pass
        for target in (APP_EXE, os.path.join(APP_DIR, "terms.txt")):
            try:
                if os.path.isfile(target):
                    os.remove(target)
            except Exception:
                pass
        try:
            shutil.rmtree(os.path.join(APP_DIR, "data"))
        except Exception:
            pass
        try:
            if os.path.isdir(OLD_APP_DIR):
                shutil.rmtree(OLD_APP_DIR)
        except Exception:
            pass
        step(100, "アンインストールしました。")
        messagebox.showinfo("Discord Wallpaper Faker", "アンインストールしました。")
    finally:
        ui["install_btn"].config(state="normal")
        ui["uninstall_btn"].config(state="normal")


def main():
    root = tk.Tk()
    root.title("Discord Wallpaper Faker インストーラー")
    root.geometry("480x300")
    root.resizable(False, False)
    status = tk.StringVar(value="デスクトップにショートカットは作りません。検索バーで探せるよう、スタートメニューにだけ登録します。")
    tk.Label(root, text="Discord Wallpaper Faker", font=("", 14, "bold")).pack(pady=(12, 2))
    tk.Label(root, text="Discordの全画面共有を検知して、壁紙を自動で隠す常駐アプリ").pack()
    tk.Label(root, textvariable=status, wraplength=440).pack(pady=10)
    bar = ttk.Progressbar(root, mode="determinate", maximum=100, length=400)
    bar.pack(pady=(0, 10))
    btns = tk.Frame(root)
    btns.pack()
    ui = {"root": root, "status": status, "bar": bar}
    install_btn = tk.Button(btns, text="インストール", width=16,
                            command=lambda: do_install(ui))
    install_btn.pack(side="left", padx=6)
    uninstall_btn = tk.Button(btns, text="アンインストール", width=16,
                              command=lambda: do_uninstall(ui))
    uninstall_btn.pack(side="left", padx=6)
    ui["install_btn"] = install_btn
    ui["uninstall_btn"] = uninstall_btn
    tk.Label(root, text="Discord本体は変更しません。読むのはログ、変えるのは壁紙だけです。",
             fg="#666666").pack(pady=(10, 0))
    tk.Label(root, text="利用規約: 改造・販売・二次配布は禁止です。",
             fg="#666666").pack()
    tk.Label(root, text="制作者: TRS00131  https://github.com/trs00131",
             fg="#1a5fb4").pack(pady=(4, 0))
    root.mainloop()


if __name__ == "__main__":
    main()
