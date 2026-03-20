#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import wave
import serial
import shutil
import threading
import traceback
from pathlib import Path
from typing import Optional, List

import numpy as np
import sounddevice as sd

# ========= 設定 =========
PORT = "/dev/tty.usbmodem101"   # 環境に合わせて変更
BAUDRATE = 115200
SER_TIMEOUT = 0.05
ENCODING = "utf-8"

AUDIO_TRIMMED = "trimmed.wav"
AUDIO_RAW = "audio.wav"

WAIT_BEFORE_PLAY_SEC = 0.10   # close受信後に少し待つ
FIXED_PLAY_SEC = 0.50         # 1回の咀嚼で鳴らす長さ
MAIN_LOOP_SLEEP = 0.003

# ---- ログ ----
def log(msg: str):
    print(time.strftime("[%H:%M:%S]"), msg, flush=True)

# ---- WAV読み込み補助 ----
def open_best_wav() -> wave.Wave_read:
    """
    trimmed.wav があればそれを使い、
    無ければ audio.wav を使う。
    """
    if Path(AUDIO_TRIMMED).exists():
        return wave.open(AUDIO_TRIMMED, "rb")
    if Path(AUDIO_RAW).exists():
        return wave.open(AUDIO_RAW, "rb")
    raise FileNotFoundError("trimmed.wav も audio.wav も見つかりません。")

def read_exact_sec(wf: wave.Wave_read, sec: float) -> bytes:
    fr = wf.getframerate()
    ch = wf.getnchannels()
    sw = wf.getsampwidth()

    need_frames = int(fr * float(sec))
    chunks: List[bytes] = []
    remain = need_frames

    while remain > 0:
        frames = wf.readframes(remain)
        if not frames:
            wf.rewind()
            continue
        chunks.append(frames)
        got = len(frames) // (ch * sw)
        remain -= got

    return b"".join(chunks)

def pcm_bytes_to_numpy(pcm: bytes, channels: int, sampwidth: int):
    if sampwidth == 2:
        arr = np.frombuffer(pcm, dtype=np.int16)
        if channels > 1:
            arr = arr.reshape(-1, channels)
        return arr
    elif sampwidth == 1:
        arr = np.frombuffer(pcm, dtype=np.int8).astype(np.float32) / 128.0
        if channels > 1:
            arr = arr.reshape(-1, channels)
        return arr
    elif sampwidth == 4:
        arr = np.frombuffer(pcm, dtype=np.int32).astype(np.float32) / (2**31)
        if channels > 1:
            arr = arr.reshape(-1, channels)
        return arr
    else:
        raise ValueError(f"Unsupported sample width: {sampwidth}")

def play_nonblocking(pcm: bytes, channels: int, sampwidth: int, framerate: int):
    arr = pcm_bytes_to_numpy(pcm, channels, sampwidth)
    sd.stop()
    sd.play(arr, framerate, blocking=False)

# ---- 共有状態 ----
class SharedState:
    def __init__(self):
        self.stop_event = threading.Event()
        self.next_requested = threading.Event()

# ---- キーボード監視 ----
def keyboard_watch_thread(shared: SharedState, ser: serial.Serial):
    """
    Shift+N（大文字 N）で next を送る
    """
    try:
        if os.name == "nt":
            import msvcrt
            while not shared.stop_event.is_set():
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch == "N":
                        try:
                            ser.write(b"next\n")
                            ser.flush()
                            sd.stop()
                            log("[key] Shift+N -> next")
                        except Exception as e:
                            log(f"[warn] next送信失敗: {e}")
                time.sleep(0.02)
        else:
            import tty
            import termios
            import select

            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                while not shared.stop_event.is_set():
                    rlist, _, _ = select.select([sys.stdin], [], [], 0.02)
                    if rlist:
                        ch = sys.stdin.read(1)
                        if ch == "N":
                            try:
                                ser.write(b"next\n")
                                ser.flush()
                                sd.stop()
                                log("[key] Shift+N -> next")
                            except Exception as e:
                                log(f"[warn] next送信失敗: {e}")
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    except Exception as e:
        log(f"[warn] keyboard thread error: {e}")

