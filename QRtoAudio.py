import cv2
import requests

API_ENDPOINT = "http://upiscium.f5.si:8001"

# QRコード検出器を初期化
qrCodeDetector = cv2.QRCodeDetector()

cap = cv2.VideoCapture(0)

print("Web camera activated")

while True:
    ret, frame = cap.read()
    if not ret:
        break

    # QRコードを検出し、その位置を取得
    decodedText, points, _ = qrCodeDetector.detectAndDecode(frame)

    if points is not None:
        points = points[0]
        if len(decodedText) == 0 :
            continue
        print("qr detected:"+decodedText)
        res = requests.get(f"{API_ENDPOINT}/{decodedText}/audio")
        with open("audio.wav", "wb") as f:
            f.write(res.content)
        print("Audio file downloaded")
        

cap.release()
cv2.destroyAllWindows()

