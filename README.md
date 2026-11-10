# HandMouse – control your mouse with your hand

Uses your laptop camera to track your hand. Runs on CPU only, Windows.

## 1. Setup (one time)

1. Install **Python 3.10, 3.11 or 3.12** from python.org.
   Tick **"Add Python to PATH"** during install. (Python 3.13 will not work.)
2. Unzip this folder anywhere.
3. Double-click **`run.bat`**.
   The first run downloads packages (a few minutes). After that it starts instantly.

A small preview window opens showing your hand. You are ready.

## 2. Gestures

Hold your hand in front of the camera, palm facing it, about 40-60 cm away.

| What you want | What to do |
|---|---|
| **Move cursor** | Point with your **index finger up**. Other fingers folded, **thumb tucked in**. Move your finger. |
| **Zoom** | Open **thumb + index finger** like an "L". **Spread them apart = zoom in**, **bring them together = zoom out**. Other fingers folded. |
| **Scroll** | Hold up **index + middle finger** together (ring and pinky folded). Move your hand **up = scroll up**, **down = scroll down**. |
| **Pause / Resume** | Make a **closed fist** and hold for about 0.7 seconds. You hear a beep (high = ON, low = PAUSED). |
| **Quit** | Click the preview window and press **Q** or **ESC**. |

The grey box in the preview is your "mouse area". Your fingertip inside that box covers the whole screen, so you do not need to stretch your arm.

## 3. Tips for smooth results

- **Good light** matters most. A dark room makes the webcam drop to a low frame rate and tracking gets jumpy. Light in front of you, not behind.
- Keep a plain background if possible.
- Keep your **thumb tucked** while pointing, otherwise it is read as the zoom gesture.
- Switch gestures with a clear, deliberate change of fingers.
- Use the fist to pause when you want to talk with your hands or type.

## 4. Adjusting the feel

Open **`main.py`** in Notepad. All settings are at the top, labeled, in the section **TUNABLE SETTINGS**. The common ones:

- `ACTIVE_LEFT / RIGHT / TOP / BOTTOM`: smaller box = less hand movement needed.
- `SMOOTH_MIN_CUTOFF`: lower = smoother cursor, higher = more responsive.
- `SMOOTH_BETA`: higher = less lag on fast moves.
- `ZOOM_STEP`: lower = zoom reacts more.
- `SCROLL_GAIN`: higher = faster scrolling.
- `ZOOM_INVERT`, `SCROLL_INVERT`: set to `True` to flip a direction.
- `SHOW_PREVIEW = False`: no window, slightly faster (quit with Ctrl+C in the black window).

Save the file and run `run.bat` again.

## 5. Troubleshooting

- **Camera does not open**: close Zoom/Teams/browser tabs using the camera. In Windows go to Settings > Privacy > Camera and allow desktop apps. Try `CAMERA_INDEX = 1` in `main.py`.
- **Install fails**: check your Python version (must be 3.10 to 3.12) and your internet connection. Delete the `venv` folder and run `run.bat` again.
- **Cursor shakes**: lower `SMOOTH_MIN_CUTOFF` (for example 1.0) and improve lighting.
- **Cursor feels slow**: raise `SMOOTH_MIN_CUTOFF` or `SMOOTH_BETA`.
- **Zoom does nothing**: zoom works in apps that support Ctrl + mouse wheel (browsers, Word, Excel, PDF viewers, image viewers).
- **Does not control some windows**: Windows blocks it over programs running as Administrator (for example Task Manager).
- Only your **main monitor** is controlled.
