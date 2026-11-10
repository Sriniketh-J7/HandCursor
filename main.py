"""
HandMouse v2 (optimized) - control your Windows mouse with your hand (webcam, CPU only).

Gestures (palm facing the camera):
    POINT   : index finger up, other fingers folded, thumb tucked  -> move cursor
    ZOOM    : thumb + index finger out (L shape), others folded    -> spread = zoom in, pinch = zoom out
    SCROLL  : index + middle finger up, ring + pinky folded        -> move hand up/down to scroll
    FIST    : closed fist, hold ~0.7 s                             -> toggle control ON / PAUSED

Quit: press Q or ESC in the preview window (or Ctrl+C in the terminal).

How v2 stays smooth:
    * Camera reading, hand detection and mouse output each run on their own thread.
    * Detection runs asynchronously: old frames are dropped, so the cursor never lags behind.
    * The cursor and scrolling are driven by a 120 Hz output thread that glides between
      camera frames (and predicts slightly ahead), so low camera FPS still looks smooth.
"""

import math
import os
import sys
import threading
import time
import traceback
import urllib.request

os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")

import cv2
import mediapipe as mp
from mediapipe.tasks.python import BaseOptions, vision

# =============================================================
#                     TUNABLE SETTINGS
#        (change these numbers to adjust the feel)
# =============================================================

# ---- Camera ----
CAMERA_INDEX = 0          # 0 = default webcam, try 1 if you have several
CAMERA_BACKEND = "DSHOW"  # "DSHOW", "MSMF" or "ANY" - if FPS stays low, try another one
CAM_WIDTH = 640           # 320x240 is faster, 640x480 is more accurate
CAM_HEIGHT = 480
CAM_FPS = 30
CAM_FOURCC = "MJPG"       # "MJPG" gives 30 FPS on most webcams; "YUY2" is the alternative
# Low-light FPS fix. In dim light webcams slow down to ~10 FPS because auto-exposure
# makes each frame take longer. A fixed exposure keeps 30 FPS but makes the image darker.
# None = leave automatic. Try -5 (1/32 s), or -6 (1/64 s) if still slow. Camera dependent.
CAM_EXPOSURE = None

# ---- Hand model (downloaded automatically the first time, ~8 MB, needs internet once) ----
MODEL_FILE = "hand_landmarker.task"   # saved next to main.py
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
             "hand_landmarker/float16/latest/hand_landmarker.task")
MIN_DETECTION_CONF = 0.6  # how sure the app must be to START tracking a hand
MIN_PRESENCE_CONF = 0.5   # how sure it must be that a hand is still there
MIN_TRACKING_CONF = 0.5   # how sure it must be to KEEP tracking between frames

# ---- General ----
PRINT_DIAGNOSTICS = True  # prints camera FPS, hand-tracking FPS and delay every second
SHOW_PREVIEW = True       # False = no window (faster, quit with Ctrl+C)
PREVIEW_SCALE = 0.75      # size of the preview window
PREVIEW_EVERY_N = 2       # draw the preview on every Nth frame (higher = faster)
START_ENABLED = True      # False = start paused, make a fist to turn on
HAND_LOST_SECONDS = 0.30  # hand missing this long -> everything resets
MODE_CONFIRM_FRAMES = 3   # frames a gesture must hold before it switches (anti-flicker)

# ---- Cursor movement ----
# Box inside the camera image that maps to your whole screen
# (0.0 - 1.0, measured on the mirrored image). Smaller box = less hand travel.
ACTIVE_LEFT = 0.20
ACTIVE_RIGHT = 0.80
ACTIVE_TOP = 0.15
ACTIVE_BOTTOM = 0.70
# Which hand point drives the cursor: 5 = index knuckle (stable, does not jump when you
# curl your fingers into a fist), 8 = index fingertip (moves when fingers curl).
CURSOR_LANDMARK = 5
# Smoothing (One Euro filter). Raise MIN_CUTOFF = more responsive but more jitter.
# Raise BETA = less lag during fast moves.
SMOOTH_MIN_CUTOFF = 2.5
SMOOTH_BETA = 0.05
SMOOTH_D_CUTOFF = 1.0
# Output thread: moves the cursor / scrolls 120 times per second between camera frames.
GLIDE_HZ = 120
GLIDE_SECONDS = 0.04      # higher = smoother but more lag, lower = snappier but steppier
PREDICT_SECONDS = 0.03    # cursor aims this far ahead along your motion (0 = off, max ~0.06)

