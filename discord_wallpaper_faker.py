# -*- coding: utf-8 -*-
"""
Discord Wallpaper Faker v1 (Windows専用)

- Discord のログフォルダをイベント駆動で監視する (ReadDirectoryChangesW)
- ログに「画面全体の共有」らしい行が出たら、指定した無難な壁紙に切り替える
- 共有が終わったら元の壁紙に戻す
- Discord 本体は一切いじらない。読むのは Discord が自分で書くログファイルだけ、
  変えるのは Windows の壁紙だけ (公式API)。だからクライアント改造ではない
- 設定と記録は、exe と同じフォルダの data の中 (ポータブル方式。書き込めない場所なら LOCALAPPDATA)
- 標準ライブラリだけ。追加インストールなし
"""

import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from ctypes import wintypes

IS_WINDOWS = os.name == "nt"
BASE_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))


def _default_data_dir():
    """ポータブル方式: exe 版は exe と同じフォルダの data の中で動く
    (展開したフォルダの中で全部完結する)。書き込めない場所なら LOCALAPPDATA に退避。"""
    if getattr(sys, "frozen", False):
        d = os.path.join(BASE_DIR, "data")
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".write_test")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe)
            return d
        except Exception:
            pass
    return os.path.join(os.environ.get("LOCALAPPDATA") or BASE_DIR, "DiscordWallpaperFaker")


DATA_DIR = _default_data_dir()
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
LOG_PATH = os.path.join(DATA_DIR, "discord_wallpaper_faker.log")
DIAG_PATH = os.path.join(DATA_DIR, "discord_log_hits.txt")

SPI_SETDESKWALLPAPER = 20
SPI_GETDESKWALLPAPER = 115
SPIF_SENDCHANGE = 3

DEFAULT_CONFIG = {
    "fake_wallpaper": "",
    "enabled": True,
    "diag_log": True,
    "original_wallpaper": "",
    "original_wallpaper_manual": "",
}


def now_text():
    t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + ".%03d" % int((t % 1) * 1000)


def log(msg):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(now_text() + " " + str(msg) + chr(10))
    except Exception:
        pass


def diag(msg):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(DIAG_PATH, "a", encoding="utf-8") as f:
            f.write(now_text() + " " + str(msg) + chr(10))
    except Exception:
        pass


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log("設定の保存に失敗: " + str(e))


# ---------------- 壁紙 ----------------

def get_wallpaper():
    if not IS_WINDOWS:
        return ""
    buf = ctypes.create_unicode_buffer(1024)
    ok = ctypes.windll.user32.SystemParametersInfoW(SPI_GETDESKWALLPAPER, 1024, buf, 0)
    return buf.value if ok else ""


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp"}


def is_valid_image_path(path):
    if not path:
        return False
    path = str(path).strip().strip('"')
    return os.path.isfile(path) and os.path.splitext(path)[1].lower() in IMAGE_EXTS


def set_wallpaper(path):
    if not IS_WINDOWS:
        log("Windowsではないので壁紙は変えません: " + str(path))
        return False
    if not path or not os.path.isfile(path):
        log("壁紙ファイルがありません: " + str(path))
        return False
    ok = ctypes.windll.user32.SystemParametersInfoW(
        SPI_SETDESKWALLPAPER, 0, ctypes.create_unicode_buffer(path), SPIF_SENDCHANGE)
    log("壁紙を変更 -> " + path + " (ok=" + str(bool(ok)) + ")")
    return bool(ok)


# ---------------- ログ行の判定 ----------------
# Discord のログに共有開始がどう書かれるかは実機でしか確定しない。
# そのため v1 は広めに拾い、候補行は全部 discord_log_hits.txt に残す。
# 実機テストの記録を見て、v2 で判定を正確に絞る設計。

CANDIDATE_WORDS = [
    "stream", "screen", "capture", "video", "live", "source",
    "rtc", "media", "golive", "share", "desktop", "webrtc",
    "screencast", "track", "sender", "encoder", "outbound",
]
SCREEN_SOURCE_RE = re.compile(r"screen:\d+")
WINDOW_SOURCE_RE = re.compile(r"window:\d+")
START_HINTS = ["start", "begin", "publish", "connect", "create", "init"]
# v3: 終了判定は強い言い方だけに限定する。
# v2 は destroy / release / closed のような汎用の単語でも「終了」と判定していて、
# 共有中のログに普通に出てくる単語で壁紙が戻り、点滅の原因になっていた。
STOP_PHRASES = [
    "stream stopped", "stopped streaming", "stream ended",
    "screenshare stopped", "screenshare ended", "screen share stopped",
    "screen share ended", "golive stopped", "go live stopped",
    "sharing stopped", "share stopped",
]
START_PHRASES = [
    "video capture started", "capture started", "stream started",
    "started streaming", "screenshare started", "screen share started",
    "sharing started",
]


def is_candidate(line):
    low = line.lower()
    return any(w in low for w in CANDIDATE_WORDS)


def is_exact_stop(low):
    """実ログで確認した、共有終了の正確な行 (renderer_js.log)。
    音声通話 (default) ではなく映像共有 (stream) 側の接続の終了だけを見る。"""
    if "activestream: false" in low:
        return True
    if "stream)" in low and "destroy rtcconnection" in low:
        return True
    if "[connection(stream)]" in low and "disconnected" in low:
        return True
    return False


