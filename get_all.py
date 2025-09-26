# -*- coding: utf-8 -*-
"""
QRで user_id を取得 → APIから audio と param(chewiness/firmness) を取得
param をテーブルで up hold down d5 d6 に変換 → Arduinoへ (1行のみ送信)
Arduinoが "open" と "close" を送ってくる想定で、
直近3回分の open→close 間隔（秒）の移動平均だけ音声を再生（sounddevice）。
音声は audio.wav を ffmpeg で保守的トリムした trimmed.wav を再生する。

プロトコル（このArduinoコードに合わせた最終形）:
PC → Arduino:
  <up,hold,down,d5,d6>\n     # 例: "50,100,33,55,50\n" （カンマ区切り / 改行で確定）
Arduino → PC:
  任意ログ / "open" / "close"
"""

import os
import time
import wave
import cv2
import requests
import serial
import traceback
import tempfile
import threading
import subprocess
import shutil
from pathlib import Path
from typing import Optional, Dict, Any, List
from collections import deque

# ===== 変更：simpleaudio を廃止し、sounddevice + numpy を使用 =====
import numpy as np
import sounddevice as sd

# ========= 設定 =========
API_ENDPOINT = "http://upiscium.f5.si:8001"
DB_BASE = API_ENDPOINT  # /{user_id}/param
PORT = "/dev/cu.usbmodem1101"   # ←環境に合わせて（Win: "COM3" 等, Linux: "/dev/ttyACM0" 等）
BAUDRATE = 115200
SER_TIMEOUT = 1
ENCODING = "utf-8"

AUDIO_RAW = "audio.wav"       # ダウンロード保存先（生）
AUDIO_TRIMMED = "trimmed.wav" # 再生に使うファイル（トリム後）

# —— 無音トリムのデフォルト（ゆるめ＝切りすぎ防止）——
TRIM_THRESHOLD_DB = -50.0   # 小さくすると「ゆるい」判定（例: -55, -60）
TRIM_MIN_SIL_MS   = 600     # 大きくすると「ゆるい」判定（例: 700, 800）
TRIM_REMOVE_MID   = False   # 途中の無音も消すなら True（通常は False 推奨）
# ========================

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
    return f"{up},{hold},{down},{d5},{d6}"

# ---- 再生（sounddevice 版）----
def _bytes_to_numpy(pcm: bytes, channels: int, sampwidth: int):
    """PCMバイト列を sounddevice で再生できる numpy 配列へ変換"""
    if sampwidth == 2:
        # 16-bit signed
        arr = np.frombuffer(pcm, dtype=np.int16)
        if channels > 1:
            arr = arr.reshape(-1, channels)
        return arr, None  # dtypeそのまま再生
    elif sampwidth == 1:
        # 8-bit unsigned/signed混在の可能性あり → 安全に float32 正規化
        a = np.frombuffer(pcm, dtype=np.int8).astype(np.float32) / 128.0
        if channels > 1:
            a = a.reshape(-1, channels)
        return a, "float32"
    elif sampwidth == 3:
        # 24-bit PCM -> int32 に拡張 → float32 正規化
        b = np.frombuffer(pcm, dtype=np.uint8)
        # 3バイト→4バイト展開
        a32 = (b[0::3].astype(np.uint32) |
               (b[1::3].astype(np.uint32) << 8) |
               (b[2::3].astype(np.uint32) << 16))
        # 符号拡張
        sign_mask = 1 << 23
        a32 = a32.astype(np.int32)
        a32 = (a32 ^ sign_mask) - sign_mask
        a = (a32.astype(np.float32) / (2**23))
        if channels > 1:
            a = a.reshape(-1, channels)
        return a, "float32"
    elif sampwidth == 4:
        # 多くは 32-bit float か 32-bit int。WAV エンコーディングによるが、
        # 安全策として int32 として読み、float32 正規化して再生。
        a32 = np.frombuffer(pcm, dtype=np.int32).astype(np.float32) / (2**31)
        if channels > 1:
            a32 = a32.reshape(-1, channels)
        return a32, "float32"
    else:
        raise ValueError(f"Unsupported sample width: {sampwidth} byte/sample")

