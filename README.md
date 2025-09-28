## getall使い方

Python スクリプトで、**QRから user_id を取得 → API から音声とパラメータ取得 → 無音トリム → Arduinoへ 5値送信 → Arduino の `open/close` 間隔の移動平均だけ音声を再生**します。

---

## 機能概要

* **QR読み取り**（OpenCV）：カメラ映像から QR を検出し、最初に見つかった `user_id` を取得。
* **音声ダウンロード**：`GET {API_ENDPOINT}/{user_id}/audio` を叩いて `audio.wav` として保存。
* **無音トリム**（FFmpeg）：前後（オプションで途中）無音を除去 → `trimmed.wav` を生成。
* **パラメータ取得**：`GET {DB_BASE}/{user_id}/param` を叩いて `chewiness` / `firmness`（1–10）を取得。
* **5値合成 & 送信**：

  * 弾力→`up,hold,down`（ミリ秒）
  * 硬度→`d5,d6`（Duty[%]）
  * フォーマット：`<up,hold,down,d5,d6>\n` を **1行だけ** Arduino へ送信。
* **再生制御**（sounddevice）：Arduino からの `open` と `close` の**直近3回**の間隔の**移動平均(秒)**だけ、`trimmed.wav` の続きから再生。

> Arduino 側は起動直後の合図として `new\n` を受け取ります（任意）。

---

## 依存関係

* Python 3.10+ 推奨
* パッケージ

  * `opencv-python`
  * `requests`
  * `pyserial`
  * `numpy`
  * `sounddevice`（PortAudio バインディング）
* 外部ツール

  * **FFmpeg**（無音トリムに使用）

### インストール例

```bash
# venv 推奨
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install opencv-python requests pyserial numpy sounddevice

# FFmpeg
# macOS:   brew install ffmpeg
# Windows: winget install --id=Gyan.FFmpeg  # または choco install ffmpeg
# Ubuntu:  sudo apt update && sudo apt install -y ffmpeg libportaudio2
```

> Linux では `sounddevice` 用に `libportaudio2` が必要です。

---

## 使い方（クイックスタート）

1. **配線/接続**：Arduino を PC に接続（シリアルポート名を確認）。
2. **スクリプト設定**：ファイル先頭の設定値を環境に合わせて修正：

   * `API_ENDPOINT` / `DB_BASE`
   * `PORT`（例：mac`/dev/cu.usbmodem1101`、Linux`/dev/ttyACM0`、Win`COM3`）
   * `BAUDRATE`（Arduino 側と一致）
   * カメラ `CAM_INDEX`（デフォルト 0）
   * トリム閾値 `TRIM_THRESHOLD_DB` / `TRIM_MIN_SIL_MS` / `TRIM_REMOVE_MID`
3. **実行**：

   ```bash
   python your_script.py
   ```
4. **QR をかざす**：コンソールに `qr detected: <user_id>` が出て、音声DL→トリム→5値送信→`open/close` に応じて再生。

---

## シリアル・プロトコル

* **PC → Arduino**

  * 起動時：`new\n`（任意）
  * パラメ更新：`<up,hold,down,d5,d6>\n`

    * 例：`50,100,33,55,50\n`
* **Arduino → PC**

  * 任意ログ行
  * `open` / `close`（小文字推奨）

> このスクリプトは `[send]` などの前置きコマンドを**使いません**。1行のみ送ります。

---

## マッピング（既定）

* **硬度 → D5/D6 Duty[%]**

```
10:(80,60), 9:(75,58), 8:(70,56), 7:(65,54), 6:(60,52),
 5:(55,50), 4:(50,48), 3:(45,46), 2:(40,44), 1:(40,42)
```

* **弾力 → up/hold/down [ms]**

```
10:(150,300,100), 9:(136,273,91), 8:(120,240,80), 7:(107,214,71),
 6:(88,176,59), 5:(75,150,50), 4:(60,120,40), 3:(50,100,33),
 2:(30,60,20), 1:(15,30,10)
```

> `chewiness` / `firmness` の値は 1–10 にクランプされます。

---

## 無音トリム（FFmpeg）の調整

* 設定項目：

  * `TRIM_THRESHOLD_DB`（デフォルト `-50 dB`）
  * `TRIM_MIN_SIL_MS`（デフォルト `600 ms`）
  * `TRIM_REMOVE_MID`（デフォルト `False`）