def classify_line(line):
    """戻り値: screen_start / window_start / stream_stop_exact / stream_stop / candidate / None
    v5: 判定は実機ログの実物の行が根拠。
    開始: source id が screen-handle (全画面) か window-handle (ウィンドウ) で確定できる。
    終了: (stream) 側の接続終了の行だけを正確な終了とする。
    loopback (アプリ音声) の行は終了に使わない (v4 で誤判定した)。"""
    low = line.lower()
    if not is_candidate(low):
        return None
    if is_exact_stop(low):
        return "stream_stop_exact"
    if "screen-handle" in low:
        return "screen_start"
    if "window-handle" in low:
        return "window_start"
    if "loopback" in low:
        return "candidate"
    if any(p in low for p in STOP_PHRASES):
        return "stream_stop"
    if SCREEN_SOURCE_RE.search(low):
        return "screen_start"
    if WINDOW_SOURCE_RE.search(low):
        return "window_start"
    if "screencapturer" in low or "screen_capturer" in low or "screen capturer" in low:
        return "screen_start"
    if "windowcapturer" in low or "window_capturer" in low or "window capturer" in low:
        return "window_start"
    if "error" in low or "fail" in low:
        return "candidate"
    if any(p in low for p in START_PHRASES):
        return "screen_start"
    has_start = any(w in low for w in START_HINTS)
    strong = ("stream" in low or "capture" in low or "golive" in low
              or "screenshare" in low or "screen_share" in low)
    if strong and has_start and ("screen" in low or "desktop" in low or "golive" in low):
        return "screen_start"
    return "candidate"


def discord_logs_dirs():
    appdata = os.environ.get("APPDATA") or ""
    out = []
    for name in ("discord", "discordcanary", "discordptb"):
        d = os.path.join(appdata, name, "logs")
        if os.path.isdir(d):
            out.append(d)
    return out


def _kernel32():
    """64bit Windows ではハンドルを int のまま受けると切り捨てられて壊れる。
    v1 の検知が動かなかった主犯。必ず c_void_p で受ける。"""
    k = ctypes.windll.kernel32
    k.CreateFileW.restype = ctypes.c_void_p
    k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    k.ReadDirectoryChangesW.restype = wintypes.BOOL
    k.ReadDirectoryChangesW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                        wintypes.BOOL, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, ctypes.c_void_p]
    k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.Process32FirstW.restype = wintypes.BOOL
    k.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k.Process32NextW.restype = wintypes.BOOL
    k.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k.OpenProcess.restype = ctypes.c_void_p
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.GetProcessIoCounters.restype = wintypes.BOOL
    k.GetProcessIoCounters.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k.CloseHandle.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    return k


INVALID_HANDLE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def discord_pids():
    if not IS_WINDOWS:
        return []
    kernel32 = _kernel32()
    snap = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if not snap or snap == INVALID_HANDLE:
        log("プロセス一覧の取得に失敗 error=" + str(ctypes.get_last_error()))
        return []
    pids = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == "discord.exe":
                pids.append(int(entry.th32ProcessID))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return pids


def discord_running():
    return len(discord_pids()) > 0


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


def discord_io_total():
    """Discord 全プロセスの転送量合計 (bytes)。映像を送り始めると跳ね上がる。"""
    if not IS_WINDOWS:
        return None
    kernel32 = _kernel32()
    total = 0
    found = False
    for pid in discord_pids():
        h = kernel32.OpenProcess(0x1000, False, pid)
        if not h or h == INVALID_HANDLE:
            continue
        try:
            counters = IO_COUNTERS()
            if kernel32.GetProcessIoCounters(h, ctypes.byref(counters)):
                total += (counters.ReadTransferCount + counters.WriteTransferCount
                          + counters.OtherTransferCount)
                found = True
        finally:
            kernel32.CloseHandle(h)
    return total if found else None


# ---------------- 監視と状態管理 ----------------

