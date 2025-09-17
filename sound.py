# macOS向け: afplayで安定再生。close が来たら audio.wav の続きを 1秒だけ再生
# pip: pyserial だけでOK（simpleaudio不要）

import time, wave, serial, traceback, sys, os, struct, subprocess, tempfile
from pathlib import Path

PORT = "/dev/cu.usbmodem21301"
BAUDRATE = 9600
AUDIO_FILE = "audio.wav"
SEG_SEC = 1.0
ENCODING = "utf-8"
SER_TIMEOUT = 1
RETRY_SEC = 2

def log(msg): print(time.strftime("[%H:%M:%S]"), msg, flush=True)

def open_wav(path: str) -> wave.Wave_read:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"WAV が見つかりません: {p.resolve()}")
    wf = wave.open(str(p), "rb")
    log(f"[WAV] {AUDIO_FILE}: {wf.getnchannels()}ch, {wf.getsampwidth()*8}bit, {wf.getframerate()}Hz, {wf.getnframes()}frames")
    return wf

def read_exact_1s(wf: wave.Wave_read, sec: float) -> bytes:
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

def play_blocking_macos(pcm: bytes, channels: int, sampwidth: int, framerate: int):
    """一時WAVを書き、afplayでブロッキング再生（macOS）。"""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
        name = tmp.name
    try:
        with wave.open(name, "wb") as out:
            out.setnchannels(channels)
            out.setsampwidth(sampwidth)
            out.setframerate(framerate)
            out.writeframes(pcm)
        # -q 1 で静かめ、ブロッキングは run() で実現
        subprocess.run(["afplay", name], check=False)
    finally:
        try: os.remove(name)
        except Exception: pass

def open_serial_forever():
    while True:
        try:
            ser = serial.Serial(PORT, BAUDRATE, timeout=SER_TIMEOUT)
            # 自動リセット抑止トライ
            for attr in ("dtr", "rts"):
                try: setattr(ser, attr, False)
                except Exception: pass
            log(f"[Serial] Open: {PORT} @ {BAUDRATE}")
            time.sleep(2)  # 自動リセット待ち
            return ser
        except serial.SerialException as e:
            log(f"[warn] シリアル接続失敗: {e} -> {RETRY_SEC}s後に再試行")
            time.sleep(RETRY_SEC)

def main():
    if sys.platform != "darwin":
        log("[warn] この実装は macOS 専用(afplay)。他OSは別手段が必要です。")
    wf = open_wav(AUDIO_FILE)
    fr, ch, sw = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
    ser = open_serial_forever()

    while True:
        try:
            msg = ser.readline().decode(ENCODING, errors="ignore").strip()
            if not msg:
                continue
            log(f"[recv] {msg}")
            if msg.lower() == "close":
                log("[play] start 1s")
                try:
                    pcm = read_exact_1s(wf, SEG_SEC)
                    play_blocking_macos(pcm, ch, sw, fr)
                    log("[play] done 1s")
                except Exception:
                    log("[err] 再生中に例外発生:")
                    traceback.print_exc()

        except serial.SerialException as e:
            log(f"[warn] シリアル通信エラー: {e} -> 再接続します")
            try: ser.close()
            except Exception: pass
            ser = open_serial_forever()
        except KeyboardInterrupt:
            log("[info] 停止要求。終了します。")
            try: ser.close()
            except Exception: pass
            wf.close()
            break
        except Exception:
            log("[err] 予期しない例外（継続）:")
            traceback.print_exc()
            time.sleep(0.2)

if __name__ == "__main__":
    main()
