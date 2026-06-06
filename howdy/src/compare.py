# Compare incoming video with known faces
# Running in a local python instance to get around PATH issues

# Import time so we can start timing asap
import time

# Start timing
timings = {
	"st": time.time()
}

# Import required modules
import sys
import os
import json
import configparser
import dlib
import cv2
from datetime import timezone, datetime
import atexit
import subprocess
import snapshot
import numpy as np
import _thread as thread
import paths_factory
from recorders.video_capture import VideoCapture
from i18n import _

def exit(code=None):
	"""Exit while closing howdy-gtk properly"""
	global gtk_proc

	# Exit the auth ui process if there is one
	if "gtk_proc" in globals():
		gtk_proc.terminate()

	# Remove the active-auth signal file so the GNOME Shell extension
	# knows authentication has ended and closes the camera overlay.
	for _f in ("/tmp/howdy-active", "/tmp/howdy-frame.jpg", "/tmp/howdy-frame.jpg.tmp"):
		try:
			os.remove(_f)
		except Exception:
			pass

	# Destroy all windows on exit
	try:
		cv2.destroyAllWindows()
	except Exception:
		pass

	# Exit compare
	if code is not None:
		sys.exit(code)


def init_detector(lock):
	"""Start face detector, encoder and predictor in a new thread"""
	global face_detector, pose_predictor, face_encoder

	# Test if at lest 1 of the data files is there and abort if it's not
	if not os.path.isfile(paths_factory.shape_predictor_5_face_landmarks_path()):
		print(_("Data files have not been downloaded, please run the following commands:"))
		print("\n\tcd " + paths_factory.dlib_data_dir_path())
		print("\tsudo ./install.sh\n")
		lock.release()
		exit(1)

	# Use the CNN detector if enabled
	if use_cnn:
		face_detector = dlib.cnn_face_detection_model_v1(paths_factory.mmod_human_face_detector_path())
	else:
		face_detector = dlib.get_frontal_face_detector()

	# Start the others regardless
	pose_predictor = dlib.shape_predictor(paths_factory.shape_predictor_5_face_landmarks_path())
	face_encoder = dlib.face_recognition_model_v1(paths_factory.dlib_face_recognition_resnet_model_v1_path())

	# Note the time it took to initialize detectors
	timings["ll"] = time.time() - timings["ll"]
	lock.release()


def make_snapshot(type):
	"""Generate snapshot after detection"""
	snapshot.generate(snapframes, [
		type + _(" LOGIN"),
		_("Date: ") + datetime.now(timezone.utc).strftime("%Y/%m/%d %H:%M:%S UTC"),
		_("Scan time: ") + str(round(time.time() - timings["fr"], 2)) + "s",
		_("Frames: ") + str(frames) + " (" + str(round(frames / (time.time() - timings["fr"]), 2)) + "FPS)",
		_("Hostname: ") + os.uname().nodename,
		_("Best certainty value: ") + str(round(lowest_certainty * 10, 1))
	])


def send_to_ui(type, message):
	"""Send message to the auth ui"""
	global gtk_proc

	# Only execute of the process started
	if "gtk_proc" in globals():
		# Format message so the ui can parse it
		message = type + "=" + message + " \n"

		# Try to send the message to the auth ui, but it's okay if that fails
		try:
			if gtk_proc.poll() is None:
				gtk_proc.stdin.write(bytearray(message.encode("utf-8")))
				gtk_proc.stdin.flush()
		except IOError:
			pass


