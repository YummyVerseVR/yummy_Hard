# -*- coding: utf-8 -*-
"""
audio.wav の無音を「切りすぎない」よう保守的にトリムして trimmed.wav を生成。
- デフォルトは前後のみ（途中の無音は保持）
- 閾値や最小長さは引数で調整可
- Python 3.13対応（pydub不要、ffmpegバイナリ使用）

使い方:
  uv run python3 trim_silence_ffmpeg_conservative.py
  # もっと緩く/厳しく:
  uv run python3 trim_silence_ffmpeg_conservative.py -t -48 -m 700
  # 途中の無音も削除したい場合（慎重に!）:
  uv run python3 trim_silence_ffmpeg_conservative.py --remove-mid
"""

import subprocess
from pathlib import Path
import sys
import shutil
import argparse

INPUT_FILE = "audio.wav"
OUTPUT_FILE = "trimmed.wav"

def build_filter(th_db: float, min_ms: int, remove_mid: bool) -> str:
    """
    th_db: 無音閾値[dB]（例: -50）
    min_ms: 無音とみなす最小継続[ms]（例: 600）
    remove_mid: 途中の無音も消すなら True（デフォルトは False = 前後のみ）
    """
    sec = min_ms / 1000.0

    if not remove_mid:
        # 前後のみトリム（途中は保持）
        # silenceremoveは基本的に先頭/末尾の無音に作用。1つで両端に効く。
        return (
            f"silenceremove="
            f"start_periods=1:start_silence={sec}:start_threshold={th_db}dB:"
            f"stop_periods=1:stop_silence={sec}:stop_threshold={th_db}dB"
        )
    else:
        # 途中の長い無音も削除（切れすぎ注意）
        # 一部環境では中間も圧縮され得るため、値は保守的に。
        # 前処理で末尾、後処理でもう一度末尾に保険をかけるチェーン例。
        return (
            f"silenceremove="
            f"start_periods=1:start_silence={sec}:start_threshold={th_db}dB,"
            f"silenceremove="
            f"stop_periods=1:stop_silence={sec}:stop_threshold={th_db}dB"
        )

def run_ffmpeg(in_path: Path, out_path: Path, af: str) -> None:
    cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "warning",
        "-i", str(in_path),
        "-af", af,
        "-c:a", "pcm_s16le",
        "-y", str(out_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr or res.stdout)

def main():
    if shutil.which("ffmpeg") is None:
        print("❌ ffmpeg が見つかりません。macOSなら `brew install ffmpeg` を実行してください。")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="WAVの無音を保守的にトリム")
    parser.add_argument("-t", "--threshold", type=float, default=-50.0,
                        help="無音閾値[dB]（小さく=ゆるい, 例: -50）")
    parser.add_argument("-m", "--min-ms", type=int, default=600,
                        help="無音とみなす最小継続[ms]（大きく=ゆるい, 例: 600）")
    parser.add_argument("--remove-mid", action="store_true",
                        help="途中の無音も削除（切れすぎ注意、通常は使わない）")
    args = parser.parse_args()

    in_path = Path(INPUT_FILE)
    if not in_path.exists():
        print(f"❌ 入力が見つかりません: {in_path.resolve()}")
        sys.exit(1)

    af = build_filter(args.threshold, args.min_ms, args.remove_mid)

    try:
        print(f"▶️ 無音トリム実行: {in_path.name} -> {OUTPUT_FILE}")
        print(f"   閾値 {args.threshold} dB, 最小無音 {args.min_ms} ms, remove_mid={args.remove_mid}")
        run_ffmpeg(in_path, Path(OUTPUT_FILE), af)
        print(f"✅ 完了: {OUTPUT_FILE}")
    except Exception as e:
        print("❌ 失敗:", e)
        sys.exit(2)

if __name__ == "__main__":
    main()
