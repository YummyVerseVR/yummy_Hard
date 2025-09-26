# -*- coding: utf-8 -*-
"""
QRで user_id を取得 → APIから audio と param(chewiness/firmness) を取得
param をテーブルで up hold down d5 d6 に変換 → Arduinoへ (1行のみ送信)
Arduinoが "close" を送ってきたら audio.wav の続きを1秒だけ再生（macOS: afplay）

プロトコル（このArduinoコードに合わせた最終形）:
PC → Arduino:
  <up,hold,down,d5,d6>\n     # 例: "50,100,33,55,50\n" （カンマ区切り / 改行で確定）
Arduino → PC
  任意ログ（ACK不要）/ 将来 "close" を送るならPCで1秒再生
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
from typing import Optional, Dict, Any, List

# ========= 設定 =========
API_ENDPOINT = "http://upiscium.f5.si:8001"
DB_BASE = API_ENDPOINT  # /{user_id}/param
PORT = "/dev/cu.usbmodem101"   # ←環境に合わせて
BAUDRATE = 115200
SER_TIMEOUT = 1
ENCODING = "utf-8"

AUDIO_FILE = "audio.wav"
SEG_SEC = 0.5

CAM_INDEX = 0
CAM_POLL_DELAY = 0.01
# ========================

def log(msg: str):
    print(time.strftime("[%H:%M:%S]"), msg, flush=True)

# ---- ParamGetter ----
class ParamGetter:
    def __init__(self, db_base: str):
        self.db_base = db_base.rstrip("/")

    def get_param(self, user_id: str) -> Dict[str, Any]:
        url = f"{self.db_base}/{user_id}/param"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.json()

# ---- マッピング ----
FIRMNESS_TO_DUTY = {
    10: (80, 60), 9: (75, 58), 8: (70, 56), 7: (65, 54), 6: (60, 52),
    5: (55, 50), 4: (50, 48), 3: (45, 46), 2: (40, 44), 1: (40, 42)
}
CHEWINESS_TO_SEQ = {
    10: (150, 300, 100), 9: (136, 273, 91), 8: (120, 240, 80), 7: (107, 214, 71),
    6: (88, 176, 59), 5: (75, 150, 50), 4: (60, 120, 40), 3: (50, 100, 33),
    2: (30, 60, 20), 1: (15, 30, 10)
}

def clamp10(x: Any) -> int:
    try:
        xi = int(x)
    except Exception:
        return 5
    return 1 if xi < 1 else 10 if xi > 10 else xi

def compose_ctrl_line(chewiness: int, firmness: int) -> str:
    up, hold, down = CHEWINESS_TO_SEQ[int(chewiness)]
    d5, d6 = FIRMNESS_TO_DUTY[int(firmness)]
    # カンマ区切り 5 値（ArduinoはparseInt/Floatなので","でも" "でもOK）
    return f"{up},{hold},{down},{d5},{d6}"

# ---- 再生 ----
def play_blocking_macos(pcm: bytes, channels: int, sampwidth: int, framerate: int):
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
    return wave.open(str(p), "rb")

def read_exact_sec(wf: wave.Wave_read, sec: float) -> bytes:
    fr, ch, sw = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
    need = int(fr * sec)
    chunks: List[bytes] = []
    remain = need
    while remain > 0:
        frames = wf.readframes(remain)
        if not frames:
            wf.rewind()
            continue
        chunks.append(frames)
        got = len(frames) // (ch * sw)
        remain -= got
    return b"".join(chunks)

# ---- 共有状態 ----
class SharedAudioState:
    def __init__(self, audio_path: str):
        self.audio_path = audio_path
        self.reload_event = threading.Event()
        self.lock = threading.Lock()
        self.latest_user_id: Optional[str] = None
        self.latest_param: Optional[Dict[str, Any]] = None
        self.pending_ctrl_line: Optional[str] = None

    def signal_reload(self):
        with self.lock:
            self.reload_event.set()

    def consume_reload(self) -> bool:
        with self.lock:
            if self.reload_event.is_set():
                self.reload_event.clear()
                return True
            return False

    def set_param(self, user_id: str, param: Dict[str, Any]):
        with self.lock:
            self.latest_user_id = user_id
            self.latest_param = param

    def get_param_snapshot(self):
        with self.lock:
            uid = self.latest_user_id
            par = dict(self.latest_param) if isinstance(self.latest_param, dict) else self.latest_param
            return uid, par

    def set_ctrl_line(self, line: str):
        with self.lock:
            self.pending_ctrl_line = line

    def pop_ctrl_line(self) -> Optional[str]:
        with self.lock:
            line = self.pending_ctrl_line
            self.pending_ctrl_line = None
            return line

# ---- QR安全ラッパー ----
def safe_iter_qr_strings(qr: cv2.QRCodeDetector, frame) -> List[str]:
    results: List[str] = []
    try:
        ok, decoded_info, points, _ = qr.detectAndDecodeMulti(frame)
        if ok and points is not None:
            for s, pts in zip(decoded_info, points):
                if not s or pts is None:
                    continue
                try:
                    import numpy as np
                    area = cv2.contourArea(np.asarray(pts, dtype="float32"))
                    if area > 1.0:
                        results.append(s)
                except Exception:
                    continue
    except cv2.error:
        pass
    if results:
        return results
    try:
        s, pts, _ = qr.detectAndDecode(frame)
        if s and pts is not None:
            try:
                import numpy as np
                area = cv2.contourArea(np.asarray(pts, dtype="float32"))
                if area > 1.0:
                    return [s]
            except Exception:
                return []
    except cv2.error:
        return []
    return []

# ---- スレッド: QR + ダウンロード + パラメータ取得/送信予約 ----
def qr_download_thread(shared: SharedAudioState, stop_event: threading.Event):
    qr = cv2.QRCodeDetector()
    cap = cv2.VideoCapture(CAM_INDEX)
    if not cap.isOpened():
        log("[err] Webカメラを開けませんでした")
        return
    log("Web camera activated")
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass

    last_id = None
    getter = ParamGetter(DB_BASE)
    start_t = time.time()

    try:
        while not stop_event.is_set():
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            if time.time() - start_t < 0.3:
                time.sleep(CAM_POLL_DELAY)
                continue

            decoded_list = safe_iter_qr_strings(qr, frame)
            if decoded_list:
                user_id = decoded_list[0]
                if user_id != last_id:
                    log(f"qr detected: {user_id}")
                    # 1) 音声DL
                    try:
                        url = f"{API_ENDPOINT}/{user_id}/audio"
                        r = requests.get(url, timeout=10)
                        r.raise_for_status()
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
                            tmp.write(r.content)
                            tmp_name = tmp.name
                        os.replace(tmp_name, AUDIO_FILE)
                        log("Audio file downloaded & replaced -> audio.wav")
                        shared.signal_reload()
                    except Exception as e:
                        log(f"[err] ダウンロード失敗: {e}")

                    # 2) パラメ取得 → 送信キュー
                    try:
                        param = getter.get_param(user_id)
                        shared.set_param(user_id, param)
                        che = clamp10(param.get("chewiness"))
                        fir = clamp10(param.get("firmness"))
                        ctrl_line = compose_ctrl_line(che, fir)
                        print(ctrl_line)                # 見えるログ
                        shared.set_ctrl_line(ctrl_line)  # 送信キュー
                    except Exception as pe:
                        log(f"[warn] パラメ取得/合成失敗: {pe}")

                    last_id = user_id

            time.sleep(CAM_POLL_DELAY)
    finally:
        cap.release()
        cv2.destroyAllWindows()
        log("[camera] 終了")

# ---- スレッド: 受信（任意ログ/"close"対応） & 再生 ----
def continuously_read_from_arduino(ser: serial.Serial, stop_event: threading.Event, shared: SharedAudioState):
    if sys.platform != "darwin":
        log("[warn] この実装は macOS 専用(afplay)。他OSは別手段が必要です。")

    wf = None
    channels = sampwidth = framerate = None

    def ensure_wav_open():
        nonlocal wf, channels, sampwidth, framerate
        if wf is not None:
            try:
                wf.close()
            except Exception:
                pass
            wf = None
        wf = open_wav(AUDIO_FILE)
        framerate = wf.getframerate()
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()

    if Path(AUDIO_FILE).exists():
        try:
            ensure_wav_open()
        except Exception as e:
            log(f"[warn] 初回WAVオープン失敗: {e}")

    while not stop_event.is_set():
        try:
            if ser.in_waiting > 0:
                received = ser.readline().decode(ENCODING, errors="ignore").strip()
                if not received:
                    continue
                print(f"\nArduinoからの応答: {received}")

                # 将来Arduinoが "close" を送るなら 1秒再生
                if received.lower() == "close":
                    if shared.consume_reload() or wf is None:
                        try:
                            ensure_wav_open()
                        except Exception as e:
                            log(f"[err] WAVが開けず再生不可: {e}")
                            continue
                    uid, param = shared.get_param_snapshot()
                    if uid is not None:
                        log(f"[param] using (user_id={uid}): {param}")
                    log("[play] start 1s")
                    try:
                        pcm = read_exact_sec(wf, SEG_SEC)
                        play_blocking_macos(pcm, channels, sampwidth, framerate)
                        log("[play] done 1s")
                    except Exception:
                        log("[err] 再生中に例外発生:")
                        traceback.print_exc()
            else:
                if shared.consume_reload():
                    try:
                        ensure_wav_open()
                    except Exception as e:
                        log(f"[warn] WAVリロード失敗: {e}")
                time.sleep(0.01)
        except serial.SerialException as e:
            log(f"[warn] シリアル受信エラー: {e}")
            time.sleep(0.5)
        except Exception as e:
            log(f"[warn] 受信スレッド例外: {e}")
            time.sleep(0.1)
    log("[receiver] 終了")

# ---- メイン ----
def main():
    stop_event = threading.Event()
    shared = SharedAudioState(AUDIO_FILE)

    # シリアルを開く
    try:
        ser = serial.Serial(PORT, BAUDRATE, timeout=SER_TIMEOUT)
        time.sleep(2)  # UNO R4の自動リセット待ち
    except serial.SerialException:
        print(f"シリアルポート {PORT} に接続できません。ポート名を確認してください。")
        return

    # 受信スレッド開始
    rx_thread = threading.Thread(target=continuously_read_from_arduino, args=(ser, stop_event, shared), daemon=True)
    rx_thread.start()

    # QRスレッド開始
    cam_thread = threading.Thread(target=qr_download_thread, args=(shared, stop_event), daemon=True)
    cam_thread.start()

    log("起動：QR→音声DL & パラメ取得。新しい 5値 が来たら '<up,hold,down,d5,d6>\\n' を1行送信。Ctrl+Cで終了。")

    try:
        while True:
            ctrl_line = shared.pop_ctrl_line()
            if ctrl_line:
                try:
                    # ★ このArduinoコード用：1行だけ送る（[send]なし）
                    ser.write((ctrl_line + "\n").encode(ENCODING))
                    ser.flush()
                    log(f"[send] {ctrl_line}")
                except serial.SerialException as e:
                    log(f"[warn] 送信失敗: {e}")
            time.sleep(0.02)
    except KeyboardInterrupt:
        log("[info] 停止要求。終了します。")
    finally:
        stop_event.set()
        try:
            ser.close()
        except Exception:
            pass
        rx_thread.join(timeout=2.0)
        cam_thread.join(timeout=2.0)
        log("[main] 終了")

if __name__ == "__main__":
    main()