def play_blocking(pcm: bytes, channels: int, sampwidth: int, framerate: int):
    arr, kind = _bytes_to_numpy(pcm, channels, sampwidth)
    # blocking=True で再生が終わるまで待機（simpleaudio と同等の挙動）
    sd.play(arr, framerate, blocking=True)

def open_wav(path: str) -> wave.Wave_read:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"WAV が見つかりません: {p.resolve()}")
    return wave.open(str(p), "rb")

def read_exact_sec(wf: wave.Wave_read, sec: float) -> bytes:
    fr, ch, sw = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
    need = int(fr * float(sec))
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

# ---- 無音トリム（ffmpeg） ----
def _build_silenceremove(th_db: float, min_ms: int, remove_mid: bool) -> str:
    sec = max(0.0, min_ms / 1000.0)
    if not remove_mid:
        # 前後のみトリム
        return (
            f"silenceremove="
            f"start_periods=1:start_silence={sec}:start_threshold={th_db}dB:"
            f"stop_periods=1:stop_silence={sec}:stop_threshold={th_db}dB"
        )
    else:
        # 途中の長い無音も削除（切れすぎ注意）
        return (
            f"silenceremove="
            f"start_periods=1:start_silence={sec}:start_threshold={th_db}dB,"
            f"silenceremove="
            f"stop_periods=1:stop_silence={sec}:stop_threshold={th_db}dB"
        )