class Controller:
    def __init__(self, events):
        self.events = events  # GUI へ状態を伝えるキュー
        self.cfg = load_config()
        self.fake_active = False
        self.activated_by = ""
        self.activated_at = 0.0
        self.last_activity = 0.0
        self.saved_original = ""
        self.discord_active = False  # False の間は待機状態 (Discord の起動待ち)
        self.lock = threading.Lock()

    def status(self, text):
        self.events.put(("status", text))

    MIN_ON_SECONDS = 10
    QUIET_RESTORE_SECONDS = 45

    def activate_fake(self, reason):
        with self.lock:
            self.last_activity = time.time()
            if self.fake_active:
                return
            fake = self.cfg.get("fake_wallpaper", "")
            if not fake:
                log("フェイク壁紙が未設定なので切り替えません (" + reason + ")")
                self.status("共有らしい動きを検知。でもフェイク壁紙が未設定です")
                return
            manual = self.cfg.get("original_wallpaper_manual", "")
            if manual and is_valid_image_path(manual):
                self.saved_original = manual
            else:
                current = get_wallpaper()
                if current and current.lower() != fake.lower():
                    self.saved_original = current
                    self.cfg["original_wallpaper"] = current
                    save_config(self.cfg)
            if set_wallpaper(fake):
                self.fake_active = True
                self.activated_by = reason
                self.activated_at = time.time()
                self.last_activity = time.time()
                log("フェイク壁紙 ON (" + reason + ")")
                self.status("画面共有中: フェイク壁紙に切り替え済み")
                self.events.put(("fake", True))

    def restore(self, reason):
        with self.lock:
            if not self.fake_active:
                return
            manual = self.cfg.get("original_wallpaper_manual", "")
            if manual and is_valid_image_path(manual):
                original = manual
            else:
                original = self.saved_original or self.cfg.get("original_wallpaper", "")
            if original:
                set_wallpaper(original)
            self.fake_active = False
            self.activated_by = ""
            log("元の壁紙に復元 (" + reason + ")")
            self.status("共有終了: 元の壁紙に戻しました")
            self.events.put(("fake", False))

    def handle_line(self, line, source=""):
        kind = classify_line(line)
        if kind is None:
            return
        if self.cfg.get("diag_log", True):
            diag("[" + kind + "] (" + source + ") " + line.strip()[:500])
        if not self.cfg.get("enabled", True):
            return
        low = line.lower()
        if self.fake_active and (kind == "screen_start" or "outbound screen share stats" in low):
            # 「まだ共有中」の合図は、共有の統計行だけにする。
            # v4 は音声通話の行でも更新していたため、共有を止めても通話中は永遠に戻らなかった
            self.last_activity = time.time()
        if kind == "screen_start":
            self.activate_fake("ログで画面全体の共有を検知")
        elif kind == "stream_stop_exact":
            self.restore("ログで共有終了を検知 (正確な終了行)")
        elif kind == "stream_stop":
            now = time.time()
            if self.fake_active and now - self.activated_at < self.MIN_ON_SECONDS:
                log("終了らしい行が来ましたが、切り替えた直後なので無視します (点滅防止)")
                return
            if self.fake_active and now - self.last_activity < 5:
                log("終了らしい行が来ましたが、共有中のログがまだ流れているので無視します (点滅防止)")
                return
            self.restore("ログで共有終了を検知")

    def check_quiet_restore(self):
        """共有中のログが長く途絶えたら、終了の合図を取りこぼしたと考えて戻す。"""
        if not self.fake_active:
            return
        if self.activated_by.startswith("補助検知"):
            return
        if time.time() - self.last_activity > self.QUIET_RESTORE_SECONDS:
            self.restore("共有のログが " + str(self.QUIET_RESTORE_SECONDS) + " 秒途絶えたため")


class LogWatcher(threading.Thread):
    """v2: Discord のログフォルダ内の全ファイルを監視する。
    - 対象は .log だけではない。discord-webrtc_0 のような拡張子なしファイルが本命
    - 監視中に新しく作られたファイルは先頭から読む (v1 は丸ごと読み飛ばしていた)
    - フォルダ変化の通知 (イベント駆動) で即スキャンし、保険で1秒ごとの確認もする
    """

    def __init__(self, controller):
        super().__init__(daemon=True)
        self.ctl = controller
        self.offsets = {}
        self.stop_flag = threading.Event()
        self.dirs = []

    def prime(self, dirs):
        for d in dirs:
            try:
                names = os.listdir(d)
            except Exception:
                continue
            for name in names:
                p = os.path.join(d, name)
                try:
                    if os.path.isfile(p):
                        self.offsets[p] = os.path.getsize(p)
                except Exception:
                    pass

    def scan_new(self, dirs=None):
        for d in (dirs or self.dirs):
            try:
                names = os.listdir(d)
            except Exception:
                continue
            for name in names:
                p = os.path.join(d, name)
                try:
                    if not os.path.isfile(p):
                        continue
                    size = os.path.getsize(p)
                except Exception:
                    continue
                if p not in self.offsets:
                    start = 0
                    log("新しいログファイルを発見 (先頭から読みます): " + name)
                else:
                    start = self.offsets[p]
                if size < start:
                    start = 0
                if size == start:
                    self.offsets[p] = size
                    continue
                try:
                    with open(p, "rb") as f:
                        f.seek(start)
                        data = f.read(size - start)
                    self.offsets[p] = size
                except Exception as e:
                    log("ログ読み取り失敗 " + name + ": " + str(e))
                    continue
                is_webrtc = "webrtc" in name.lower()
                text = data.decode("utf-8", errors="ignore")
                for line in text.splitlines():
                    if not line.strip():
                        continue
                    if is_webrtc and self.ctl.cfg.get("diag_log", True):
                        diag("[webrtc-raw] " + line.strip()[:500])
                    self.ctl.handle_line(line, source=name)

    def watch_dir_events(self, d):
        try:
            kernel32 = _kernel32()
            h = kernel32.CreateFileW(d, 1, 7, None, 3, 0x02000000, None)
            if not h or h == INVALID_HANDLE:
                log("フォルダ監視を開けませんでした: " + d
                    + " error=" + str(ctypes.get_last_error()))
                return
            log("イベント駆動の監視を開始: " + d)
            buf = ctypes.create_string_buffer(16384)
            while not self.stop_flag.is_set():
                bytes_returned = wintypes.DWORD(0)
                ok = kernel32.ReadDirectoryChangesW(
                    h, buf, len(buf), False, 0x00000001 | 0x00000002 | 0x00000008 | 0x00000010,
                    ctypes.byref(bytes_returned), None, None)
                if not ok:
                    log("フォルダ監視が失敗しました error=" + str(ctypes.get_last_error())
                        + " (保険の定期確認で続けます)")
                    kernel32.CloseHandle(h)
                    return
                self.scan_new([d])
        except Exception as e:
            log("フォルダ監視スレッドで例外: " + str(e))

    def run(self):
        self.dirs = discord_logs_dirs()
        if not self.dirs:
            log("Discord のログフォルダが見つかりませんでした")
            self.ctl.status("Discordのログが見つかりません (Discord未導入か未起動)")
            return
        for d in self.dirs:
            try:
                files = sorted(os.listdir(d))
            except Exception:
                files = []
            log("監視フォルダ: " + d + " / ファイル: " + ", ".join(files))
        self.prime(self.dirs)
        for d in self.dirs:
            threading.Thread(target=self.watch_dir_events, args=(d,), daemon=True).start()
        while not self.stop_flag.is_set():
            self.scan_new(self.dirs)
            self.stop_flag.wait(1.0)