# ---- Zoom (thumb + index distance) ----
ZOOM_STEP = 0.10          # distance change (in hand-sizes) per zoom tick. Lower = more sensitive
ZOOM_DEADZONE = 0.005     # ignore tiny changes per frame
ZOOM_SMOOTHING = 0.4      # 0-1, higher = more responsive, lower = smoother
ZOOM_MAX_TICKS_PER_FRAME = 3
ZOOM_INVERT = False       # True = swap zoom in / zoom out direction

# ---- Scroll (two fingers up/down) ----
SCROLL_GAIN = 5000.0      # higher = faster scrolling
SCROLL_DEADZONE = 0.0015  # ignore tiny hand shake (fraction of image height per frame)
SCROLL_SMOOTHING = 0.5    # 0-1, higher = more responsive
SCROLL_MAX_PER_FRAME = 360  # max wheel units per camera frame (120 = one notch)
SCROLL_MIN_SEND = 10      # smallest wheel amount worth sending
SCROLL_GLIDE_SECONDS = 0.06  # spreads each scroll over this time, higher = smoother
SCROLL_INVERT = False     # True = hand up scrolls down instead

# ---- Fist on/off toggle ----
FIST_HOLD_SECONDS = 0.7   # how long to hold the fist
TOGGLE_BEEP = True        # high beep = ON, low beep = PAUSED

# ---- Gesture detection ----
FINGER_STRAIGHT_COS = 0.6 # how straight a finger must be to count as "up" (lower = easier)
THUMB_OUT_ENTER = 0.65    # thumb distance (in hand-sizes) to count as "out" for ZOOM
THUMB_OUT_STAY = 0.45     # once in ZOOM, thumb must stay beyond this

# =============================================================
#                     END OF SETTINGS
# =============================================================

IDLE, POINT, ZOOM, SCROLL, FIST = "IDLE", "POINT", "ZOOM", "SCROLL", "FIST"

FINGERS = ((5, 6, 8), (9, 10, 12), (13, 14, 16), (17, 18, 20))  # (mcp, pip, tip)


# ---------------------------------------------------------------
# Windows mouse / keyboard output (direct Win32 calls = lowest lag)
# ---------------------------------------------------------------
class WinMouse:
    WHEEL = 0x0800
    KEYUP = 0x0002
    VK_CONTROL = 0x11

    def __init__(self):
        if sys.platform != "win32":
            raise SystemExit("This version of HandMouse is for Windows only.")
        import ctypes

        self.user32 = ctypes.windll.user32
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                self.user32.SetProcessDPIAware()
            except Exception:
                pass
        self.width = self.user32.GetSystemMetrics(0)
        self.height = self.user32.GetSystemMetrics(1)
        self.user32.mouse_event.argtypes = [
            ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_size_t]
        self.user32.keybd_event.argtypes = [
            ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ulong, ctypes.c_size_t]
        # Smoother timing: 1 ms timer + slightly higher priority
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)
            k32 = ctypes.windll.kernel32
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00008000)  # ABOVE_NORMAL
        except Exception:
            pass

    def move(self, x, y):
        self.user32.SetCursorPos(int(x), int(y))

    def wheel(self, amount):
        self.user32.mouse_event(self.WHEEL, 0, 0, int(amount) & 0xFFFFFFFF, 0)

    def zoom(self, ticks):
        """ticks > 0 zoom in, < 0 zoom out (Ctrl + mouse wheel)."""
        if ticks == 0:
            return
        self.user32.keybd_event(self.VK_CONTROL, 0, 0, 0)
        step = 120 if ticks > 0 else -120
        for _ in range(abs(ticks)):
            self.wheel(step)
        self.user32.keybd_event(self.VK_CONTROL, 0, self.KEYUP, 0)

    def release(self):
        try:
            self.user32.keybd_event(self.VK_CONTROL, 0, self.KEYUP, 0)
        except Exception:
            pass