def run_ffmpeg_trim(in_path: Path, out_path: Path,
                    th_db: float = TRIM_THRESHOLD_DB,
                    min_ms: int = TRIM_MIN_SIL_MS,
                    remove_mid: bool = TRIM_REMOVE_MID) -> bool:
    """成功で True。ffmpeg 未導入などで失敗したら False を返す。"""
    if shutil.which("ffmpeg") is None:
        log("❌ ffmpeg が見つかりません（macOS: `brew install ffmpeg` 推奨）。生WAVをそのまま使用します。")
        try:
            shutil.copyfile(in_path, out_path)
            return True
        except Exception as e:
            log(f"[err] 生コピーにも失敗: {e}")
            return False

    af = _build_silenceremove(th_db, min_ms, remove_mid)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-i", str(in_path),
        "-af", af,
        "-c:a", "pcm_s16le",
        "-y", str(out_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        log(f"[err] ffmpeg 失敗: {res.stderr or res.stdout}")
        return False
    return True

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

# ---- スレッド: QR + ダウンロード + 無音トリム + パラメ取得/送信予約 ----
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
                    # 1) 音声DL（生）
                    try:
                        url = f"{API_ENDPOINT}/{user_id}/audio"
                        r = requests.get(url, timeout=10)
                        r.raise_for_status()
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
                            tmp.write(r.content)
                            tmp_name = tmp.name
                        os.replace(tmp_name, AUDIO_RAW)
                        log("Audio file downloaded -> audio.wav")

                        # 2) 無音トリム → trimmed.wav 生成
                        ok_trim = run_ffmpeg_trim(Path(AUDIO_RAW), Path(AUDIO_TRIMMED),
                                                  TRIM_THRESHOLD_DB, TRIM_MIN_SIL_MS, TRIM_REMOVE_MID)
                        if ok_trim:
                            log(f"Trim success -> {AUDIO_TRIMMED}")
                            shared.signal_reload()  # 再生側にリロード通知
                        else:
                            log("[warn] トリム失敗。生WAVをそのまま利用します。")
                            try:
                                shutil.copyfile(AUDIO_RAW, AUDIO_TRIMMED)
                                shared.signal_reload()
                            except Exception as e:
                                log(f"[err] フォールバックコピー失敗: {e}")

                    except Exception as e:
                        log(f"[err] ダウンロード/トリム失敗: {e}")

                    # 3) パラメ取得 → 送信キュー
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

# ---- スレッド: 受信（open/close対応） & 再生（trimmed.wav） ----
def continuously_read_from_arduino(ser: serial.Serial, stop_event: threading.Event, shared: SharedAudioState):
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
        # 再生は常に trimmed.wav を使用
        wf = open_wav(AUDIO_TRIMMED)
        framerate = wf.getframerate()
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()

    # 初期ロード（trimmed.wav が無ければ audio.wav をコピーして作る）
    if not Path(AUDIO_TRIMMED).exists() and Path(AUDIO_RAW).exists():
        try:
            shutil.copyfile(AUDIO_RAW, AUDIO_TRIMMED)
        except Exception as e:
            log(f"[warn] 初期フォールバックコピー失敗: {e}")

    if Path(AUDIO_TRIMMED).exists():
        try:
            ensure_wav_open()
        except Exception as e:
            log(f"[warn] 初回WAVオープン失敗: {e}")

    # --- open→close 間隔の移動平均用 ---
    last_open_ts: Optional[float] = None
    recent_intervals: deque = deque(maxlen=3)
    DEFAULT_FALLBACK_SEC = 0.5  # データが無いときのフォールバック
    MIN_SEC, MAX_SEC = 0.05, 5.0  # 安全のための下限・上限

    while not stop_event.is_set():
        try:
            if ser.in_waiting > 0:
                received = ser.readline().decode(ENCODING, errors="ignore").strip()
                if not received:
                    continue

                # 任意ログ
                print(f"\nArduinoからの応答: {received}")

                # ---- "open" 到着：時刻記録 ----
                if received.lower() == "open":
                    last_open_ts = time.time()
                    log("[event] open")
                    continue

                # ---- "close" 到着：直近区間を記録→移動平均で再生 ----
                if received.lower() == "close":
                    log("[event] close")

                    # WAVのリロード（trimmed.wav の更新に追随）
                    if shared.consume_reload() or wf is None:
                        try:
                            ensure_wav_open()
                        except Exception as e:
                            log(f"[err] WAVが開けず再生不可: {e}")
                            continue

                    # open→close 間隔（秒）を計算
                    now = time.time()
                    if last_open_ts is not None:
                        interval = max(0.0, now - last_open_ts)
                        recent_intervals.append(interval)
                        last_open_ts = None  # 1区間分消費
                    else:
                        log("[warn] 直前に open が無いので区間不明。フォールバックを使用します.")

                    # 移動平均秒数を決定
                    if len(recent_intervals) > 0:
                        avg_sec = sum(recent_intervals) / len(recent_intervals)
                    else:
                        avg_sec = DEFAULT_FALLBACK_SEC
                    # クリップ（安全対策）
                    avg_sec = max(MIN_SEC, min(MAX_SEC, avg_sec))

                    log(f"[dur] intervals={list(map(lambda x: round(x,3), recent_intervals))} -> avg={avg_sec:.3f}s")

                    # 任意ログ（誰のparamで再生しているか）
                    uid, param = shared.get_param_snapshot()
                    if uid is not None:
                        log(f"[param] using (user_id={uid}): {param}")

                    # 再生（trimmed.wav）
                    log(f"[play] start {avg_sec:.3f}s")
                    try:
                        pcm = read_exact_sec(wf, avg_sec)
                        play_blocking(pcm, channels, sampwidth, framerate)
                        log("[play] done")
                    except Exception:
                        log("[err] 再生中に例外発生:")
                        traceback.print_exc()
                    continue

                # （その他の文字列は単なるログとして扱う）
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
    shared = SharedAudioState(AUDIO_TRIMMED)

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

    log("起動：QR→音声DL→無音トリム(trimmed.wav) & パラメ取得。新しい 5値 が来たら '<up,hold,down,d5,d6>\\n' を1行送信。Ctrl+Cで終了。")

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