* 指針：

  * **切れすぎる** → `TRIM_THRESHOLD_DB` を **小さく**（例 `-55` / `-60`）、または `TRIM_MIN_SIL_MS` を **大きく**（例 `700–1000`）。
  * **切れが甘い** → `TRIM_THRESHOLD_DB` を **大きく**（例 `-45`）、または `TRIM_MIN_SIL_MS` を **小さく**（例 `300–400`）。
  * 途中の無音も切りたい → `TRIM_REMOVE_MID = True`（※切れすぎ注意）。

FFmpeg が無い場合は自動で生 WAV をコピーして使用します。

---

## 再生ロジック（sounddevice）

* `open` を受信した時刻を記録。
* 次の `close` を受信したら、その区間を **recent_intervals（最大3件）** に追加。
* 平均値 `avg_sec` を算出し、`[MIN_SEC=0.05, MAX_SEC=5.0]` でクリップ。
* `trimmed.wav` から **ちょうど `avg_sec` 秒分** の PCM を読み出して **同期再生**。
* 連続再生の位置は WAV の読み進みで管理。EOF に達したら自動で **巻き戻し**。

---

## ログ例

```
[12:34:56] Web camera activated
qr detected: 5bda669c-...
Audio file downloaded -> audio.wav
Trim success -> trimmed.wav
50,100,33,55,50
[send] 50,100,33,55,50
Arduinoからの応答: open
[event] open
Arduinoからの応答: close
[event] close
[dur] intervals=[0.62, 0.58, 0.60] -> avg=0.600s
[param] using (user_id=...): {"chewiness":3, "firmness":5}
[play] start 0.600s
[play] done
```

---

## よくあるエラーと対処

* **`[err] Webカメラを開けませんでした`**

  * `CAM_INDEX` を変更 / 別アプリが占有していないか確認。
* **シリアル `Resource busy` / `could not open port`**

  * Arduino IDE のシリアルモニタを閉じる。ポート名が正しいか確認。
  * macOS で複数候補がある場合は `ls /dev/cu.usb*` で確認。
* **`ffmpeg が見つかりません`**

  * FFmpeg をインストール。PATH を通す。
* **`ModuleNotFoundError`**

  * 必要パッケージを `pip install`。
* **音が出ない / ガリガリする**

  * デバイスの既定出力を確認。`sounddevice` のブロッキング再生なので、他アプリの独占に注意。
  * Linux は `libportaudio2` 必須。
* **`open` が来ない**

  * Arduino 側の送信ロジック・ボーレート・小文字表記を確認。
* **切れすぎ**

  * `TRIM_THRESHOLD_DB` を下げる（例 `-60`）、`TRIM_MIN_SIL_MS` を上げる（例 `800`）。

---

## 設定項目まとめ

```python
API_ENDPOINT = "http://upiscium.f5.si:8001"
DB_BASE      = API_ENDPOINT
PORT         = "/dev/cu.usbmodem1101"  # Win: "COM3", Linux: "/dev/ttyACM0"
BAUDRATE     = 115200
SER_TIMEOUT  = 1
AUDIO_RAW    = "audio.wav"
AUDIO_TRIMMED= "trimmed.wav"
TRIM_THRESHOLD_DB = -50.0
TRIM_MIN_SIL_MS   = 600
TRIM_REMOVE_MID   = False
CAM_INDEX    = 0
```

---

## テスト（Arduino なし）

* **擬似シリアル**は同梱していませんが、Arduino 側の簡易スケッチ（交互に `open` / `close` を送る）で検証可能です。
* 既にある「交互送信スケッチ」を 9600/115200 など本スクリプトと一致させて利用してください。

---

## 安全上の注意

* D5/D6 は EMS/駆動回路に接続される前提です。**人体に印加する場合は必ず安全基準**（電圧電流・周波数・通電時間・同意/倫理）を遵守してください。
* 大音量での再生に注意。ヘッドホン使用時は音量を最小から確認。

---

## ライセンス

* プロジェクトのライセンス方針に合わせて追記してください（例：MIT）。

---

## 変更履歴（この版での主な変更）

* `simpleaudio` → **`sounddevice + numpy`** に移行。
* WAV 読み出しをサンプル幅別に安全化（8/16/24/32-bit 対応）。
* FFmpeg 無音トリムの閾値・期間を**切れすぎ防止寄り**に初期化。
* `open/close` の**移動平均(3)** による秒数決定と安全クリップ（0.05–5.0s）。
* `new\n` を起動時に送信（任意・ログ用）。
