# QRtoAudio

カメラで読み取った **QRコードの内容** をもとに API にアクセスし、対応する **音声ファイル (audio.wav)** を自動で取得・保存するアプリケーションです。

## 📦 プロジェクト概要
このプロジェクトは FastAPI・OpenCV・Requests を利用して構築されています。  
QRコードをリアルタイムで検出し、そのデータを API に送信して音声を取得します。  

- **プロジェクト名:** `yummy`  
- **Python バージョン:** `>=3.13`  
- **主要ライブラリ:**  
  - FastAPI  
  - OpenCV (opencv-python)  
  - Requests  

## 🚀 セットアップ方法

1. **リポジトリをクローン**
   ```bash
   git clone <your-repo-url>
   cd yummy
   ```

2. **仮想環境を作成して有効化**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate   # Windows の場合は .venv\Scripts\activate
   ```

3. **依存関係をインストール**
   ```bash
   pip install -r requirements.txt
   ```
   または Poetry/uv を使う場合:
   ```bash
   uv pip install -r pyproject.toml
   ```

## 🖥️ 使い方

1. カメラを接続する。
2. メインスクリプトを実行:
   ```bash
   python QRtoAudio.py
   ```
3. カメラ映像に QRコードをかざすと、自動で内容が読み取られます。
4. 読み取られた内容を元に `http://upiscium.f5.si:8001/<QR内容>/audio` へリクエストを送信し、音声ファイル `audio.wav` が保存されます。
5. 保存完了後、コンソールに `"Audio file downloaded"` と表示されます。

## 📂 ファイル構成
```
.
├── pyproject.toml     # プロジェクト設定と依存関係
├── QRtoAudio.py       # QRコード読み取り & 音声ダウンロード処理
└── README.md          # 本ドキュメント
```

## ⚠️ 注意点
- デフォルトでは PC 内蔵カメラ (`cv2.VideoCapture(0)`) を使用します。外部カメラを使う場合は引数を変更してください。
- API のエンドポイントはソースコード内で固定されています。変更する場合は `QRtoAudio.py` 内の `API_ENDPOINT` を編集してください。
- 音声ファイルは常に上書き保存されます (`audio.wav`)。