class WindowWatch(threading.Thread):
    """Discord のウィンドウ状態を見る (記録専用、壁紙は切り替えない)。
    共有を始めると Discord 側に新しいウィンドウやタイトルの変化が出ることがある。
    その変化を診断ログに残し、判定材料にする。Discord 本体には触らない。"""

    def __init__(self, controller):
        super().__init__(daemon=True)
        self.ctl = controller
        self.known = set()

    def snapshot(self):
        if not IS_WINDOWS:
            return set()
        user32 = ctypes.windll.user32
        user32.EnumWindows.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        pids = set(discord_pids())
        found = set()
        if not pids:
            return found

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def callback(hwnd, lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids:
                buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, buf, 256)
                cls = ctypes.create_unicode_buffer(128)
                user32.GetClassNameW(hwnd, cls, 128)
                found.add((buf.value or "(タイトルなし)", cls.value))
            return True

        user32.EnumWindows(callback, 0)
        return found

    def run(self):
        while True:
            try:
                current = self.snapshot()
                for item in sorted(current - self.known):
                    if self.ctl.cfg.get("diag_log", True):
                        diag("[window] 出現: " + item[0] + " / " + item[1])
                for item in sorted(self.known - current):
                    if self.ctl.cfg.get("diag_log", True):
                        diag("[window] 消滅: " + item[0] + " / " + item[1])
                self.known = current
            except Exception as e:
                log("ウィンドウ監視で例外: " + str(e))
            time.sleep(2)


class DiscordWatch(threading.Thread):
    """Discord の起動/終了を見る。終了していたら安全のため壁紙を戻す。"""

    def __init__(self, controller):
        super().__init__(daemon=True)
        self.ctl = controller
        self.was_running = None

    def run(self):
        while True:
            running = discord_running()
            if self.was_running is None:
                self.was_running = running
                self.ctl.discord_active = running
                self.ctl.events.put(("discord", running))
            elif running and not self.was_running:
                log("Discord の起動を検知 -> 待機状態から監視状態へ")
                self.ctl.discord_active = True
                self.ctl.events.put(("discord", True))
                self.ctl.status("Discord監視中 (共有が始まったら壁紙を守ります)")
            elif not running and self.was_running:
                log("Discord の終了を検知 -> 待機状態へ戻る")
                self.ctl.discord_active = False
                self.ctl.events.put(("discord", False))
                self.ctl.restore("Discordが終了したため")
                self.ctl.status("待機中 (Discordが起動したら監視します)")
            self.was_running = running
            self.ctl.check_quiet_restore()
            time.sleep(2)



def make_log_bundle(dest_path):
    """診断材料を1つのZIPにまとめる。妖精に渡すのはこのファイル1つでいい。
    中身: 自分のログ2つ + Discord のログファイルの末尾 (WebRTC / renderer)。"""
    import zipfile
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in (LOG_PATH, DIAG_PATH, CONFIG_PATH):
            if os.path.isfile(p):
                z.write(p, os.path.basename(p))
        for d in discord_logs_dirs():
            try:
                names = os.listdir(d)
            except Exception:
                continue
            for name in names:
                p = os.path.join(d, name)
                try:
                    if not os.path.isfile(p):
                        continue
                    size = os.path.getsize(p)
                    with open(p, "rb") as f:
                        f.seek(max(0, size - 300000))
                        data = f.read()
                    z.writestr("discord_logs/" + name, data)
                except Exception:
                    pass
    return dest_path


# ---------------- 画面 ----------------

TASK_NAME = "DiscordWallpaperFaker"


def admin_task_exists():
    if not IS_WINDOWS:
        return False
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME],
                           capture_output=True, text=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0
    except Exception:
        return False


def do_admin_task(enabled):
    """管理者に昇格した自プロセスから呼ばれる。タスクの登録/解除だけをして終わる。"""
    exe = sys.executable if getattr(sys, "frozen", False) else ""
    if not exe:
        log("管理者タスクの操作は exe 版でのみ可能です")
        return
    if enabled:
        tr = '"' + exe + '" --background'
        r = subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME, "/TR", tr,
                            "/SC", "ONLOGON", "/RL", "HIGHEST", "/F"],
                           capture_output=True, text=True)
        log("管理者常駐タスクを登録: returncode=" + str(r.returncode))
    else:
        r = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                           capture_output=True, text=True)
        log("管理者常駐タスクを解除: returncode=" + str(r.returncode))


def is_admin():
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    """UAC の認証を出して、自分を管理者で起動し直す。"""
    if not IS_WINDOWS or not getattr(sys, "frozen", False):
        return False
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, "", None, 1)
        return int(rc) > 32
    except Exception as e:
        log("管理者での再起動に失敗: " + str(e))
        return False


