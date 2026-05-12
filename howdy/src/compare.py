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
			if gtk_proc.poll() is None: # Make sure the gtk_proc is still running before write into the pipe
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
		#Scan /proc for a process owned by `name` that has DISPLAY set.
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
		# GDM's Xauthority is not always in its process environment.
		# Check the standard location if the process scan didn't find it.
		if not xauth:
			for pattern in ["/run/gdm3/auth-for-gdm*/database", "/var/run/gdm3/auth-for-gdm*/database"]:
				matches = glob.glob(pattern)
				if matches:
					xauth = matches[0]
					break
		return display, xauth

	# Second fallback: find the X socket and GDM auth file directly on disk
	import glob as _glob

	sockets = sorted(_glob.glob("/tmp/.X11-unix/X*"))
	if sockets:
		display = ":" + sockets[0].rsplit("X", 1)[-1]  # → ":1"

		xauth = None
		for pattern in [
			"/run/user/*/gdm/Xauthority",       # ← your system
			"/run/gdm3/auth-for-gdm*/database",
			"/var/run/gdm3/auth-for-gdm*/database",
		]:
			matches = _glob.glob(pattern)
			if matches:
				xauth = matches[0]
				break

		if xauth:
			return display, xauth

	return None, None

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
show_window = config.getboolean("video", "show_window", fallback=False) # Show window

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
_window_ready = False
if show_window:
	try:
		display, xauthority = get_user_display(user)

		if display:
			os.environ["DISPLAY"] = display
			if xauthority:
				os.environ["XAUTHORITY"] = xauthority

				# Merge GDM's Xauth cookie into root's keyring so the
				# PAM process (running as root) is allowed to open the
				# X server that GDM owns.
				subprocess.run(
					["xauth", "merge", xauthority],
					stdout=subprocess.DEVNULL,
					stderr=subprocess.DEVNULL
				)

			# Qt / OpenCV need XDG_RUNTIME_DIR. Derive it from the
			# authenticating user's UID — /run/user/<uid> is the standard
			# location set by systemd-logind.
			import pwd
			uid = pwd.getpwnam(user).pw_uid
			xdg_runtime = f"/run/user/{uid}"
			if os.path.isdir(xdg_runtime):
				os.environ["XDG_RUNTIME_DIR"] = xdg_runtime

			# Probe the X connection before creating any window.
			# If this fails (e.g. cookie mismatch) we disable the window
			# and log the reason so it can be diagnosed without breaking auth.
			probe = subprocess.run(
				["xdpyinfo"],
				stdout=subprocess.DEVNULL,
				stderr=subprocess.PIPE
			)
			if probe.returncode != 0:
				with open("/var/log/howdy-window.log", "a") as f:
					f.write(
						f"{datetime.now(timezone.utc).isoformat()} "
						f"xdpyinfo probe failed: {probe.stderr.decode(errors='replace').strip()}\n"
					)
				show_window = False
			else:
				# Create a named window so we can control its properties
				cv2.namedWindow("Howdy", cv2.WINDOW_GUI_NORMAL)
				cv2.resizeWindow("Howdy", 640, 360)
				cv2.setWindowTitle("Howdy", "Howdy - identifying you")
				cv2.setWindowProperty("Howdy", cv2.WND_PROP_TOPMOST, 1)
				_window_ready = True
		else:
			# No display found — silently disable the window so auth still works
			with open("/var/log/howdy-window.log", "a") as f:
				f.write(
					f"{datetime.now(timezone.utc).isoformat()} "
					f"get_user_display returned no display for user '{user}'\n"
				)
			show_window = False

	except Exception as e:
		# Log the real error so future failures can be diagnosed
		try:
			with open("/var/log/howdy-window.log", "a") as f:
				f.write(
					f"{datetime.now(timezone.utc).isoformat()} "
					f"window init exception: {e}\n"
				)
		except Exception:
			pass
		show_window = False

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
	if (dark_tries > 1):
		ui_subtext += " (skipped " + str(dark_tries) + " dark frames)"
	# Show it in the ui as subtext
	send_to_ui("S", ui_subtext)

	# Stop if we've exceeded the time limit
	if time.time() - timings["fr"] > timeout:
		# Create a timeout snapshot if enabled
		if save_failed:
			make_snapshot(_("FAILED"))

		if dark_tries == valid_frames:
			print(_("All frames were too dark, please check dark_threshold in config"))
			print(_("Average darkness: {avg}, Threshold: {threshold}").format(avg=str(dark_running_total / max(1, valid_frames)), threshold=str(dark_threshold)))
			exit(13)
		else:
			exit(11)

	# Grab a single frame of video
	frame, gsframe = video_capture.read_frame()
	gsframe = clahe.apply(gsframe)

	# If snapshots have been turned on
	if save_failed or save_successful:
		# Start capturing frames for the snapshot
		if len(snapframes) < 3:
			snapframes.append(frame)

	# Create a histogram of the image with 8 values
	hist = cv2.calcHist([gsframe], [0], None, [8], [0, 256])
	# All values combined for percentage calculation
	hist_total = np.sum(hist)

	# Calculate frame darkness
	darkness = (hist[0] / hist_total * 100)

	# If the image is fully black due to a bad camera read,
	# skip to the next frame
	if (hist_total == 0) or (darkness == 100):
		black_tries += 1
		continue

	dark_running_total += darkness
	valid_frames += 1

	# If the image exceeds darkness threshold due to subject distance,
	# skip to the next frame
	if (darkness > dark_threshold):
		dark_tries += 1
		continue

	# If the height is too high
	if scaling_factor != 1:
		# Apply that factor to the frame
		frame = cv2.resize(frame, None, fx=scaling_factor, fy=scaling_factor, interpolation=cv2.INTER_AREA)
		gsframe = cv2.resize(gsframe, None, fx=scaling_factor, fy=scaling_factor, interpolation=cv2.INTER_AREA)

	# If camera is configured to rotate = 1, check portrait in addition to landscape
	if rotate == 1:
		if frames % 3 == 1:
			frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
			gsframe = cv2.rotate(gsframe, cv2.ROTATE_90_COUNTERCLOCKWISE)
		if frames % 3 == 2:
			frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
			gsframe = cv2.rotate(gsframe, cv2.ROTATE_90_CLOCKWISE)

	# If camera is configured to rotate = 2, check portrait orientation
	elif rotate == 2:
		if frames % 2 == 0:
			frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
			gsframe = cv2.rotate(gsframe, cv2.ROTATE_90_COUNTERCLOCKWISE)
		else:
			frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
			gsframe = cv2.rotate(gsframe, cv2.ROTATE_90_CLOCKWISE)

	# Get all faces from that frame as encodings
	# Upsamples 1 time
	face_locations = face_detector(gsframe, 1)
	# Loop through each face
	for fl in face_locations:
		if use_cnn:
			fl = fl.rect

		# Fetch the faces in the image
		face_landmark = pose_predictor(frame, fl)
		face_encoding = np.array(face_encoder.compute_face_descriptor(frame, face_landmark, 1))

		# Match this found face against a known face
		matches = np.linalg.norm(encodings - face_encoding, axis=1)

		# Get best match
		match_index = np.argmin(matches)
		match = matches[match_index]

		# Update certainty if we have a new low
		if lowest_certainty > match:
			lowest_certainty = match

		# Check if a match that's confident enough
		if 0 < match < video_certainty:
			timings["tt"] = time.time() - timings["st"]
			timings["fl"] = time.time() - timings["fr"]

			# If set to true in the config, print debug text
			if end_report:
				def print_timing(label, k):
					"""Helper function to print a timing from the list"""
					print("  %s: %dms" % (label, round(timings[k] * 1000)))

				# Print a nice timing report
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
				# Save the new size for diagnostics
				scale_height, scale_width = frame.shape[:2]
				print(_("  Used: %dx%d") % (scale_height, scale_width))

				# Show the total number of frames and calculate the FPS by dividing it by the total scan time
				print(_("\nFrames searched: %d (%.2f fps)") % (frames, frames / timings["fl"]))
				print(_("Black frames ignored: %d ") % (black_tries, ))
				print(_("Dark frames ignored: %d ") % (dark_tries, ))
				print(_("Certainty of winning frame: %.3f") % (match * 10, ))

				print(_("Winning model: %d (\"%s\")") % (match_index, models[match_index]["label"]))

			# Make snapshot if enabled
			if save_successful:
				make_snapshot(_("SUCCESSFUL"))

			# Run rubberstamps if enabled
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

			# End peacefully
			exit(0)
  
	# We render after the recognition loop so we can annotate:
	#   • green circle  = face detected, currently matching
	#   • orange circle = face detected, not yet matching
	#   • certainty     = best score so far (lower = more certain, shown ×10)
	#   • frame info    = elapsed time and frame count
	#
	# The entire block is wrapped in try/except — a display hiccup must
	# never cause authentication to fail.
	#
	if show_window and _window_ready:
		try:
			display_frame = frame.copy()

			if display_frame.ndim == 2:
				display_frame = cv2.cvtColor(display_frame, cv2.COLOR_GRAY2BGR)

			elapsed = round(time.time() - timings["fr"], 1)
			is_match_found = lowest_certainty < video_certainty

			for fl in face_locations:
				if use_cnn:
					fl = fl.rect

				box_color = (0, 255, 0) if is_match_found else (0, 165, 255)

				# Compute circle center and radius from the bounding box
				cx = (fl.left() + fl.right()) // 2
				cy = (fl.top() + fl.bottom()) // 2
				radius = max(fl.right() - fl.left(), fl.bottom() - fl.top()) // 2 + 8

				cv2.circle(display_frame, (cx, cy), radius, box_color, 2)

				label = "Matched" if is_match_found else "Scanning"
				cv2.putText(
					display_frame, label,
					(cx - 30, cy - radius - 8),
					cv2.FONT_HERSHEY_SIMPLEX, 0.5, box_color, 1,
					cv2.LINE_AA
				)

			if lowest_certainty < 10:
				certainty_display = round(lowest_certainty * 10, 1)
				hud_color = (0, 255, 0) if is_match_found else (200, 200, 200)
				cv2.putText(
					display_frame,
					f"Certainty: {certainty_display}  (need < {round(video_certainty * 10, 1)})",
					(8, 20),
					cv2.FONT_HERSHEY_SIMPLEX, 0.5, hud_color, 1,
					cv2.LINE_AA
				)

			cv2.putText(
				display_frame,
				f"Frame {frames}  |  {elapsed}s elapsed",
				(8, display_frame.shape[0] - 8),
				cv2.FONT_HERSHEY_SIMPLEX, 0.45, (180, 180, 180), 1,
				cv2.LINE_AA
			)

			cv2.imshow("Howdy", display_frame)
			cv2.waitKey(1)

			# On the first rendered frame, force the window above GDM's shell.
			# We set _NET_WM_STATE_ABOVE via xprop (which talks to the X server
			# directly) and then raise the window with xdotool. Both tools must
			# be installed: apt install x11-utils xdotool
			if frames == 1:
				try:
					wid_result = subprocess.run(
						["xdotool", "search", "--name", "Howdy"],
						capture_output=True,
						text=True
					)
					wid = wid_result.stdout.strip().split("\n")[0]
					if wid:
						# Set ABOVE + STAYS_ON_TOP so gnome-shell's compositor
						# cannot bury the window beneath the login screen.
						subprocess.Popen(
							[
								"xprop", "-id", wid,
								"-f", "_NET_WM_STATE", "32a",
								"-set", "_NET_WM_STATE",
								"_NET_WM_STATE_ABOVE,_NET_WM_STATE_STAYS_ON_TOP"
							],
							stdout=subprocess.DEVNULL,
							stderr=subprocess.DEVNULL
						)
						subprocess.Popen(
							["xdotool", "windowraise", wid],
							stdout=subprocess.DEVNULL,
							stderr=subprocess.DEVNULL
						)
				except FileNotFoundError:
					pass  # xdotool / xprop not installed — skip silently

		except Exception as e:
			# Log window rendering errors without interrupting auth
			try:
				with open("/var/log/howdy-window.log", "a") as f:
					f.write(
						f"{datetime.now(timezone.utc).isoformat()} "
						f"window render exception: {e}\n"
					)
			except Exception:
				pass
			show_window = False

	if exposure != -1:
		# For a strange reason on some cameras (e.g. Lenoxo X1E) setting manual exposure works only after a couple frames
		# are captured and even after a delay it does not always work. Setting exposure at every frame is reliable though.
		video_capture.internal.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1.0)  # 1 = Manual
		video_capture.internal.set(cv2.CAP_PROP_EXPOSURE, float(exposure))