def beep(on):
    if not TOGGLE_BEEP:
        return
    try:
        import winsound
        threading.Thread(
            target=winsound.Beep, args=(1100 if on else 500, 120), daemon=True).start()
    except Exception:
        pass


# ---------------------------------------------------------------
# Camera reader running in its own thread (always newest frame)
# ---------------------------------------------------------------
class CameraStream:
    def __init__(self):
        backends = {"DSHOW": cv2.CAP_DSHOW, "MSMF": cv2.CAP_MSMF, "ANY": cv2.CAP_ANY}
        cap = cv2.VideoCapture(CAMERA_INDEX, backends.get(CAMERA_BACKEND.upper(), cv2.CAP_ANY))
        if not cap.isOpened():
            cap = cv2.VideoCapture(CAMERA_INDEX)
        if not cap.isOpened():
            raise SystemExit("Could not open the camera. Close other apps using it "
                             "and check Windows camera privacy settings.")
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*CAM_FOURCC))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAM_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAM_HEIGHT)
        cap.set(cv2.CAP_PROP_FPS, CAM_FPS)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if CAM_EXPOSURE is not None:
            cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)   # 0.25 = manual on DirectShow
            cap.set(cv2.CAP_PROP_EXPOSURE, CAM_EXPOSURE)
        self.cap = cap
        print(f"Camera opened: {int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x"
              f"{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}, driver says "
              f"{cap.get(cv2.CAP_PROP_FPS):.0f} FPS (backend {CAMERA_BACKEND})")
        self.frame = None
        self.counter = 0
        self.lock = threading.Lock()
        self.new_frame = threading.Event()
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while self.running:
            ok, frame = self.cap.read()
            if ok:
                with self.lock:
                    self.frame = frame
                    self.counter += 1
                self.new_frame.set()
            else:
                time.sleep(0.005)

    def wait_new(self, timeout=0.2):
        """Sleeps until a new frame arrives, then returns (frame, counter)."""
        self.new_frame.wait(timeout)
        self.new_frame.clear()
        with self.lock:
            return self.frame, self.counter

    def stop(self):
        self.running = False
        self.thread.join(timeout=1.0)
        self.cap.release()


# ---------------------------------------------------------------
# Output thread: cursor glide + prediction + smooth scrolling (120 Hz)
# ---------------------------------------------------------------
class Glider:
    def __init__(self, mouse):
        self.mouse = mouse
        self.lock = threading.Lock()
        self.target = None
        self.t_target = None
        self.vel = (0.0, 0.0)
        self.pos = None
        self.last = None
        self.scroll_pending = 0.0
        self.scroll_out = 0.0
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.running = False

    def set_target(self, x, y, t):
        with self.lock:
            if self.target is not None and self.t_target is not None:
                dt = t - self.t_target
                if 0.005 < dt < 0.3:
                    vx = (x - self.target[0]) / dt
                    vy = (y - self.target[1]) / dt
                    self.vel = (0.5 * vx + 0.5 * self.vel[0], 0.5 * vy + 0.5 * self.vel[1])
            self.target = (x, y)
            self.t_target = t

    def add_scroll(self, amount):
        with self.lock:
            self.scroll_pending += amount

    def clear(self):
        with self.lock:
            self.target = None
            self.t_target = None
            self.vel = (0.0, 0.0)
            self.pos = None
            self.scroll_pending = 0.0

    def _loop(self):
        period = 1.0 / GLIDE_HZ
        t_prev = time.perf_counter()
        max_x, max_y = self.mouse.width - 1, self.mouse.height - 1
        while self.running:
            time.sleep(period)
            now = time.perf_counter()
            dt = max(now - t_prev, 1e-4)
            t_prev = now
            move_to = None
            wheel = 0
            with self.lock:
                # ---- cursor ----
                tgt = self.target
                if tgt is None:
                    self.pos = None
                    self.last = None
                else:
                    vx, vy = self.vel
                    if now - self.t_target > 0.25:      # stale: stop predicting
                        vx = vy = 0.0
                    goal = (min(max(tgt[0] + vx * PREDICT_SECONDS, 0.0), max_x),
                            min(max(tgt[1] + vy * PREDICT_SECONDS, 0.0), max_y))
                    if self.pos is None:
                        self.pos = goal
                    else:
                        a = 1.0 - math.exp(-dt / GLIDE_SECONDS)
                        self.pos = (self.pos[0] + (goal[0] - self.pos[0]) * a,
                                    self.pos[1] + (goal[1] - self.pos[1]) * a)
                    ip = (int(round(self.pos[0])), int(round(self.pos[1])))
                    if ip != self.last:
                        move_to = ip
                        self.last = ip
                # ---- scroll ----
                if self.scroll_pending != 0.0:
                    a = 1.0 - math.exp(-dt / SCROLL_GLIDE_SECONDS)
                    take = self.scroll_pending * a
                    self.scroll_pending -= take
                    self.scroll_out += take
                    if abs(self.scroll_out) >= SCROLL_MIN_SEND:
                        wheel = int(self.scroll_out)
                        self.scroll_out -= wheel
                    if abs(self.scroll_pending) < 0.5:
                        self.scroll_pending = 0.0
            if move_to is not None:
                self.mouse.move(*move_to)
            if wheel:
                self.mouse.wheel(wheel)


