import warnings

warnings.filterwarnings("ignore", category=UserWarning, module="sklearn")
warnings.filterwarnings(
    "ignore",
    message=r"`sklearn\.utils\.parallel\.delayed` should be used with `sklearn\.utils\.parallel\.Parallel`.*",
    category=UserWarning,
)

import argparse
import json
import os
import pickle
import queue
import serial
import subprocess
import sys
import threading
import time
import numpy as np
import requests

NUM_LANDMARKS = 21
CLEAR_CMD = "CLEAR_BUFFER"

# --- Ollama Engine Configuration ---
OLLAMA_MODEL = "sanchaar-mitra"
OLLAMA_URL = "http://localhost:11434/api/generate"

# --- TTS Background Thread Configuration ---
tts_queue = queue.Queue()
SPEAK_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "speak_heera1.ps1"
)


def estimate_tts_timeout_seconds(text: str) -> int:
    words = len(text.split())
    chars = len(text)
    return max(30, min(300, 12 + words * 2 + chars // 8))


def tts_worker():
    """Background thread: speaks words using Heera OneCore voice via PowerShell."""
    while True:
        word = tts_queue.get()
        if word is None:
            break
        normalized = str(word).strip()
        if not normalized:
            tts_queue.task_done()
            continue
        try:
            timeout_seconds = estimate_tts_timeout_seconds(normalized)
            subprocess.run(
                [
                    "powershell",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    SPEAK_SCRIPT,
                    "-text",
                    normalized,
                ],
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=timeout_seconds,
            )
        except Exception as e:
            print(f"  [TTS error] {e}", flush=True)
        tts_queue.task_done()


# Start the background TTS thread
tts_thread = threading.Thread(target=tts_worker, daemon=True)
tts_thread.start()


# --- Ollama Background Thread Configuration ---
ollama_queue = queue.Queue()


def ollama_worker():
    """Background thread: Sends sentence buffers to local Ollama and handles output piping."""
    while True:
        text = ollama_queue.get()
        if text is None:
            break

        try:
            print(
                f"\n  [Ollama] Processing buffer: \"{text}\"\n  [Ollama] Thinking...",
                flush=True,
            )
            response = requests.post(
                OLLAMA_URL,
                json={
                    "model": OLLAMA_MODEL,
                    "prompt": text,
                    "stream": True,
                },
                timeout=60,
                stream=True,
            )
            response.raise_for_status()

            chunks = []
            print("  [Ollama] Response: ", end="", flush=True)

            # Process incoming tokens on the fly
            for line in response.iter_lines():
                if line:
                    try:
                        token_data = json.loads(line)
                        token = token_data.get("response", "")
                        if token:
                            chunks.append(token)
                            print(token, end="", flush=True)
                        if token_data.get("done", False):
                            break
                    except Exception:
                        pass
            print(flush=True)

            ai_reply = "".join(chunks).strip()
            if ai_reply:
                # Pipe the generated text into the TTS audio engine queue
                print("  [TTS] Dispatching Ollama response to voice...", flush=True)
                tts_queue.put(ai_reply)
            else:
                print("  [Ollama] Received empty response context.", flush=True)

        except Exception as e:
            print(f"  [Ollama connection error] {e}", flush=True)

        ollama_queue.task_done()


# Start the background Ollama processing thread
ollama_thread = threading.Thread(target=ollama_worker, daemon=True)
ollama_thread.start()


def parse_landmarks(line: str):
    """Parse UART line with 'LM:' payload into 42 xy values."""
    if "LM:" not in line:
        return None

    raw = line.split("LM:", 1)[1]
    values = [v.strip() for v in raw.split(",") if v.strip()]
    if len(values) != 63:
        return None

    try:
        coords = [float(v) for v in values]
    except ValueError:
        return None

    xy = []
    for i in range(NUM_LANDMARKS):
        xy.append(coords[i * 3])
        xy.append(coords[i * 3 + 1])
    return xy


def normalize_xy(xy_42):
    """Translate to wrist origin and scale by middle-finger MCP distance."""
    wx, wy = xy_42[0], xy_42[1]

    translated = []
    for i in range(NUM_LANDMARKS):
        translated.append(xy_42[i * 2] - wx)
        translated.append(xy_42[i * 2 + 1] - wy)

    mx, my = translated[9 * 2], translated[9 * 2 + 1]
    scale = max((mx * mx + my * my) ** 0.5, 1e-6)
    return [v / scale for v in translated]


def model_confidence(clf, X):
    if hasattr(clf, "predict_proba"):
        probs = clf.predict_proba(X)[0]
        return float(np.max(probs))
    return 1.0


def main():
    parser = argparse.ArgumentParser(
        description="Responsive Dynamic Real-Time Dual-Hardware ASL Engine."
    )
    parser.add_argument(
        "--port", default="COM3", help="STM32N6 Serial port (default: COM3)"
    )
    parser.add_argument(
        "--uno-port",
        default="COM4",
        help="Arduino UNO Q Serial port (default: COM4)",
    )
    parser.add_argument(
        "--baud", type=int, default=115200, help="Baud rate (default: 115200)"
    )
    parser.add_argument(
        "--model", default="asl_model.pkl", help="Path to pickle model file"
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.20,
        help="Minimum confidence to send prediction (default: 0.20)",
    )
    args = parser.parse_args()

    # Load machine learning model
    with open(args.model, "rb") as f:
        clf = pickle.load(f)
    print(f"Model loaded: {args.model}", flush=True)

    # Establish hardware serial links
    try:
        ser = serial.Serial(args.port, args.baud, timeout=0.01)
        print(f"Connected to STM32N6 on {args.port} at {args.baud}.", flush=True)
    except Exception as e:
        print(f"Error connecting to STM32N6 on {args.port}: {e}", flush=True)
        return

    try:
        uno = serial.Serial(args.uno_port, 115200, timeout=1)
        print(
            f"Connected to Arduino UNO Q on {args.uno_port} at 115200.",
            flush=True,
        )
    except Exception as e:
        print(
            f"Error connecting to Arduino UNO Q on {args.uno_port}: {e}",
            flush=True,
        )
        ser.close()
        return

    # Real-Time Sentence Construction States
    sentence = []
    current_word = []  # Tracks letters of the word currently being built
    last_letter = None
    repeat_count = 0
    space_added = True  # Start True to avoid injecting a space at startup
    was_receiving = False  # Track if we were actively getting valid landmarks

    # Tracking structural clocks
    last_valid_frame_time = time.time()
    SPACE_TIMEOUT = (
        1.2  # Must be zero hand data for 1 full second to type a space
    )

    CONFIRM_FRAMES = 2

    print(
        "\n>>> System Active. Start signing to form a sentence... <<<\n",
        flush=True,
    )

    try:
        while True:
            # Read raw lines arriving from serial buffer interface link
            line = ser.readline().decode("utf-8", errors="replace").strip()

            if line:
                xy = parse_landmarks(line)

                if xy is not None:
                    # Update active status flags because we got clear valid coordinates
                    last_valid_frame_time = time.time()
                    was_receiving = True
                    space_added = False

                    norm = normalize_xy(xy)
                    X = np.array(norm).reshape(1, -1)
                    prediction = str(clf.predict(X)[0]).upper()
                    confidence = model_confidence(clf, X)

                    print(f"  >> {prediction} ({confidence:.0%})", end="", flush=True)

                    if confidence >= args.min_confidence:
                        if prediction == last_letter:
                            repeat_count += 1
                        else:
                            repeat_count = 1
                            last_letter = prediction

                        if (
                            repeat_count > 0
                            and repeat_count % CONFIRM_FRAMES == 0
                        ):
                            if prediction == "SEND":
                                buf_text = "".join(sentence).strip()
                                if buf_text:
                                    print(
                                        f"\n\n[SEND VALIDATED] Transmitting to Uno Q: \"{buf_text}\"",
                                        flush=True,
                                    )
                                    uno.write((buf_text + "\n").encode("utf-8"))

                                    # TTS Integration: Queue any remaining letters
                                    if current_word:
                                        current_word.clear()

                                    # Push the complete accumulated text to the Ollama pipeline thread
                                    ollama_queue.put(buf_text)
                                else:
                                    print(
                                        "\n\n[SEND ABORTED] Buffer empty, nothing to execute.",
                                        flush=True,
                                    )

                                sentence.clear()
                                current_word.clear()
                                last_letter = None
                                repeat_count = 0
                                space_added = True
                            else:
                                sentence.append(prediction)
                                current_word.append(prediction)
                                word_state = "".join(sentence)

                                # Send character streams out to hardware instantly
                                ser.write(prediction.encode("utf-8"))
                                uno.write((prediction + "\n").encode("utf-8"))

                                print(
                                    f" | Verified: {prediction} | Buffer: '{word_state}'",
                                    flush=True,
                                )
                        else:
                            print(
                                f" ({repeat_count % CONFIRM_FRAMES}/{CONFIRM_FRAMES})",
                                flush=True,
                            )
                    else:
                        last_letter = None
                        repeat_count = 1
                        print(" (low conf, skip)", flush=True)
            else:
                # --- READLINE TIMEOUT HANDLING (NO SERIAL DATA ARRIVED) ---
                if was_receiving:
                    if not space_added and (
                        time.time() - last_valid_frame_time > SPACE_TIMEOUT
                    ):
                        if sentence and sentence[-1] != " ":
                            sentence.append(" ")
                            space_added = True
                            last_letter = None
                            repeat_count = 0

                            # TTS Integration: Speak completed individual word during manual typing pauses
                            if current_word:
                                finished_word = "".join(current_word)
                                tts_queue.put(finished_word)
                                print(f"  [TTS] Speaking: {finished_word}", flush=True)
                                current_word.clear()

                            word_state = "".join(sentence)
                            print(
                                f"\n[Inactivity Pause] Space added | Current Sentence: '{word_state}'\n",
                                flush=True,
                            )
                        was_receiving = False

    except KeyboardInterrupt:
        print("\nStopping engine...", flush=True)
    finally:
        # Graceful shutdown markers
        tts_queue.put(None)
        ollama_queue.put(None)
        if ser.is_open:
            ser.close()
        if uno.is_open:
            uno.close()


if __name__ == "__main__":
    main()