def get_user_display(username):
	"""
	During sudo/lock screen: scan the authenticating user's processes for DISPLAY.
	During login (GDM): the user has no session yet, so fall back to the gdm
	user's display, which owns the login screen.
	"""
	import glob
	import pwd

	if os.environ.get("DISPLAY"):
		return os.environ.get("DISPLAY"), os.environ.get("XAUTHORITY")

	def scan_user(name):
		try:
			uid = pwd.getpwnam(name).pw_uid
		except KeyError:
			return None, None

		for pid_dir in glob.glob("/proc/[0-9]*"):
			try:
				if os.stat(pid_dir).st_uid != uid:
					continue
				with open(os.path.join(pid_dir, "environ"), "rb") as f:
					env = {}
					for item in f.read().split(b"\x00"):
						if b"=" in item:
							k, v = item.split(b"=", 1)
							env[k.decode(errors="replace")] = v.decode(errors="replace")
				if "DISPLAY" in env:
					return env["DISPLAY"], env.get("XAUTHORITY")
			except (PermissionError, FileNotFoundError, ValueError, OSError):
				continue
		return None, None

	# First try the authenticating user (covers sudo, lock screen)
	display, xauth = scan_user(username)
	if display:
		return display, xauth

	# Fall back to gdm's session (covers login screen)
	display, xauth = scan_user("gdm")
	if display:
		if not xauth:
			for pattern in ["/run/gdm3/auth-for-gdm*/database", "/var/run/gdm3/auth-for-gdm*/database"]:
				matches = glob.glob(pattern)
				if matches:
					xauth = matches[0]
					break
		return display, xauth

	# Second fallback: find the X socket and GDM auth file directly on disk
	sockets = sorted(glob.glob("/tmp/.X11-unix/X*"))
	if sockets:
		display = ":" + sockets[0].rsplit("X", 1)[-1]

		xauth = None
		for pattern in [
			"/run/user/*/gdm/Xauthority",
			"/run/gdm3/auth-for-gdm*/database",
			"/var/run/gdm3/auth-for-gdm*/database",
		]:
			matches = glob.glob(pattern)
			if matches:
				xauth = matches[0]
				break

		if xauth:
			return display, xauth

	return None, None