# ---------------------------------------------------------------
# One Euro filter: smooth when still, fast when moving
# ---------------------------------------------------------------
class OneEuro:
    def __init__(self, min_cutoff, beta, d_cutoff):
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self.reset()

    def reset(self):
        self.t_prev = None
        self.x_prev = 0.0
        self.dx_prev = 0.0

    @staticmethod
    def _alpha(dt, cutoff):
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, x, t):
        if self.t_prev is None:
            self.t_prev, self.x_prev, self.dx_prev = t, x, 0.0
            return x
        dt = max(t - self.t_prev, 1e-3)
        dx = (x - self.x_prev) / dt
        a_d = self._alpha(dt, self.d_cutoff)
        dx_hat = a_d * dx + (1.0 - a_d) * self.dx_prev
        cutoff = self.min_cutoff + self.beta * abs(dx_hat)
        a = self._alpha(dt, cutoff)
        x_hat = a * x + (1.0 - a) * self.x_prev
        self.t_prev, self.x_prev, self.dx_prev = t, x_hat, dx_hat
        return x_hat


# ---------------------------------------------------------------
# Gesture analysis
# ---------------------------------------------------------------
def dist(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def finger_straight(pts, mcp, pip, tip):
    """True when the finger is extended (angle at the middle joint is nearly straight)."""
    v1 = [pts[pip][i] - pts[mcp][i] for i in range(3)]
    v2 = [pts[tip][i] - pts[pip][i] for i in range(3)]
    n1 = math.sqrt(sum(c * c for c in v1))
    n2 = math.sqrt(sum(c * c for c in v2))
    if n1 < 1e-6 or n2 < 1e-6:
        return False
    cos = sum(v1[i] * v2[i] for i in range(3)) / (n1 * n2)
    return cos > FINGER_STRAIGHT_COS


def analyze(pts):
    """Returns ([index, middle, ring, pinky] extended flags, thumb distance in hand-sizes)."""
    size = max(dist(pts[0], pts[9]), 1e-6)
    ext = [finger_straight(pts, m, p, t) for m, p, t in FINGERS]
    thumb = dist(pts[4], pts[5]) / size
    return ext, thumb


def classify(ext, thumb, prev_mode):
    idx, mid, ring, pnk = ext
    others_folded = not (mid or ring or pnk)
    if prev_mode == ZOOM and others_folded and thumb > THUMB_OUT_STAY:
        return ZOOM  # sticky: pinching closed won't drop out of zoom
    if idx and others_folded and thumb > THUMB_OUT_ENTER:
        return ZOOM
    if idx and others_folded:
        return POINT
    if idx and mid and not ring and not pnk:
        return SCROLL
    if not (idx or mid or ring or pnk) and thumb <= THUMB_OUT_ENTER:
        return FIST
    return IDLE


def clamp01(v):
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


# ---------------------------------------------------------------
# Controller: turns gestures into mouse actions
# ---------------------------------------------------------------
class Controller:
    def __init__(self, mouse, glider=None):
        self.mouse = mouse
        self.glider = glider
        self.enabled = START_ENABLED
        self.mode = IDLE
        self._pending, self._pending_n = IDLE, 0
        self.last_seen = 0.0
        self.fx = OneEuro(SMOOTH_MIN_CUTOFF, SMOOTH_BETA, SMOOTH_D_CUTOFF)
        self.fy = OneEuro(SMOOTH_MIN_CUTOFF, SMOOTH_BETA, SMOOTH_D_CUTOFF)
        self.fist_start = None
        self.fist_toggled = False
        self.fist_progress = 0.0
        self._reset_motion()

    def _reset_motion(self):
        self.fx.reset()
        self.fy.reset()
        if self.glider is not None:
            self.glider.clear()
        self.last_pos = None
        self.zoom_prev, self.zoom_acc = None, 0.0
        self.scroll_prev, self.scroll_acc = None, 0.0

    def _set_mode(self, mode):
        self.mode = mode
        self._pending, self._pending_n = mode, 0
        self._reset_motion()

    def _toggle(self):
        self.enabled = not self.enabled
        self._reset_motion()
        beep(self.enabled)

    def hand_lost(self, now):
        if self.mode != IDLE and now - self.last_seen > HAND_LOST_SECONDS:
            self._set_mode(IDLE)
            self.fist_start, self.fist_toggled, self.fist_progress = None, False, 0.0

    def update(self, lm, now, w, h):
        self.last_seen = now
        pts = [(p.x * w, p.y * h, p.z * w) for p in lm]
        ext, thumb = analyze(pts)
        raw = classify(ext, thumb, self.mode)

        # Debounce mode changes
        if raw != self.mode:
            if raw == self._pending:
                self._pending_n += 1
            else:
                self._pending, self._pending_n = raw, 1
            if self._pending_n >= MODE_CONFIRM_FRAMES:
                self._set_mode(raw)
        else:
            self._pending, self._pending_n = raw, 0

        # Fist toggle (works even while paused)
        if self.mode == FIST:
            if self.fist_start is None:
                self.fist_start = now
            self.fist_progress = min((now - self.fist_start) / FIST_HOLD_SECONDS, 1.0)
            if self.fist_progress >= 1.0 and not self.fist_toggled:
                self.fist_toggled = True
                self._toggle()
        else:
            self.fist_start, self.fist_toggled, self.fist_progress = None, False, 0.0

        if not self.enabled:
            return

        # While a gesture change is being detected (pose differs from current mode),
        # do nothing, so curling into a fist / lowering the hand never moves anything.
        if raw != self.mode:
            # hold still, but keep the cursor smoothing state (no jump when it resumes)
            self.zoom_prev, self.zoom_acc = None, 0.0
            self.scroll_prev, self.scroll_acc = None, 0.0
            return

        if self.mode == POINT:
            self._do_point(lm, now)
        elif self.mode == ZOOM:
            self._do_zoom(pts)
        elif self.mode == SCROLL:
            self._do_scroll(lm)

    def _do_point(self, lm, now):
        x = 1.0 - lm[CURSOR_LANDMARK].x  # mirror so moving right moves cursor right
        y = lm[CURSOR_LANDMARK].y
        nx = clamp01((x - ACTIVE_LEFT) / (ACTIVE_RIGHT - ACTIVE_LEFT))
        ny = clamp01((y - ACTIVE_TOP) / (ACTIVE_BOTTOM - ACTIVE_TOP))
        sx = self.fx(nx * (self.mouse.width - 1), now)
        sy = self.fy(ny * (self.mouse.height - 1), now)
        if self.glider is not None:
            self.glider.set_target(sx, sy, now)
            return
        pos = (int(round(sx)), int(round(sy)))
        if pos != self.last_pos:
            self.mouse.move(*pos)
            self.last_pos = pos

    def _do_zoom(self, pts):
        size = max(dist(pts[0], pts[9]), 1e-6)
        d = dist(pts[4], pts[8]) / size
        if self.zoom_prev is None:
            self.zoom_prev = d
            return
        d_s = ZOOM_SMOOTHING * d + (1.0 - ZOOM_SMOOTHING) * self.zoom_prev
        delta = d_s - self.zoom_prev
        self.zoom_prev = d_s
        if abs(delta) < ZOOM_DEADZONE:
            return
        self.zoom_acc += -delta if ZOOM_INVERT else delta
        limit = ZOOM_STEP * ZOOM_MAX_TICKS_PER_FRAME
        self.zoom_acc = max(-limit, min(limit, self.zoom_acc))
        ticks = int(self.zoom_acc / ZOOM_STEP)  # truncates toward zero
        if ticks:
            self.zoom_acc -= ticks * ZOOM_STEP
            self.mouse.zoom(ticks)

    def _do_scroll(self, lm):
        y = (lm[8].y + lm[12].y) / 2.0
        if self.scroll_prev is None:
            self.scroll_prev = y
            return
        y_s = SCROLL_SMOOTHING * y + (1.0 - SCROLL_SMOOTHING) * self.scroll_prev
        dy = y_s - self.scroll_prev
        self.scroll_prev = y_s
        if abs(dy) < SCROLL_DEADZONE:
            return
        amount = dy * SCROLL_GAIN if SCROLL_INVERT else -dy * SCROLL_GAIN
        amount = max(-SCROLL_MAX_PER_FRAME, min(SCROLL_MAX_PER_FRAME, amount))
        if self.glider is not None:
            self.glider.add_scroll(amount)
        elif abs(amount) >= SCROLL_MIN_SEND:
            self.mouse.wheel(int(amount))


# ---------------------------------------------------------------
# Preview window
# ---------------------------------------------------------------
def draw_preview(frame, lm, ctrl, fps, connections):
    h, w = frame.shape[:2]
    view = cv2.flip(frame, 1)

    # active region box
    cv2.rectangle(view, (int(ACTIVE_LEFT * w), int(ACTIVE_TOP * h)),
                  (int(ACTIVE_RIGHT * w), int(ACTIVE_BOTTOM * h)), (120, 120, 120), 1)

    if lm is not None:
        pts = [(int((1.0 - p.x) * w), int(p.y * h)) for p in lm]
        for c in connections:
            cv2.line(view, pts[c.start], pts[c.end], (255, 200, 0), 1)
        for p in pts:
            cv2.circle(view, p, 3, (0, 255, 120), -1)

    color = (0, 200, 0) if ctrl.enabled else (0, 0, 220)
    state = "ON" if ctrl.enabled else "PAUSED"
    cv2.putText(view, f"{state}  |  {ctrl.mode}  |  {fps:.0f} FPS", (10, 25),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
    if ctrl.fist_progress > 0:
        cv2.rectangle(view, (10, 38), (10 + int(150 * ctrl.fist_progress), 48), color, -1)
        cv2.rectangle(view, (10, 38), (160, 48), (255, 255, 255), 1)
    cv2.putText(view, "Q / ESC = quit", (10, h - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 200, 200), 1)

    if PREVIEW_SCALE != 1.0:
        view = cv2.resize(view, None, fx=PREVIEW_SCALE, fy=PREVIEW_SCALE)
    cv2.imshow("HandMouse", view)
    key = cv2.waitKey(1) & 0xFF
    return key in (ord("q"), ord("Q"), 27)


# ---------------------------------------------------------------
# Hand model file (auto-download on first run)
# ---------------------------------------------------------------
def get_model_path():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), MODEL_FILE)
    if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
        return path
    print("First run: downloading the hand model (~8 MB)...")
    tmp = path + ".part"
    try:
        urllib.request.urlretrieve(MODEL_URL, tmp)
        if os.path.getsize(tmp) < 1_000_000:
            raise OSError("downloaded file is too small")
        os.replace(tmp, path)
    except Exception as exc:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise SystemExit(
            f"Could not download the hand model ({exc}).\n"
            f"Download it manually from:\n  {MODEL_URL}\n"
            f"and save it as '{MODEL_FILE}' next to main.py.")
    return path