def request_admin_task(enabled):
    """UAC の確認を出して、自分自身を管理者で起動し直してタスクを操作させる。"""
    if not IS_WINDOWS or not getattr(sys, "frozen", False):
        return False
    arg = "--admin-task-on" if enabled else "--admin-task-off"
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, arg, None, 0)
        return int(rc) > 32
    except Exception as e:
        log("管理者での起動に失敗: " + str(e))
        return False


def resource_path(name):
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(base, name)
    if os.path.isfile(p):
        return p
    alt = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", name)
    return alt if os.path.isfile(alt) else p


RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "DiscordWallpaperFaker"


def run_key_exists():
    if not IS_WINDOWS:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH) as k:
            winreg.QueryValueEx(k, RUN_VALUE_NAME)
        return True
    except Exception:
        return False


def set_run_key(enabled):
    """通常の自動起動 (レジストリ Run キー)。管理者権限は要らない。"""
    if not IS_WINDOWS or not getattr(sys, "frozen", False):
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0,
                            winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, RUN_VALUE_NAME, 0, winreg.REG_SZ,
                                  '"' + sys.executable + '" --background')
            else:
                try:
                    winreg.DeleteValue(k, RUN_VALUE_NAME)
                except FileNotFoundError:
                    pass
        log("通常の自動起動を " + ("ON" if enabled else "OFF") + " にしました")
        return True
    except Exception as e:
        log("自動起動の設定に失敗: " + str(e))
        return False


class TrayIcon(threading.Thread):
    """通知領域 (トレイ) のアイコン。専用スレッドで小さい Win32 ウィンドウを持つ。"""
    WM_TRAYICON = 0x0400 + 21
    NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
    NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4

    def __init__(self, events):
        super().__init__(daemon=True)
        self.events = events
        self.ready = threading.Event()
        self.ok = False
        self.hwnd = None
        self.thread_id = None
        self._icons = {}

    def _load_icon(self, name):
        path = resource_path(name)
        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = ctypes.c_void_p
        user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        h = user32.LoadImageW(None, path, 1, 0, 0, 0x10 | 0x40)
        log("トレイ用アイコン読み込み: " + name + " -> " + path + " handle=" + str(h))
        return h

    def _nid(self, icon, tip):
        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong), ("hWnd", ctypes.c_void_p),
                        ("uID", ctypes.c_uint), ("uFlags", ctypes.c_uint),
                        ("uCallbackMessage", ctypes.c_uint), ("hIcon", ctypes.c_void_p),
                        ("szTip", ctypes.c_wchar * 128), ("dwState", ctypes.c_ulong),
                        ("dwStateMask", ctypes.c_ulong), ("szInfo", ctypes.c_wchar * 256),
                        ("uTimeoutOrVersion", ctypes.c_uint),
                        ("szInfoTitle", ctypes.c_wchar * 64),
                        ("dwInfoFlags", ctypes.c_ulong),
                        ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", ctypes.c_void_p)]
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP
        nid.uCallbackMessage = self.WM_TRAYICON
        nid.hIcon = icon
        nid.szTip = tip
        return nid

    def run(self):
        if not IS_WINDOWS:
            self.ready.set()
            return
        try:
            self._run()
        except Exception as e:
            log("トレイアイコンの作成に失敗: " + str(e))
            self.ready.set()

    def _run(self):
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32
        # 64bit でハンドルが切り捨てられないように、戻り値の型を全部明示する
        user32.CreateWindowExW.restype = ctypes.c_void_p
        user32.CreateWindowExW.argtypes = [ctypes.c_ulong, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                           ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                          ctypes.c_size_t, ctypes.c_ssize_t]
        user32.DispatchMessageW.restype = ctypes.c_ssize_t
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        shell32.Shell_NotifyIconW.restype = ctypes.c_int
        shell32.Shell_NotifyIconW.argtypes = [ctypes.c_ulong, ctypes.c_void_p]
        self.thread_id = kernel32.GetCurrentThreadId()
        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_void_p,
                                     ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", ctypes.c_void_p),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
                        ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                        ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p)]

        class MSG(ctypes.Structure):
            _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                        ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                        ("time", ctypes.c_ulong), ("pt_x", ctypes.c_long),
                        ("pt_y", ctypes.c_long)]

        events = self.events

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == TrayIcon.WM_TRAYICON:
                mouse = lparam & 0xFFFF
                if mouse in (0x0202, 0x0203, 0x0205):  # 左/ダブル/右クリック
                    events.put(("tray_show", None))
                return 0
            if msg == 0x0010:  # WM_CLOSE
                user32.DestroyWindow(hwnd)
                return 0
            if msg == 0x0002:  # WM_DESTROY
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._proc = WNDPROC(wndproc)
        wc = WNDCLASSW()
        wc.lpfnWndProc = ctypes.cast(self._proc, ctypes.c_void_p)
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = "DiscordWallpaperFakerTray"
        user32.RegisterClassW(ctypes.byref(wc))
        hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "", 0, 0, 0, 0, 0,
                                      ctypes.c_void_p(2 ** 64 - 3), None, wc.hInstance, None)
        if not hwnd:
            log("トレイ用ウィンドウの作成に失敗 GetLastError=" + str(kernel32.GetLastError()))
            self.ready.set()
            return
        self.hwnd = hwnd
        self._icons = {"idle": self._load_icon("tray_idle.ico"),
                       "active": self._load_icon("tray_active.ico")}
        nid = self._nid(self._icons.get("idle"), "Discord Wallpaper Faker (待機中: Discordの起動待ち)")
        if shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(nid)):
            self.ok = True
            log("トレイアイコンを表示しました")
        else:
            log("トレイアイコンの登録に失敗 GetLastError=" + str(kernel32.GetLastError()))
        self.ready.set()
        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def set_state(self, state):
        """state: standby (Discord待ち) / active (監視中) / sharing (共有中)"""
        if not self.ok or not self.hwnd:
            return
        try:
            icon = self._icons.get("active" if state == "sharing" else "idle")
            tip = {"standby": "Discord Wallpaper Faker (待機中: Discordの起動待ち)",
                   "active": "Discord Wallpaper Faker (監視中)",
                   "sharing": "Discord Wallpaper Faker (共有中: フェイク壁紙表示中)"}.get(state, "")
            nid = self._nid(icon, tip)
            shell32 = ctypes.windll.shell32
            shell32.Shell_NotifyIconW.restype = ctypes.c_int
            shell32.Shell_NotifyIconW.argtypes = [ctypes.c_ulong, ctypes.c_void_p]
            shell32.Shell_NotifyIconW(self.NIM_MODIFY, ctypes.byref(nid))
        except Exception:
            pass

    def set_active(self, active):
        self.set_state("sharing" if active else "active")

    def close(self):
        if not self.hwnd:
            return
        try:
            nid = self._nid(None, "")
            nid.uFlags = 0
            ctypes.windll.shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(nid))
        except Exception:
            pass
        if self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)  # WM_QUIT


