# -*- coding: utf-8 -*-
"""
QRで音源を取得 + Arduinoの"close"で1秒だけ音を再生（macOS）
- スレッド1: WebカメラでQR検出 → audio.wav をダウンロード（API_ENDPOINT/{decodedText}/audio）
- スレッド2: シリアルで"close"受信 → audio.wav の続きを 1秒だけ afplay で再生
"""

import os
import sys
import time
import wave
import cv2
import requests
import serial
import traceback
import tempfile
import subprocess
import threading
from pathlib import Path

# ========= 設定 =========
API_ENDPOINT = "http://upiscium.f5.si:8001"

# シリアル
PORT = "/dev/cu.usbmodem11301"
BAUDRATE = 9600
SER_TIMEOUT = 1
RETRY_SEC = 2
ENCODING = "utf-8"

# 再生
AUDIO_FILE = "audio.wav"
SEG_SEC = 1.0  # 1回の再生秒数

# カメラ
CAM_INDEX = 0
CAM_POLL_DELAY = 0.01  # CPU負荷軽減

# ========================

def log(msg: str):
    print(time.strftime("[%H:%M:%S]"), msg, flush=True)

# ---- 再生ユーティリティ ----
def play_blocking_macos(pcm: bytes, channels: int, sampwidth: int, framerate: int):
    """一時WAVを書いてafplayでブロッキング再生（macOS）"""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
        tmp_name = tmp.name
    try:
        with wave.open(tmp_name, "wb") as out:
            out.setnchannels(channels)
            out.setsampwidth(sampwidth)
            out.setframerate(framerate)
            out.writeframes(pcm)
        subprocess.run(["afplay", tmp_name], check=False)
    finally:
        try:
            os.remove(tmp_name)
        except Exception:
            pass

def open_wav(path: str) -> wave.Wave_read:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"WAV が見つかりません: {p.resolve()}")
    wf = wave.open(str(p), "rb")
    log(f"[WAV] {p.name}: {wf.getnchannels()}ch, {wf.getsampwidth()*8}bit, {wf.getframerate()}Hz, {wf.getnframes()}frames")
    return wf

def read_exact_sec(wf: wave.Wave_read, sec: float) -> bytes:
    """現在位置からちょうど sec 秒分のPCMを返す。末尾で不足は先頭に巻き戻して埋める。"""
    fr, ch, sw = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
    need_frames = int(fr * sec)
    chunks, remaining = [], need_frames
    while remaining > 0:
        frames = wf.readframes(remaining)
        if not frames:
            wf.rewind()
            continue
        chunks.append(frames)
        got_frames = len(frames) // (ch * sw)
        remaining -= got_frames
    return b"".join(chunks)

def open_serial_forever():
    while True:
        try:
            ser = serial.Serial(PORT, BAUDRATE, timeout=SER_TIMEOUT)
            # 自動リセット抑止トライ
            for attr in ("dtr", "rts"):
                try:
                    setattr(ser, attr, False)
                except Exception:
                    pass
            log(f"[Serial] Open: {PORT} @ {BAUDRATE}")
            time.sleep(2)  # 自動リセット待ち
            return ser
        except serial.SerialException as e:
            log(f"[warn] シリアル接続失敗: {e} -> {RETRY_SEC}s後に再試行")
            time.sleep(RETRY_SEC)

# ---- 共有状態 ----
class SharedAudioState:
    """音源ファイルの入れ替えをプレーヤに通知するための共有状態"""
    def __init__(self, audio_path: str):
        self.audio_path = audio_path
        self.reload_event = threading.Event()  # Trueなら次回再生前にロードやり直し
        self.lock = threading.Lock()

    def signal_reload(self):
        with self.lock:
            self.reload_event.set()

    def consume_reload(self) -> bool:
        """イベントを消費してbool返す"""
        with self.lock:
            if self.reload_event.is_set():
                self.reload_event.clear()
                return True
            return False