# ---- 受信＆再生 ----
def continuously_read_from_arduino(ser: serial.Serial, shared: SharedState):
    wf = None
    channels = None
    sampwidth = None
    framerate = None

    def reload_wav():
        nonlocal wf, channels, sampwidth, framerate
        if wf is not None:
            try:
                wf.close()
            except Exception:
                pass
        wf = open_best_wav()
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        log("[audio] WAV loaded")

    reload_wav()

    scheduled_play_at: Optional[float] = None
    playing = False
    play_end_at: Optional[float] = None

    while not shared.stop_event.is_set():
        try:
            if ser.in_waiting > 0:
                received = ser.readline().decode(ENCODING, errors="ignore").strip()
                if received:
                    print(f"Arduino: {received}")

                    if received.lower() == "open":
                        sd.stop()
                        playing = False
                        play_end_at = None
                        scheduled_play_at = None
                        log("[event] open -> stop")
                        continue

                    if received.lower() == "close":
                        scheduled_play_at = time.time() + WAIT_BEFORE_PLAY_SEC
                        log("[event] close -> schedule play")
                        continue

            now = time.time()

            if playing and play_end_at is not None and now >= play_end_at:
                playing = False
                play_end_at = None
                log("[play] done")

            if (not playing) and (scheduled_play_at is not None) and (now >= scheduled_play_at):
                scheduled_play_at = None
                try:
                    pcm = read_exact_sec(wf, FIXED_PLAY_SEC)
                    play_nonblocking(pcm, channels, sampwidth, framerate)
                    playing = True
                    play_end_at = time.time() + FIXED_PLAY_SEC
                    log(f"[play] start {FIXED_PLAY_SEC:.3f}s")
                except Exception:
                    log("[err] 再生エラー")
                    traceback.print_exc()
                    playing = False
                    play_end_at = None

            time.sleep(MAIN_LOOP_SLEEP)

        except serial.SerialException as e:
            log(f"[warn] serial error: {e}")
            time.sleep(0.2)
        except Exception as e:
            log(f"[warn] receiver error: {e}")
            time.sleep(0.1)

    try:
        if wf is not None:
            wf.close()
    except Exception:
        pass

# ---- メイン ----
def main():
    shared = SharedState()

    if not Path(AUDIO_TRIMMED).exists() and not Path(AUDIO_RAW).exists():
        print("trimmed.wav または audio.wav を同じフォルダに置いてください。")
        return

    try:
        ser = serial.Serial(PORT, BAUDRATE, timeout=SER_TIMEOUT)
        time.sleep(2.0)  # Arduino自動リセット待ち
    except serial.SerialException:
        print(f"シリアルポート {PORT} に接続できません。")
        return

    try:
        ser.write(b"new\n")
        ser.flush()
        log("[send] new")
    except Exception as e:
        print(f"new送信失敗: {e}")
        return

    rx_thread = threading.Thread(
        target=continuously_read_from_arduino,
        args=(ser, shared),
        daemon=True
    )
    rx_thread.start()

    key_thread = threading.Thread(
        target=keyboard_watch_thread,
        args=(shared, ser),
        daemon=True
    )
    key_thread.start()

    log("オフライン咀嚼テスト開始")
    log("手順: Arduinoでキャリブレーション → closeで音再生 / openで停止")
    log("Shift+N で next、Ctrl+C で終了")

    try:
        while True:
            time.sleep(0.2)
    except KeyboardInterrupt:
        log("[info] stopping...")
    finally:
        shared.stop_event.set()
        try:
            sd.stop()
        except Exception:
            pass
        try:
            ser.close()
        except Exception:
            pass
        rx_thread.join(timeout=2.0)
        key_thread.join(timeout=2.0)
        log("[main] end")

if __name__ == "__main__":
    main()