import serial
import time
import wave
import simpleaudio as sa
from pathlib import Path

# ==== 設定 ====
PORT = "/dev/tty.usbmodem2101"  # 例: mac:/dev/tty.usbmodem1101 / Linux:/dev/ttyACM0 / Windows: COM3
BAUDRATE = 9600
AUDIO_FILE = "audio.wav"
SEG_SEC = 1  # 1トリガーで再生する長さ[秒]

def load_wav_info(path: str):
    if not Path(path).exists():
        raise FileNotFoundError(f"{path} が見つかりません。")
    wf = wave.open(path, 'rb')  # 以降、フレームを必要に応じて読み出す
    nchannels = wf.getnchannels()
    sampwidth = wf.getsampwidth()
    framerate = wf.getframerate()
    nframes = wf.getnframes()
    seg_frames = framerate * SEG_SEC
    return wf, nchannels, sampwidth, framerate, nframes, seg_frames

def read_segment_looped(wf: wave.Wave_read, start_frame: int, seg_frames: int) -> bytes:
    """
    WAVを start_frame から seg_frames 分、末尾を超えたら先頭に戻って連結して返す
    """
    nframes_total = wf.getnframes()
    start = start_frame % nframes_total
    end = start + seg_frames

    # 現在の読み位置を start にして読み出し
    wf.setpos(start)
    if end <= nframes_total:
        data = wf.readframes(seg_frames)
        return data
    else:
        # 末尾まで + 先頭から不足分
        tail = wf.readframes(nframes_total - start)
        wf.rewind()
        head = wf.readframes(end - nframes_total)
        return tail + head

def main():
    wf, nchannels, sampwidth, framerate, nframes, seg_frames = load_wav_info(AUDIO_FILE)
    cursor = 0

    print(f"WAV info: {nchannels}ch, {sampwidth*8}bit, {framerate}Hz, {nframes}frames")
    print("準備OK: Arduinoから1行受信するたびに、audio.wavを1秒ずつ順送りで再生します。")

    ser = serial.Serial(PORT, BAUDRATE, timeout=0.1)
    time.sleep(2)  # Arduinoの自動リセット待ち

    try:
        while True:
            line = ser.readline().decode(errors="ignore").strip()
            if line:
                # 1秒分のフレームを切り出し（末尾をまたぐ場合は連結）
                pcm = read_segment_looped(wf, cursor, seg_frames)
                # simpleaudioで再生
                play_obj = sa.play_buffer(
                    pcm,
                    num_channels=nchannels,
                    bytes_per_sample=sampwidth,
                    sample_rate=framerate
                )
                play_obj.wait_done()  # 毎回きっちり1秒再生
                cursor = (cursor + seg_frames) % nframes
    except KeyboardInterrupt:
        print("\n終了します。")
    finally:
        ser.close()
        wf.close()

if __name__ == "__main__":
    main()
