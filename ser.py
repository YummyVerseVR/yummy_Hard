import serial
import threading
import time

def continuously_read_from_arduino(ser, stop_event):
    while not stop_event.is_set():
        if ser.in_waiting > 0:
            try:
                received = ser.readline().decode('utf-8').strip()
                print(f"\nArduinoからの応答: {received}")
            except UnicodeDecodeError:
                print("\nデコードエラーが発生しました")

def main():
    port = '/dev/cu.usbmodem2101'  # ご自身の環境に合わせて変更, Windowsの場合は'COM3'等のみでOK
    baudrate = 115200  # 使用したいバンドを指定

    try:
        ser = serial.Serial(port, baudrate, timeout=1)
        time.sleep(2)  # Arduinoのリセットを待つ
    except serial.SerialException:
        print(f"シリアルポート {port} に接続できません。ポート名を確認してください。")
        return

    stop_event = threading.Event()
    read_thread = threading.Thread(target=continuously_read_from_arduino, args=(ser, stop_event))
    read_thread.daemon = True
    read_thread.start()

    try:
        while True:
            input_data = input("送信データ ('exit'で終了): ")

            if input_data.lower() == 'exit':
                print("終了します。")
                stop_event.set()
                break

            ser.write((input_data + '\n').encode('utf-8'))  # 改行を明示的に送る
            time.sleep(0.1)

    finally:
        ser.close()

if __name__ == '__main__':
    main()