def render_frame_to_window(display_frame, face_locs, is_match_found, is_too_dark, darkness, elapsed, do_imshow=True):
	"""
	Draw annotations onto display_frame and always write the frame as a JPEG
	for the GNOME Shell lock-screen / greeter extension to pick up.

	The annotation drawing and JPEG encode are pure numpy/OpenCV and need no
	display, so they run everywhere. Only cv2.imshow() needs an X server, so it
	is gated by do_imshow — at the Wayland GDM greeter we still want the JPEG
	frames for the extension even though no OpenCV window can be drawn.
	Called both from the main loop and from the winning-frame path before exit(0).
	"""
	if display_frame.ndim == 2:
		display_frame = cv2.cvtColor(display_frame, cv2.COLOR_GRAY2BGR)

	if is_too_dark:
		cv2.putText(
			display_frame,
			f"Too dark ({round(darkness, 1)} > threshold {round(dark_threshold, 1)}) — adjust lighting",
			(8, 20),
			cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 80, 220), 1, cv2.LINE_AA
		)
	else:
		for fl in face_locs:
			if use_cnn:
				fl = fl.rect

			box_color = (0, 255, 0) if is_match_found else (0, 165, 255)
			cx = (fl.left() + fl.right()) // 2
			cy = (fl.top() + fl.bottom()) // 2
			radius = max(fl.right() - fl.left(), fl.bottom() - fl.top()) // 2 + 8

			cv2.circle(display_frame, (cx, cy), radius, box_color, 2)

			label = "Matched!" if is_match_found else "Scanning"
			cv2.putText(
				display_frame, label,
				(cx - 30, cy - radius - 8),
				cv2.FONT_HERSHEY_SIMPLEX, 0.5, box_color, 1, cv2.LINE_AA
			)

		if lowest_certainty < 10:
			certainty_display = round(lowest_certainty * 10, 1)
			hud_color = (0, 255, 0) if is_match_found else (200, 200, 200)
			cv2.putText(
				display_frame,
				f"Certainty: {certainty_display}  (need < {round(video_certainty * 10, 1)})",
				(8, 20),
				cv2.FONT_HERSHEY_SIMPLEX, 0.5, hud_color, 1, cv2.LINE_AA
			)

	cv2.putText(
		display_frame,
		f"Frame {frames}  |  {elapsed}s elapsed",
		(8, display_frame.shape[0] - 8),
		cv2.FONT_HERSHEY_SIMPLEX, 0.45, (180, 180, 180), 1, cv2.LINE_AA
	)

	# The OpenCV window only exists/works when we have a usable X display.
	if do_imshow:
		cv2.imshow("Howdy", display_frame)
		cv2.waitKey(1)

	# Write the current frame as JPEG for the GNOME Shell lock-screen extension.
	# Uses a temp file + atomic rename so the extension never reads a partial JPEG.
	try:
		_ok, _buf = cv2.imencode(".jpg", display_frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
		if _ok:
			with open("/tmp/howdy-frame.jpg.tmp", "wb") as _fh:
				_fh.write(_buf.tobytes())
			os.replace("/tmp/howdy-frame.jpg.tmp", "/tmp/howdy-frame.jpg")
	except Exception:
		pass


def raise_window_once():
	"""
	Raise the Howdy window to the top of the stacking order.
	Safe to call multiple times — only acts on the first call.
	Searches by PID so it always finds our own window, not stale ones.

	Note: override_redirect is intentionally NOT used here. It strips WM
	decorations and causes the camera content to overflow the window boundary.
	It also has no effect against GNOME Shell's lock screen (Mutter blocks it
	as a security measure). For lock screen overlay use the GNOME Shell
	extension (howdy-screen@howdy) instead.
	"""
	global _window_raised
	if _window_raised:
		return
	_window_raised = True
	wlog(f"raise_window_once — raising window (pid={_own_pid})")
	try:
		wid_result = subprocess.run(
			["xdotool", "search", "--pid", str(_own_pid)],
			capture_output=True, text=True, timeout=2
		)
		wids = [w for w in wid_result.stdout.strip().split("\n") if w]
		wlog(f"xdotool search --pid result: {wids}")

		if wids:
			wid = wids[-1]
			raise_result = subprocess.run(
				["xdotool", "windowraise", wid],
				capture_output=True, text=True, timeout=2
			)
			wlog(f"windowraise exit={raise_result.returncode}")
		else:
			wlog("xdotool --pid search found no windows for our pid")
	except FileNotFoundError:
		wlog("xdotool not found")
	except subprocess.TimeoutExpired:
		wlog("xdotool timed out")


# Make sure we were given an username to test against
if len(sys.argv) < 2:
	exit(12)

# The username of the user being authenticated
user = sys.argv[1]
# The model file contents
models = []
# Encoded face models
encodings = []
# Amount of ignored 100% black frames
black_tries = 0
# Amount of ignored dark frames
dark_tries = 0
# Total amount of frames captured
frames = 0
# Captured frames for snapshot capture
snapframes = []
# Tracks the lowest certainty value in the loop
lowest_certainty = 10
# Face recognition/detection instances
face_detector = None
pose_predictor = None
face_encoder = None


# Try to load the face model from the models folder
try:
	models = json.load(open(paths_factory.user_model_path(user)))

	for model in models:
		encodings += model["data"]
except FileNotFoundError:
	exit(10)

# Check if the file contains a model
if len(models) < 1:
	exit(10)

# Read config from disk
config = configparser.ConfigParser()
config.read(paths_factory.config_file_path())

# Get all config values needed
use_cnn = config.getboolean("core", "use_cnn", fallback=False)
timeout = config.getint("video", "timeout", fallback=4)
dark_threshold = config.getfloat("video", "dark_threshold", fallback=50.0)
video_certainty = config.getfloat("video", "certainty", fallback=3.5) / 10
end_report = config.getboolean("debug", "end_report", fallback=False)
save_failed = config.getboolean("snapshots", "save_failed", fallback=False)
save_successful = config.getboolean("snapshots", "save_successful", fallback=False)
gtk_stdout = config.getboolean("debug", "gtk_stdout", fallback=False)
rotate = config.getint("video", "rotate", fallback=0)
show_window = config.getboolean("video", "show_window", fallback=False)
# Whether to write /tmp/howdy-frame.jpg for the GNOME Shell extension overlay
# (lock screen + GDM greeter). Independent of show_window: the greeter has no
# usable X display for cv2.imshow but still wants the JPEG frames.
overlay = config.getboolean("video", "overlay", fallback=True)

# Send the gtk output to the terminal if enabled in the config
gtk_pipe = sys.stdout if gtk_stdout else subprocess.DEVNULL

# Start the auth ui, register it to be always be closed on exit
try:
	gtk_proc = subprocess.Popen(["howdy-gtk", "--start-auth-ui"], stdin=subprocess.PIPE, stdout=gtk_pipe, stderr=gtk_pipe)
	atexit.register(exit)
except FileNotFoundError:
	pass

# Write to the stdin to redraw ui
send_to_ui("M", _("Starting up..."))

# Save the time needed to start the script
timings["in"] = time.time() - timings["st"]

# Import face recognition, takes some time
timings["ll"] = time.time()

# Start threading and wait for init to finish
lock = thread.allocate_lock()
lock.acquire()
thread.start_new_thread(init_detector, (lock, ))

# Start video capture on the IR camera
timings["ic"] = time.time()

video_capture = VideoCapture(config)

# Read exposure from config to use in the main loop
exposure = config.getint("video", "exposure", fallback=-1)

# Note the time it took to open the camera
timings["ic"] = time.time() - timings["ic"]

# wait for thread to finish
lock.acquire()
lock.release()
del lock

# Fetch the max frame height
max_height = config.getfloat("video", "max_height", fallback=320.0)

# Get the height of the image (which would be the width if screen is portrait oriented)
height = video_capture.internal.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1
if rotate == 2:
	height = video_capture.internal.get(cv2.CAP_PROP_FRAME_WIDTH) or 1
# Calculate the amount the image has to shrink
scaling_factor = (max_height / height) or 1

# Fetch config settings out of the loop
timeout = config.getint("video", "timeout", fallback=4)
dark_threshold = config.getfloat("video", "dark_threshold", fallback=60)
end_report = config.getboolean("debug", "end_report", fallback=False)

# Initiate histogram equalization
clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))