# ---- スレッド: QRスキャン & ダウンロード ----
def qr_download_thread(shared: SharedAudioState, stop_event: threading.Event):
    qr = cv2.QRCodeDetector()
    cap = cv2.VideoCapture(CAM_INDEX)
    if not cap.isOpened():
        log("[err] Webカメラを開けませんでした")
        return
    log("Web camera activated")

    last_id = None
    try:
        while not stop_event.is_set():
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.05)
                continue

            decodedText, points, _ = qr.detectAndDecode(frame)

            # 文字列が取れた時だけ処理（同じIDはスキップ）
            if decodedText:
                if decodedText == last_id:
                    # 同じQRを連続検出中。軽く待ってスキップ
                    time.sleep(0.2)
                else:
                    log(f"qr detected: {decodedText}")
                    try:
                        url = f"{API_ENDPOINT}/{decodedText}/audio"
                        r = requests.get(url, timeout=10)
                        r.raise_for_status()

                        # 一時ファイルに書いてからアトミック置換
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
                            tmp.write(r.content)
                            tmp_name = tmp.name
                        os.replace(tmp_name, AUDIO_FILE)
                        log("Audio file downloaded & replaced -> audio.wav")

                        # プレーヤにリロードを通知
                        shared.signal_reload()
                        last_id = decodedText
                    except Exception as e:
                        log(f"[err] ダウンロード失敗: {e}")
                        # 失敗しても監視継続

            # キー入力ウィンドウを出さないので軽くスリープ
            time.sleep(CAM_POLL_DELAY)
    finally:
        cap.release()
        cv2.destroyAllWindows()
        log("[camera] 終了")

# ---- スレッド: シリアル受信 & 再生 ----
def serial_player_thread(shared: SharedAudioState, stop_event: threading.Event):
    if sys.platform != "darwin":
        log("[warn] この実装は macOS 専用(afplay)。他OSは別手段が必要です。")

    # 初回はファイルがないかもしれないので待ち合わせはしない（存在すれば読む）
    wf = None
    channels = sampwidth = framerate = None

    def ensure_wav_open():
        nonlocal wf, channels, sampwidth, framerate
        # 既存があれば閉じる
        if wf is not None:
            try:
                wf.close()
            except Exception:
                pass
            wf = None
        # 新しく開く
        wf = open_wav(AUDIO_FILE)
        framerate = wf.getframerate()
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()

    # 最初に存在すれば開く（なければ後で"close"受信前にリロード要求が来るまで待機）
    if Path(AUDIO_FILE).exists():
        try:
            ensure_wav_open()
        except Exception as e:
            log(f"[warn] 初回WAVオープン失敗: {e}")

    ser = open_serial_forever()

    try:
        while not stop_event.is_set():
            try:
                msg = ser.readline().decode(ENCODING, errors="ignore").strip()
                if not msg:
                    # リロード要求があればここで処理
                    if shared.consume_reload():
                        try:
                            ensure_wav_open()
                        except Exception as e:
                            log(f"[warn] WAVリロード失敗: {e}")
                    continue

                log(f"[recv] {msg}")

                if msg.lower() == "close":
                    # 直前に音源が入れ替わっているかもしれないのでチェック
                    if shared.consume_reload() or wf is None:
                        try:
                            ensure_wav_open()
                        except Exception as e:
                            log(f"[err] WAVが開けず再生不可: {e}")
                            continue

                    log("[play] start 1s")
                    try:
                        pcm = read_exact_sec(wf, SEG_SEC)
                        play_blocking_macos(pcm, channels, sampwidth, framerate)
                        log("[play] done 1s")
                    except Exception:
                        log("[err] 再生中に例外発生:")
                        traceback.print_exc()

            except serial.SerialException as e:
                log(f"[warn] シリアル通信エラー: {e} -> 再接続します")
                try:
                    ser.close()
                except Exception:
                    pass
                ser = open_serial_forever()
            except Exception:
                log("[err] 予期しない例外（継続）:")
                traceback.print_exc()
                time.sleep(0.2)
    finally:
        try:
            ser.close()
        except Exception:
            pass
        if wf is not None:
            try:
                wf.close()
            except Exception:
                pass
        log("[player] 終了")

# ---- メイン ----
def main():
    stop_event = threading.Event()
    shared = SharedAudioState(AUDIO_FILE)

    cam_t = threading.Thread(target=qr_download_thread, args=(shared, stop_event), daemon=True)
    ser_t = threading.Thread(target=serial_player_thread, args=(shared, stop_event), daemon=True)

    cam_t.start()
    ser_t.start()

    log("起動しました。QRをかざすと音声を取得、Arduinoが 'close' を送ると1秒再生します。Ctrl+Cで終了。")

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        log("[info] 停止要求。終了します。")
        stop_event.set()
        cam_t.join(timeout=2.0)
        ser_t.join(timeout=2.0)

if __name__ == "__main__":
    main()