def run_gui(background=False):
    import tkinter as tk
    from tkinter import messagebox

    events = queue.Queue()
    ctl = Controller(events)
    log("Discord Wallpaper Faker v6 起動 / Discord動作中=" + str(discord_running())
        + " / プロセス数=" + str(len(discord_pids())))
    LogWatcher(ctl).start()
    DiscordWatch(ctl).start()
    WindowWatch(ctl).start()

    root = tk.Tk()
    root.title("Discord Wallpaper Faker")
    root.geometry("520x400")
    root.resizable(False, False)

    status_var = tk.StringVar(value="起動中...")
    fake_disp = tk.StringVar(value="")
    orig_disp = tk.StringVar(value="")

    def refresh_summary():
        fake = ctl.cfg.get("fake_wallpaper", "")
        orig = ctl.cfg.get("original_wallpaper_manual", "") or ctl.cfg.get("original_wallpaper", "")
        fake_disp.set("フェイク壁紙: " + (fake if fake else "(未設定)"))
        orig_disp.set("元の壁紙: " + (orig if orig else "(未設定 - 自動で記録します)"))

    tk.Label(root, text="Discord Wallpaper Faker", font=("", 14, "bold")).pack(pady=(10, 2))
    tk.Label(root, text="Discordで画面全体を共有した時だけ、壁紙を自動で隠します").pack()
    tk.Label(root, textvariable=status_var, fg="#0a6d2d", wraplength=480).pack(pady=6)
    tk.Label(root, textvariable=fake_disp, wraplength=480, fg="#444444").pack()
    tk.Label(root, textvariable=orig_disp, wraplength=480, fg="#444444").pack()
    auto_var = tk.StringVar(value="")
    auto_label = tk.Label(root, textvariable=auto_var, wraplength=480, fg="#666666")
    auto_label.pack()

    def refresh_autostart():
        if not getattr(sys, "frozen", False):
            auto_var.set("自動起動: (exe 版でのみ設定できます)")
            return
        modes = []
        if run_key_exists():
            modes.append("スタートアップ")
        if admin_task_exists():
            modes.append("管理者タスク")
        if modes:
            auto_var.set("自動起動: ON (" + " + ".join(modes) + ") - ログインすれば待機状態で常駐します")
            auto_label.config(fg="#0a6d2d")
        else:
            auto_var.set("自動起動: OFF - このままだと Discord を起動しても動きません。設定でONにしてください")
            auto_label.config(fg="#b00020")

    refresh_summary()
    refresh_autostart()
    try:
        root.iconbitmap(resource_path("app.ico"))
    except Exception:
        pass

    tray_holder = {"tray": None}

    def full_exit():
        ctl.restore("完全終了のため")
        if tray_holder["tray"] is not None:
            tray_holder["tray"].close()
        log("Discord Wallpaper Faker 完全終了")
        try:
            root.destroy()
        finally:
            os._exit(0)

    # ---------- 設定パネル ----------
    def open_settings():
        win = tk.Toplevel(root)
        win.title("Discord Wallpaper Faker 設定")
        win.geometry("560x620")
        win.resizable(False, False)
        win.transient(root)
        win.lift()
        win.focus_force()

        fake_var = tk.StringVar(value=ctl.cfg.get("fake_wallpaper", ""))
        orig_var = tk.StringVar(value=ctl.cfg.get("original_wallpaper_manual", ""))
        enabled_var = tk.BooleanVar(value=bool(ctl.cfg.get("enabled", True)))
        diag_var = tk.BooleanVar(value=bool(ctl.cfg.get("diag_log", True)))
        is_frozen = bool(getattr(sys, "frozen", False))
        admin_now = admin_task_exists() if is_frozen else False
        admin_var = tk.BooleanVar(value=admin_now)
        run_now = run_key_exists() if is_frozen else False
        run_var = tk.BooleanVar(value=run_now)

        def apply_path(var, key, label):
            path = var.get().strip().strip('"')
            if not path and key == "fake_wallpaper":
                return True
            if key == "original_wallpaper_manual" and not path:
                ctl.cfg[key] = ""
                save_config(ctl.cfg)
                refresh_summary()
                return True
            if not is_valid_image_path(path):
                messagebox.showwarning("Discord Wallpaper Faker", label + "の画像が見つからないか、形式が違います。" + chr(10) + path, parent=win)
                return False
            ctl.cfg[key] = path
            save_config(ctl.cfg)
            var.set(path)
            refresh_summary()
            log(label + "を設定: " + path)
            return True

        tk.Label(win, text="フェイク壁紙の画像 (共有中に表示する物)。パスを貼り付けて設定:").pack(anchor="w", padx=14, pady=(12, 0))
        tk.Entry(win, textvariable=fake_var).pack(fill="x", padx=14, pady=3)
        row = tk.Frame(win); row.pack(fill="x", padx=14)
        tk.Button(row, text="入力したパスで設定",
                  command=lambda: apply_path(fake_var, "fake_wallpaper", "フェイク壁紙")).pack(side="left")

        tk.Label(win, text="元の壁紙の画像 (空なら自動で記録した物に戻します):").pack(anchor="w", padx=14, pady=(14, 0))
        tk.Entry(win, textvariable=orig_var).pack(fill="x", padx=14, pady=3)
        row2 = tk.Frame(win); row2.pack(fill="x", padx=14)
        tk.Button(row2, text="入力したパスで設定",
                  command=lambda: apply_path(orig_var, "original_wallpaper_manual", "元の壁紙")).pack(side="left", padx=(0, 6))
        tk.Button(row2, text="今の壁紙を登録",
                  command=lambda: (orig_var.set(get_wallpaper()), apply_path(orig_var, "original_wallpaper_manual", "元の壁紙"))).pack(side="left")

        opts = tk.Frame(win); opts.pack(fill="x", padx=14, pady=14)
        tk.Checkbutton(opts, text="共有の検知を有効にする", variable=enabled_var).pack(anchor="w")
        tk.Checkbutton(opts, text="診断ログを残す (判定の材料になります)", variable=diag_var).pack(anchor="w")
        run_chk = tk.Checkbutton(
            opts, text="スタートアップに登録する (ログイン時は待機状態で起動し、Discordが起動したら監視開始)", variable=run_var)
        run_chk.pack(anchor="w")
        admin_chk = tk.Checkbutton(
            opts, text="管理者権限で常駐させる (ログイン時に自動起動)", variable=admin_var)
        admin_chk.pack(anchor="w")
        if not is_frozen:
            run_chk.config(state="disabled")
            admin_chk.config(state="disabled")
            tk.Label(opts, text="自動起動の項目は exe 版でのみ設定できます", fg="#888888").pack(anchor="w")
        tk.Label(opts, text="管理者の方を変えると、保存の時に Windows の管理者確認 (UAC) が出ます。通常の方は確認なしです。",
                 fg="#888888", wraplength=500).pack(anchor="w")

        def save_settings():
            if not apply_path(fake_var, "fake_wallpaper", "フェイク壁紙"):
                return
            if not apply_path(orig_var, "original_wallpaper_manual", "元の壁紙"):
                return
            ctl.cfg["enabled"] = bool(enabled_var.get())
            ctl.cfg["diag_log"] = bool(diag_var.get())
            save_config(ctl.cfg)
            if is_frozen and bool(run_var.get()) != run_now:
                set_run_key(bool(run_var.get()))
            refresh_autostart()
            if is_frozen and bool(admin_var.get()) != admin_now:
                if request_admin_task(bool(admin_var.get())):
                    status_var.set("管理者常駐の設定を変更しました (UACで許可してください)")
                else:
                    messagebox.showwarning("Discord Wallpaper Faker", "管理者での起動ができませんでした", parent=win)
            refresh_summary()
            messagebox.showinfo("Discord Wallpaper Faker", "設定を保存しました", parent=win)
            win.destroy()

        btns = tk.Frame(win); btns.pack(pady=10)
        tk.Button(btns, text="設定を保存", command=save_settings).pack(side="left", padx=6)
        tk.Button(btns, text="閉じる", command=win.destroy).pack(side="left", padx=6)
        credit2 = tk.Label(win, text="制作者: TRS00131  https://github.com/trs00131",
                           fg="#1a5fb4", cursor="hand2")
        credit2.pack(pady=(0, 8))
        credit2.bind("<Button-1>", lambda e: __import__("webbrowser").open("https://github.com/trs00131"))

    # ---------- メイン画面のボタン ----------
    def test_switch():
        fake = ctl.cfg.get("fake_wallpaper", "")
        if not fake:
            messagebox.showwarning("Discord Wallpaper Faker", "先に設定でフェイク壁紙のパスを設定してください")
            return
        manual = ctl.cfg.get("original_wallpaper_manual", "")
        original = manual if (manual and is_valid_image_path(manual)) else get_wallpaper()
        if set_wallpaper(fake):
            status_var.set("テスト中: 3秒だけフェイク壁紙にしています")
            root.after(3000, lambda: (set_wallpaper(original), status_var.set("テスト完了: 元に戻しました")))

    def export_logs():
        try:
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            dest = os.path.join(desktop, "DiscordWallpaperFaker_logs.zip")
            make_log_bundle(dest)
            status_var.set("診断ログをデスクトップに保存しました: DiscordWallpaperFaker_logs.zip")
            messagebox.showinfo("Discord Wallpaper Faker", "デスクトップに DiscordWallpaperFaker_logs.zip を作りました。そのまま妖精に送ってください。")
        except Exception as e:
            messagebox.showerror("Discord Wallpaper Faker", "ログの書き出しに失敗しました: " + str(e))

    row = tk.Frame(root); row.pack(pady=(12, 4))
    tk.Button(row, text="設定", width=14, command=open_settings).pack(side="left", padx=4)
    tk.Button(row, text="3秒だけテスト切替", width=16, command=test_switch).pack(side="left", padx=4)
    tk.Button(row, text="今すぐ元に戻す", width=14,
              command=lambda: ctl.restore("ボタンで手動復元")).pack(side="left", padx=4)
    row2 = tk.Frame(root); row2.pack(pady=2)
    tk.Button(row2, text="ログフォルダを開く", width=16,
              command=lambda: os.startfile(DATA_DIR) if IS_WINDOWS else None).pack(side="left", padx=4)
    tk.Button(row2, text="診断ログZIP", width=14, command=export_logs).pack(side="left", padx=4)
    tk.Button(row2, text="完全終了", width=14, fg="#b00020", command=full_exit).pack(side="left", padx=4)
    priv_var = tk.StringVar(value="実行中の権限: " + ("管理者" if is_admin() else "通常"))

    def restart_as_admin():
        if is_admin():
            messagebox.showinfo("Discord Wallpaper Faker", "すでに管理者で実行中です")
            return
        if relaunch_as_admin():
            ctl.restore("管理者で再起動するため")
            if tray_holder["tray"] is not None:
                tray_holder["tray"].close()
            root.destroy()
            os._exit(0)
        else:
            messagebox.showwarning("Discord Wallpaper Faker", "管理者での起動ができませんでした (exe 版でのみ使えます)")

    row3 = tk.Frame(root); row3.pack(pady=2)
    tk.Button(row3, text="管理者として再起動", width=18, command=restart_as_admin).pack(side="left", padx=4)
    tk.Label(row3, textvariable=priv_var, fg="#666666").pack(side="left", padx=4)
    tk.Label(root, text="ウィンドウを閉じると通知領域 (トレイ) に常駐します。アイコンの色で状態が分かります (緑=待機中 / 赤=共有中)。完全に終わらせる時は完全終了ボタン。",
             fg="#666666", wraplength=480).pack(pady=(8, 2))
    credit = tk.Label(root, text="制作者: TRS00131  https://github.com/trs00131",
                      fg="#1a5fb4", cursor="hand2")
    credit.pack()
    credit.bind("<Button-1>", lambda e: __import__("webbrowser").open("https://github.com/trs00131"))

    # 通知領域 (トレイ) に常駐する。作れなかった時だけ右上の点に fallback する
    dot = tk.Toplevel(root)
    dot.overrideredirect(True)
    dot.attributes("-topmost", True)
    sw = root.winfo_screenwidth()
    dot.geometry("20x20+" + str(sw - 30) + "+10")
    dot_label = tk.Label(dot, text="●", fg="#2e9e4f", bg="#222222")
    dot_label.pack(fill="both", expand=True)
    dot.withdraw()

    tray = TrayIcon(events)
    tray_holder["tray"] = tray
    tray.start()
    tray.ready.wait(2.5)
    use_tray = bool(tray.ok)
    if use_tray:
        log("常駐先: 通知領域 (トレイ)")
    else:
        log("常駐先: 右上の点 (トレイが作れなかったため)。原因はこのログの少し上にあります")
    discord_on = {"v": bool(ctl.discord_active)}

    def current_tray_state():
        if ctl.fake_active:
            return "sharing"
        return "active" if discord_on["v"] else "standby"

    def hide_to_dot():
        root.withdraw()
        if not use_tray:
            dot.deiconify()

    def show_window():
        dot.withdraw()
        root.deiconify()
        root.lift()
        refresh_autostart()

    dot_label.bind("<Button-1>", lambda e: show_window())
    dot_label.bind("<Button-3>", lambda e: full_exit())

    def poll():
        try:
            while True:
                kind, payload = events.get_nowait()
                if kind == "status":
                    status_var.set(payload)
                elif kind == "fake":
                    if use_tray:
                        tray.set_state(current_tray_state())
                    else:
                        dot_label.config(fg="#d23b3b" if payload else "#2e9e4f")
                elif kind == "discord":
                    discord_on["v"] = bool(payload)
                    if use_tray:
                        tray.set_state(current_tray_state())
                elif kind == "tray_show":
                    show_window()
        except queue.Empty:
            pass
        root.after(150, poll)

    root.after(150, poll)

    root.after(150, poll)
    root.protocol("WM_DELETE_WINDOW", hide_to_dot)
    if background:
        root.after(300, hide_to_dot)
    root.mainloop()


if __name__ == "__main__":
    if "--admin-task-on" in sys.argv:
        do_admin_task(True)
    elif "--admin-task-off" in sys.argv:
        do_admin_task(False)
    else:
        run_gui(background="--background" in sys.argv)