# PAM strips the environment, so cv2.imshow() would fail with
# "cannot connect to X server" unless we restore DISPLAY/XAUTHORITY
# from the authenticating user's session.
#
# During GDM login the PAM process runs as root, which is not in the
# X server's access control list even when DISPLAY/XAUTHORITY are set
# correctly. We fix this by calling "xauth merge" so root inherits
# GDM's magic cookie, then probe the connection with xdpyinfo before
# creating any window. A broad try/except ensures a display failure
# never blocks authentication.

# Debug log — written at every step. Check with: sudo cat /tmp/howdy-debug.log
_LOG = "/tmp/howdy-debug.log"

def wlog(msg):
	"""Append a timestamped line to the debug log, never raises."""
	try:
		with open(_LOG, "a") as f:
			f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")
	except Exception:
		pass

_window_ready = False
_window_raised = False
_own_pid = os.getpid()

wlog(f"=== howdy window init start, user='{user}', show_window={show_window}, pid={_own_pid} ===")

if show_window:
	try:
		wlog("calling get_user_display...")
		display, xauthority = get_user_display(user)
		wlog(f"get_user_display returned display={display!r}, xauthority={xauthority!r}")

		if display:
			os.environ["DISPLAY"] = display
			wlog(f"set DISPLAY={display}")

			if xauthority:
				os.environ["XAUTHORITY"] = xauthority
				wlog(f"set XAUTHORITY={xauthority}")

				# Merge GDM's Xauth cookie into root's keyring so the PAM
				# process (running as root) is allowed to open GDM's X server.
				# Hard 2s timeout — the lock file can block for 20+ seconds if
				# a previous auth process died while holding the write lock.
				try:
					merge = subprocess.run(
						["xauth", "merge", xauthority],
						stdout=subprocess.PIPE,
						stderr=subprocess.PIPE,
						timeout=2
					)
					wlog(f"xauth merge exit={merge.returncode} stderr={merge.stderr.decode(errors='replace').strip()!r}")
				except subprocess.TimeoutExpired:
					wlog("xauth merge timed out after 2s — continuing anyway")
			else:
				wlog("no xauthority file found — skipping xauth merge")

			import pwd
			uid = pwd.getpwnam(user).pw_uid
			xdg_runtime = f"/run/user/{uid}"
			if os.path.isdir(xdg_runtime):
				os.environ["XDG_RUNTIME_DIR"] = xdg_runtime
				wlog(f"set XDG_RUNTIME_DIR={xdg_runtime}")
			else:
				wlog(f"XDG_RUNTIME_DIR path {xdg_runtime} does not exist, skipping")

			wlog("running xdpyinfo probe...")
			try:
				probe = subprocess.run(
					["xdpyinfo"],
					stdout=subprocess.DEVNULL,
					stderr=subprocess.PIPE,
					timeout=3
				)
				wlog(f"xdpyinfo exit={probe.returncode} stderr={probe.stderr.decode(errors='replace').strip()!r}")
			except subprocess.TimeoutExpired:
				wlog("xdpyinfo timed out — disabling window")
				show_window = False
				probe = None

			if show_window and probe is not None and probe.returncode != 0:
				wlog("xdpyinfo FAILED — disabling window")
				show_window = False
			elif show_window:
				wlog("xdpyinfo OK — creating OpenCV window...")
				try:
					# WINDOW_NORMAL lets us set an explicit size so the content
					# never overflows the window boundary (unlike WINDOW_AUTOSIZE).
					cv2.namedWindow("Howdy", cv2.WINDOW_NORMAL)
					cv2.resizeWindow("Howdy", int(max_height * 4 / 3), int(max_height))
					cv2.setWindowTitle("Howdy", "Howdy - identifying you")
					cv2.setWindowProperty("Howdy", cv2.WND_PROP_TOPMOST, 1)
					_window_ready = True
					wlog("OpenCV window created OK, _window_ready=True")
				except Exception as cv_err:
					wlog(f"OpenCV window creation FAILED: {cv_err}")
					show_window = False
		else:
			wlog("get_user_display returned no display — disabling window")
			show_window = False

	except Exception as e:
		wlog(f"window init EXCEPTION: {e}")
		show_window = False