# ---------------------------------------------------------------
# Main
# ---------------------------------------------------------------
def main():
    model_path = get_model_path()
    mouse = WinMouse()
    cam = CameraStream()

    print("Waiting for the first camera frame...")
    frame, _ = cam.wait_new(5.0)
    if frame is None:
        cam.stop()
        raise SystemExit("The camera opened but gives no frames. Try another CAMERA_BACKEND.")
    fh, fw = frame.shape[:2]

    glider = Glider(mouse)
    glider.start()
    ctrl = Controller(mouse, glider)
    connections = list(vision.HandLandmarksConnections.HAND_CONNECTIONS)

    shared = {"lm": None, "results": 0, "latency_ms": 0.0, "error": False}

    def on_result(result, _image, timestamp_ms):
        """Runs on MediaPipe's thread every time a frame has been processed."""
        try:
            now = time.perf_counter()
            shared["results"] += 1
            shared["latency_ms"] += max(now * 1000.0 - timestamp_ms, 0.0)
            if result.hand_landmarks:
                lm = result.hand_landmarks[0]
                shared["lm"] = lm
                ctrl.update(lm, now, fw, fh)
            else:
                shared["lm"] = None
                ctrl.hand_lost(now)
        except Exception:
            if not shared["error"]:
                shared["error"] = True
                traceback.print_exc()

    options = vision.HandLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=model_path),  # CPU
        running_mode=vision.RunningMode.LIVE_STREAM,
        num_hands=1,
        min_hand_detection_confidence=MIN_DETECTION_CONF,
        min_hand_presence_confidence=MIN_PRESENCE_CONF,
        min_tracking_confidence=MIN_TRACKING_CONF,
        result_callback=on_result,
    )
    hands = vision.HandLandmarker.create_from_options(options)

    print("HandMouse running. Make a fist and hold to pause/resume. Q or ESC to quit.")
    last_counter = -1
    last_ts = -1
    loop_n = 0
    fps, t_prev = 0.0, time.perf_counter()
    diag_t, diag_cam0, diag_res0, diag_lat0 = t_prev, cam.counter, 0, 0.0

    try:
        while True:
            frame, counter = cam.wait_new(0.2)
            if frame is None or counter == last_counter:
                continue
            last_counter = counter
            now = time.perf_counter()
            fps = 0.9 * fps + 0.1 * (1.0 / max(now - t_prev, 1e-3))
            t_prev = now

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            ts = int(now * 1000)
            if ts <= last_ts:          # timestamps must always increase
                ts = last_ts + 1
            last_ts = ts
            hands.detect_async(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts)

            if PRINT_DIAGNOSTICS and now - diag_t >= 1.0:
                span = now - diag_t
                res, lat = shared["results"], shared["latency_ms"]
                n_res = res - diag_res0
                print(f"camera {(cam.counter - diag_cam0) / span:4.1f} FPS | "
                      f"hand tracking {n_res / span:4.1f} FPS | "
                      f"delay {(lat - diag_lat0) / max(n_res, 1):5.1f} ms      ",
                      end="\r", flush=True)
                diag_t, diag_cam0, diag_res0, diag_lat0 = now, cam.counter, res, lat

            loop_n += 1
            if SHOW_PREVIEW and loop_n % max(PREVIEW_EVERY_N, 1) == 0:
                if draw_preview(frame, shared["lm"], ctrl, fps, connections):
                    break
    except KeyboardInterrupt:
        pass
    finally:
        glider.stop()
        mouse.release()
        cam.stop()
        hands.close()
        cv2.destroyAllWindows()
        print("\nHandMouse stopped.")


if __name__ == "__main__":
    main()
