import serial
import time

try:
    ser = serial.Serial("COM4", 115200, timeout=1)
    print("Connected to COM3!")
    print("Listening for 10 seconds...")

    start = time.time()
    while time.time() - start < 10:
        line = ser.readline().decode("utf-8", errors="replace").strip()
        if line:
            print(f"Received: {line[:100]}")
        else:
            print(".", end="", flush=True)
        time.sleep(0.1)

    ser.close()
    print("\nDone!")
except Exception as e:
    print(f"Error: {e}")