wlog(f"window init done: _window_ready={_window_ready}, show_window={show_window}")

# Signal to the GNOME Shell extension that auth is starting.
# The extension (howdy-screen@howdy) watches for this file and shows the
# camera overlay above the lock screen when it appears.
try:
	open("/tmp/howdy-active", "w").close()
except Exception:
	pass

# Let the ui know that we're ready
send_to_ui("M", _("Identifying you..."))

# Start the read loop
frames = 0
valid_frames = 0
timings["fr"] = time.time()
dark_running_total = 0

while True:
	# Increment the frame count every loop
	frames += 1

	# Form a string to let the user know we're real busy
	ui_subtext = "Scanned " + str(valid_frames - dark_tries) + " frames"
	if dark_tries > 1:
		ui_subtext += " (skipped " + str(dark_tries) + " dark frames)"
	send_to_ui("S", ui_subtext)

	# Stop if we've exceeded the time limit
	if time.time() - timings["fr"] > timeout:
		if save_failed:
			make_snapshot(_("FAILED"))

		if dark_tries == valid_frames:
			print(_("All frames were too dark, please check dark_threshold in config"))
			print(_("Average darkness: {avg}, Threshold: {threshold}").format(
				avg=str(dark_running_total / max(1, valid_frames)),
				threshold=str(dark_threshold)
			))
			exit(13)
		else:
			exit(11)

	# Grab a single frame of video
	frame, gsframe = video_capture.read_frame()
	gsframe = clahe.apply(gsframe)

	# If snapshots have been turned on
	if save_failed or save_successful:
		if len(snapframes) < 3:
			snapframes.append(frame)

	# Create a histogram of the image with 8 values
	hist = cv2.calcHist([gsframe], [0], None, [8], [0, 256])
	hist_total = np.sum(hist)

	# Calculate frame darkness.
	# Use hist[0][0] (not hist[0]) to extract a true Python scalar — hist[0]
	# is a 1-element numpy array and passing it to float() or round() raises
	# a DeprecationWarning (error in future numpy versions).
	darkness = float(hist[0][0] / hist_total * 100)

	# If the image is fully black due to a bad camera read, skip entirely.
	# These frames have no usable data so we don't even show them in the window.
	if (hist_total == 0) or (darkness == 100):
		black_tries += 1
		continue

	dark_running_total += darkness
	valid_frames += 1

	# Flag whether this frame is too dark for face recognition.
	# We still scale, rotate, and display dark frames — only recognition is
	# skipped — so the window always shows live camera output.
	is_too_dark = darkness > dark_threshold
	if is_too_dark:
		dark_tries += 1

	# Scale and rotate — applied to ALL non-black frames so the window
	# always shows a properly sized, oriented image.
	if scaling_factor != 1:
		frame = cv2.resize(frame, None, fx=scaling_factor, fy=scaling_factor, interpolation=cv2.INTER_AREA)
		gsframe = cv2.resize(gsframe, None, fx=scaling_factor, fy=scaling_factor, interpolation=cv2.INTER_AREA)

	if rotate == 1:
		if frames % 3 == 1:
			frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
			gsframe = cv2.rotate(gsframe, cv2.ROTATE_90_COUNTERCLOCKWISE)
		if frames % 3 == 2:
			frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
			gsframe = cv2.rotate(gsframe, cv2.ROTATE_90_CLOCKWISE)
	elif rotate == 2:
		if frames % 2 == 0:
			frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
			gsframe = cv2.rotate(gsframe, cv2.ROTATE_90_COUNTERCLOCKWISE)
		else:
			frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
			gsframe = cv2.rotate(gsframe, cv2.ROTATE_90_CLOCKWISE)

	# Face recognition — only on frames bright enough for detection
	face_locations = []
	winning_fl = None
	if not is_too_dark:
		face_locations = face_detector(gsframe, 1)
		for fl in face_locations:
			if use_cnn:
				fl = fl.rect

			face_landmark = pose_predictor(frame, fl)
			face_encoding = np.array(face_encoder.compute_face_descriptor(frame, face_landmark, 1))

			matches = np.linalg.norm(encodings - face_encoding, axis=1)
			match_index = np.argmin(matches)
			match = matches[match_index]

			if lowest_certainty > match:
				lowest_certainty = match

			if 0 < match < video_certainty:
				timings["tt"] = time.time() - timings["st"]
				timings["fl"] = time.time() - timings["fr"]
				winning_fl = fl

				if end_report:
					def print_timing(label, k):
						"""Helper function to print a timing from the list"""
						print("  %s: %dms" % (label, round(timings[k] * 1000)))

					print(_("Time spent"))
					print_timing(_("Starting up"), "in")
					print(_("  Open cam + load libs: %dms") % (round(max(timings["ll"], timings["ic"]) * 1000, )))
					print_timing(_("  Opening the camera"), "ic")
					print_timing(_("  Importing recognition libs"), "ll")
					print_timing(_("Searching for known face"), "fl")
					print_timing(_("Total time"), "tt")

					print(_("\nResolution"))
					width = video_capture.fw or 1
					print(_("  Native: %dx%d") % (height, width))
					scale_height, scale_width = frame.shape[:2]
					print(_("  Used: %dx%d") % (scale_height, scale_width))

					print(_("\nFrames searched: %d (%.2f fps)") % (frames, frames / timings["fl"]))
					print(_("Black frames ignored: %d ") % (black_tries, ))
					print(_("Dark frames ignored: %d ") % (dark_tries, ))
					print(_("Certainty of winning frame: %.3f") % (match * 10, ))
					print(_("Winning model: %d (\"%s\")") % (match_index, models[match_index]["label"]))

				if save_successful:
					make_snapshot(_("SUCCESSFUL"))

				if config.getboolean("rubberstamps", "enabled", fallback=False):
					import rubberstamps
					send_to_ui("S", "")
					if "gtk_proc" not in vars():
						gtk_proc = None
					rubberstamps.execute(config, gtk_proc, {
						"video_capture": video_capture,
						"face_detector": face_detector,
						"pose_predictor": pose_predictor,
						"clahe": clahe
					})

				# Render the winning frame with "Matched!" annotation before
				# sleeping, so the window and GNOME Shell extension overlay are
				# both visible even if this is the very first frame captured.
				_do_imshow = show_window and _window_ready
				if overlay or _do_imshow:
					try:
						if _do_imshow:
							raise_window_once()
						elapsed = round(time.time() - timings["fr"], 1)
						render_frame_to_window(
							frame.copy(),
							[winning_fl],
							True,   # is_match_found
							False,  # is_too_dark
							darkness,
							elapsed,
							do_imshow=_do_imshow
						)
					except Exception as render_err:
						wlog(f"winning frame render error: {render_err}")

				wlog("match found — holding window for 0.8s before exit")
				time.sleep(0.8)
				exit(0)

	# Frame rendering — runs for every non-black frame, including dark ones.
	# Always writes the JPEG for the extension overlay; the OpenCV window is
	# only drawn/raised when a usable X display exists (do_imshow).
	#
	# Wrapped in try/except — a display hiccup must never block authentication.
	_do_imshow = show_window and _window_ready
	if overlay or _do_imshow:
		try:
			elapsed = round(time.time() - timings["fr"], 1)
			is_match_found = lowest_certainty < video_certainty

			render_frame_to_window(
				frame.copy(),
				face_locations,
				is_match_found,
				is_too_dark,
				darkness,
				elapsed,
				do_imshow=_do_imshow
			)

			if _do_imshow:
				raise_window_once()

			if frames <= 6:
				wlog(f"render loop frame {frames} — rendered (imshow={_do_imshow}), is_too_dark={is_too_dark}")

		except Exception as e:
			wlog(f"frame render exception at frame {frames}: {e}")
			# Disable the OpenCV window on error, but keep writing overlay frames.
			show_window = False

	if exposure != -1:
		# For a strange reason on some cameras (e.g. Lenoxo X1E) setting manual exposure works only after a couple frames
		# are captured and even after a delay it does not always work. Setting exposure at every frame is reliable though.
		video_capture.internal.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1.0)  # 1 = Manual
		video_capture.internal.set(cv2.CAP_PROP_EXPOSURE, float(exposure))