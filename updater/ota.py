#!/usr/bin/env python3
import os
import re
import glob
import datetime
import subprocess
import shutil
import signal
import fcntl
import time
import threading
from collections import defaultdict
from pathlib import Path
import datetime
from functools import lru_cache


from common import BASEDIR
from common.params import Params
from common.log import cloudlog


TICI = os.path.isfile('/TICI')
AGNOS = os.path.isfile('/AGNOS')
PC = not TICI

LOCK_FILE = os.getenv("UPDATER_LOCK_FILE", "/tmp/swing_safe_staging_overlay.lock")
STAGING_ROOT = os.getenv("UPDATER_STAGING_ROOT", "/data/swing_safe_staging")

OVERLAY_UPPER = os.path.join(STAGING_ROOT, "upper")
OVERLAY_METADATA = os.path.join(STAGING_ROOT, "metadata")
OVERLAY_MERGED = os.path.join(STAGING_ROOT, "merged")
FINALIZED = os.path.join(STAGING_ROOT, "finalized")

OVERLAY_INIT = Path(os.path.join(BASEDIR, ".overlay_init"))

# do not allow to engage after this many hours onroad and this many routes
HOURS_NO_CONNECTIVITY_MAX = 27
ROUTES_NO_CONNECTIVITY_MAX = 84
# send an offroad prompt after this many hours onroad and this many routes
HOURS_NO_CONNECTIVITY_PROMPT = 23
ROUTES_NO_CONNECTIVITY_PROMPT = 80



MIN_DATE = datetime.datetime(year=2025, month=2, day=21)
MAX_DATE = datetime.datetime(year=2035, month=1, day=1)

@lru_cache
def get_device_type():
  # lru_cache and cache can cause memory leaks when used in classes
  if PC:
    return "pc"
  with open("/sys/firmware/devicetree/base/model") as f:
    model = f.read().strip('\x00')
  return model.split('comma ')[-1]

def set_offroad_alert(alert: str, show_alert: bool, extra_text: str | None = None) -> None:
  pass

def min_date():
  # on systemd systems, the default time is the systemd build time
  systemd_path = Path("/lib/systemd/systemd")
  if systemd_path.exists():
    d = datetime.datetime.fromtimestamp(systemd_path.stat().st_mtime)
    return max(MIN_DATE, d + datetime.timedelta(days=1))
  return MIN_DATE

def system_time_valid():
  return min_date() < datetime.datetime.now() < MAX_DATE


class UserRequest:
  NONE = 0
  CHECK = 1
  FETCH = 2

class WaitTimeHelper:
  def __init__(self):
    self.ready_event = threading.Event()
    self.user_request = UserRequest.NONE
    signal.signal(signal.SIGHUP, self.update_now)
    signal.signal(signal.SIGUSR1, self.check_now)

  def update_now(self, signum: int, frame) -> None:
    cloudlog.info("caught SIGHUP, attempting to downloading update")
    self.user_request = UserRequest.FETCH
    self.ready_event.set()

  def check_now(self, signum: int, frame) -> None:
    cloudlog.info("caught SIGUSR1, checking for updates")
    self.user_request = UserRequest.CHECK
    self.ready_event.set()

  def sleep(self, t: float) -> None:
    self.ready_event.wait(timeout=t)

def write_time_to_param(params, param) -> None:
  t = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
  params.put(param, t, block=True)

def run(cmd: list[str], cwd: str | None = None) -> str:
  return subprocess.check_output(cmd, cwd=cwd, stderr=subprocess.STDOUT, encoding='utf8')


def set_consistent_flag(consistent: bool) -> None:
  os.sync()
  consistent_file = Path(os.path.join(FINALIZED, ".overlay_consistent"))
  if consistent:
    consistent_file.touch()
  elif not consistent:
    consistent_file.unlink(missing_ok=True)
  os.sync()


def setup_git_options(cwd: str) -> None:
  # We sync FS object atimes (which NEOS doesn't use) and mtimes, but ctimes
  # are outside user control. Make sure Git is set up to ignore system ctimes,
  # because they change when we make hard links during finalize. Otherwise,
  # there is a lot of unnecessary churn. This appears to be a common need on
  # OSX as well: https://www.git-tower.com/blog/make-git-rebase-safe-on-osx/

  # We are using copytree to copy the directory, which also changes
  # inode numbers. Ignore those changes too.

  # Set protocol to the new version (default after git 2.26) to reduce data
  # usage on git fetch --dry-run from about 400KB to 18KB.
  git_cfg = [
    ("core.trustctime", "false"),
    ("core.checkStat", "minimal"),
    ("protocol.version", "2"),
    ("gc.auto", "0"),
    ("gc.autoDetach", "false"),
  ]
  for option, value in git_cfg:
    run(["git", "config", option, value], cwd)


def dismount_overlay() -> None:
  if os.path.ismount(OVERLAY_MERGED):
    cloudlog.info("unmounting existing overlay")
    run(["sudo", "umount", "-l", OVERLAY_MERGED])


def init_overlay() -> None:

  # Re-create the overlay if BASEDIR/.git has changed since we created the overlay
  if OVERLAY_INIT.is_file() and os.path.ismount(OVERLAY_MERGED):
    git_dir_path = os.path.join(BASEDIR, ".git")
    new_files = run(["find", git_dir_path, "-newer", str(OVERLAY_INIT)])
    if not len(new_files.splitlines()):
      # A valid overlay already exists
      return
    else:
      cloudlog.info(".git directory changed, recreating overlay")

  cloudlog.info("preparing new safe staging area")

  params = Params()
  params.put_bool("UpdateAvailable", False, block=True)
  set_consistent_flag(False)
  dismount_overlay()
  run(["sudo", "rm", "-rf", STAGING_ROOT])
  if os.path.isdir(STAGING_ROOT):
    shutil.rmtree(STAGING_ROOT)

  for dirname in [STAGING_ROOT, OVERLAY_UPPER, OVERLAY_METADATA, OVERLAY_MERGED]:
    os.mkdir(dirname, 0o755)

  if os.lstat(BASEDIR).st_dev != os.lstat(OVERLAY_MERGED).st_dev:
    raise RuntimeError("base and overlay merge directories are on different filesystems; not valid for overlay FS!")

  # Leave a timestamped canary in BASEDIR to check at startup. The device clock
  # should be correct by the time we get here. If the init file disappears, or
  # critical mtimes in BASEDIR are newer than .overlay_init, continue.sh can
  # assume that BASEDIR has used for local development or otherwise modified,
  # and skips the update activation attempt.
  consistent_file = Path(os.path.join(BASEDIR, ".overlay_consistent"))
  if consistent_file.is_file():
    consistent_file.unlink()
  OVERLAY_INIT.touch()

  os.sync()
  overlay_opts = f"lowerdir={BASEDIR},upperdir={OVERLAY_UPPER},workdir={OVERLAY_METADATA}"

  mount_cmd = ["mount", "-t", "overlay", "-o", overlay_opts, "none", OVERLAY_MERGED]
  run(["sudo"] + mount_cmd)
  run(["sudo", "chmod", "755", os.path.join(OVERLAY_METADATA, "work")])

  git_diff = run(["git", "diff", "--submodule=diff"], OVERLAY_MERGED)
  params.put("GitDiff", git_diff, block=True)
  cloudlog.info(f"git diff output:\n{git_diff}")


SYSTEM_VENV_DIR = "/usr/local/venv"


def _normalize_name(name: str) -> str:
  return name.strip().lower().replace("_", "-")


def _required_specs(requirements_file: str) -> list[tuple[str, str | None]]:
  """Parses each requirement line into (normalized name, exact version or
  None if unpinned) -- e.g. "raylib==6.0.1.0" -> ("raylib", "6.0.1.0"),
  "opencv-python-headless" -> ("opencv-python-headless", None)."""
  specs = []
  with open(requirements_file) as f:
    for line in f:
      line = line.split("#", 1)[0].strip()
      if not line:
        continue
      if "==" in line:
        name, version = line.split("==", 1)
        specs.append((_normalize_name(name), version.strip()))
      else:
        name = re.split(r"[<>!~\s]", line, maxsplit=1)[0]
        specs.append((_normalize_name(name), None))
  return specs


def _installed_versions(venv_dir: str) -> dict[str, str]:
  """Reads package versions straight from *.dist-info folders in site-packages,
  rather than `pip list` -- venvs `uv venv` creates don't ship pip at all, and
  this also avoids invoking the sudo-wrapped `uv`/`pip` shims for a plain read."""
  matches = glob.glob(os.path.join(venv_dir, "lib", "python3.*", "site-packages"))
  if not matches:
    return {}
  versions = {}
  for entry in os.listdir(matches[0]):
    m = re.match(r"^(.+)-([^-]+)\.dist-info$", entry)
    if m:
      versions[_normalize_name(m.group(1))] = m.group(2)
  return versions


def sync_venv(finalized_dir: str) -> None:
  """Bring finalized_dir/.venv in sync with ui/requirements.txt.

  launch.sh puts .venv's site-packages ahead of the normal PYTHONPATH, which
  already resolves to /usr/local/venv (the device's system-managed venv) --
  so a requirement already satisfied there needs nothing in .venv at all;
  the plain import lookup falls through and finds it. Only requirements
  missing or at the wrong version in *both* places actually need fetching,
  and only ever into .venv, never into the system venv.

  This device's `uv`/`pip` are sudo-wrapped shims that remount the
  (normally read-only) root filesystem and always produce root-owned
  output, cache disabled -- so this is called as rarely as possible (only
  for genuinely unsatisfied requirements), and .venv's ownership is forced
  back to this process's own user immediately after, since it lives inside
  the git-tracked workspace and a root-owned file there would make a later
  `git clean` unable to touch it."""
  venv_dir = os.path.join(finalized_dir, ".venv")
  venv_python = os.path.join(venv_dir, "bin", "python3")
  requirements_file = os.path.join(finalized_dir, "ui", "requirements.txt")

  if not os.path.isdir(venv_dir):
    run(["uv", "venv", venv_dir])
    run(["sudo", "chown", "-R", f"{os.getuid()}:{os.getgid()}", venv_dir])

  required = _required_specs(requirements_file)
  system_installed = _installed_versions(SYSTEM_VENV_DIR)
  local_installed = _installed_versions(venv_dir)

  def satisfied(name: str, version: str | None) -> bool:
    for current in (local_installed.get(name), system_installed.get(name)):
      if current is not None and (version is None or current == version):
        return True
    return False

  missing = [f"{name}=={version}" if version else name
             for name, version in required if not satisfied(name, version)]

  if not missing:
    cloudlog.info("sync_venv: already satisfied by .venv or /usr/local/venv")
    return

  cloudlog.info(f"sync_venv: fetching into .venv: {missing}")
  run(["uv", "pip", "install", "--python", venv_python, *missing])
  run(["sudo", "chown", "-R", f"{os.getuid()}:{os.getgid()}", venv_dir])


def finalize_update() -> None:
  """Take the current OverlayFS merged view and finalize a copy outside of
  OverlayFS, ready to be swapped-in at BASEDIR. Copy using shutil.copytree"""

  # Remove the update ready flag and any old updates
  cloudlog.info("creating finalized version of the overlay")
  set_consistent_flag(False)

  # Copy the merged overlay view and set the update ready flag
  if os.path.exists(FINALIZED):
    shutil.rmtree(FINALIZED)
  shutil.copytree(OVERLAY_MERGED, FINALIZED, symlinks=True)

  run(["git", "reset", "--hard"], FINALIZED)
  run(["git", "submodule", "foreach", "--recursive", "git", "reset", "--hard"], FINALIZED)

  cloudlog.info("Starting git cleanup in finalized update")
  t = time.monotonic()
  try:
    run(["git", "gc"], FINALIZED)
    run(["git", "lfs", "prune"], FINALIZED)
    cloudlog.event("Done git cleanup", duration=time.monotonic() - t)
  except subprocess.CalledProcessError:
    cloudlog.exception(f"Failed git cleanup, took {time.monotonic() - t:.3f} s")

  cloudlog.info("syncing venv for finalized update")
  sync_venv(FINALIZED)

  set_consistent_flag(True)
  cloudlog.info("done finalizing overlay")


def handle_agnos_update() -> None:
  pass


def reboot_watcher(params: Params) -> None:
  while True:
    if params.get_bool("DoReboot"):
      cloudlog.warning("updater: DoReboot requested, rebooting now")
      params.put_bool("DoReboot", False, block=True)
      try:
        run(["sudo", "reboot"])
      except subprocess.CalledProcessError:
        cloudlog.exception("updater: reboot command failed")
    time.sleep(1)



class Updater:
  def __init__(self):
    self.params = Params()
    self.branches = defaultdict(str)
    self._has_internet: bool = False

  @property
  def has_internet(self) -> bool:
    return self._has_internet

  @property
  def target_branch(self) -> str:
    b: str | None = self.params.get("UpdaterTargetBranch")
    if b is None:
      b = self.get_branch(BASEDIR)
    b = {
      ("tizi", "release3"): "release-tizi",
      ("tizi", "release3-staging"): "release-tizi-staging",
      ("mici", "release3"): "release-mici",
      ("mici", "release3-staging"): "release-mici-staging",
    }.get((get_device_type(), b), b)
    return b

  @property
  def update_ready(self) -> bool:
    # "Ready to reboot" must mean FINALIZED actually differs from what's
    # currently running (BASEDIR) -- comparing BASEDIR against the remote's
    # latest known commit instead (self.branches[...]) is wrong: check_for_update()
    # refreshes that on every CHECK, so it goes stale the instant a new commit
    # lands upstream, well before fetch_update() ever runs. That falsely
    # reports "ready" (and drives a wasted reboot) whenever FINALIZED is
    # consistent but simply hasn't caught up to the newest push yet.
    consistent_file = Path(os.path.join(FINALIZED, ".overlay_consistent"))
    if consistent_file.is_file():
      hash_mismatch = self.get_commit_hash(FINALIZED) != self.get_commit_hash(BASEDIR)
      branch_mismatch = self.get_branch(BASEDIR) != self.target_branch
      on_target_branch = self.get_branch(FINALIZED) == self.target_branch
      return ((hash_mismatch or branch_mismatch) and on_target_branch)
    return False

  @property
  def update_available(self) -> bool:
    if os.path.isdir(OVERLAY_MERGED) and len(self.branches) > 0:
      hash_mismatch = self.get_commit_hash(OVERLAY_MERGED) != self.branches[self.target_branch]
      branch_mismatch = self.get_branch(OVERLAY_MERGED) != self.target_branch
      return hash_mismatch or branch_mismatch
    return False

  def get_branch(self, path: str) -> str:
    return run(["git", "rev-parse", "--abbrev-ref", "HEAD"], path).rstrip()

  def get_commit_hash(self, path: str = OVERLAY_MERGED) -> str:
    return run(["git", "rev-parse", "HEAD"], path).rstrip()

  def set_params(self, update_success: bool, failed_count: int, exception: str | None) -> None:
    self.params.put("UpdateFailedCount", failed_count, block=True)
    self.params.put("UpdaterTargetBranch", self.target_branch, block=True)

    self.params.put_bool("UpdaterFetchAvailable", self.update_available, block=True)
    if len(self.branches):
      self.params.put("UpdaterAvailableBranches", ','.join(self.branches.keys()), block=True)

    last_uptime_onroad = self.params.get("UptimeOnroad", return_default=True)
    last_route_count = self.params.get("RouteCount", return_default=True)
    if update_success:
      self.params.put("LastUpdateTime", datetime.datetime.now(datetime.UTC).replace(tzinfo=None), block=True)
      self.params.put("LastUpdateUptimeOnroad", last_uptime_onroad, block=True)
      self.params.put("LastUpdateRouteCount", last_route_count, block=True)
    else:
      last_uptime_onroad = self.params.get("LastUpdateUptimeOnroad", return_default=True)
      last_route_count = self.params.get("LastUpdateRouteCount", return_default=True)

    if exception is None:
      self.params.remove("LastUpdateException")
    else:
      self.params.put("LastUpdateException", exception, block=True)

    # Write out current and new version info
    def get_description(basedir: str) -> str:
      if not os.path.exists(basedir):
        return ""

      version = ""
      branch = ""
      commit = ""
      commit_date = ""
      try:
        branch = self.get_branch(basedir)
        commit = self.get_commit_hash(basedir)[:7]
        with open(os.path.join(basedir, "version")) as f:
          version = f.read().strip()

        commit_unix_ts = run(["git", "show", "-s", "--format=%ct", "HEAD"], basedir).rstrip()
        dt = datetime.datetime.fromtimestamp(int(commit_unix_ts))
        commit_date = dt.strftime("%b %d")
      except Exception:
        cloudlog.exception("updater.get_description")
      return f"{version} / {branch} / {commit} / {commit_date}"
    self.params.put("UpdaterCurrentDescription", get_description(BASEDIR), block=True)
    self.params.put("UpdaterNewDescription", get_description(FINALIZED), block=True)
    self.params.put_bool("UpdateAvailable", self.update_ready, block=True)

    # Handle user prompt
    for alert in ("Offroad_UpdateFailed", "Offroad_ConnectivityNeeded", "Offroad_ConnectivityNeededPrompt"):
      set_offroad_alert(alert, False)

    dt_uptime_onroad = (self.params.get("UptimeOnroad", return_default=True) - last_uptime_onroad) / (60*60)
    dt_route_count = self.params.get("RouteCount", return_default=True) - last_route_count
    if failed_count > 15 and exception is not None and self.has_internet:
      extra_text = exception
      set_offroad_alert("Offroad_UpdateFailed", True, extra_text=extra_text)
    elif failed_count > 0:
      if dt_uptime_onroad > HOURS_NO_CONNECTIVITY_MAX and dt_route_count > ROUTES_NO_CONNECTIVITY_MAX:
        set_offroad_alert("Offroad_ConnectivityNeeded", True)
      elif dt_uptime_onroad > HOURS_NO_CONNECTIVITY_PROMPT and dt_route_count > ROUTES_NO_CONNECTIVITY_PROMPT:
        remaining = max(HOURS_NO_CONNECTIVITY_MAX - dt_uptime_onroad, 1)
        set_offroad_alert("Offroad_ConnectivityNeededPrompt", True, extra_text=f"{remaining} hour{'' if remaining == 1 else 's'}.")

  def check_for_update(self) -> None:
    cloudlog.info("checking for updates")

    excluded_branches = ('release2', 'release2-staging')

    try:
      run(["git", "ls-remote", "origin", "HEAD"], OVERLAY_MERGED)
      self._has_internet = True
    except subprocess.CalledProcessError:
      self._has_internet = False

    setup_git_options(OVERLAY_MERGED)
    output = run(["git", "ls-remote", "--heads"], OVERLAY_MERGED)

    self.branches.clear()
    for line in output.split('\n'):
      ls_remotes_re = r'(?P<commit_sha>\b[0-9a-f]{5,40}\b)(\s+)(refs\/heads\/)(?P<branch_name>.*$)'
      x = re.fullmatch(ls_remotes_re, line.strip())
      if x is not None and x.group('branch_name') not in excluded_branches:
        self.branches[x.group('branch_name')] = x.group('commit_sha')

    cur_branch = self.get_branch(OVERLAY_MERGED)
    cur_commit = self.get_commit_hash(OVERLAY_MERGED)
    new_branch = self.target_branch
    new_commit = self.branches[new_branch]
    if (cur_branch, cur_commit) != (new_branch, new_commit):
      cloudlog.info(f"update available, {cur_branch} ({str(cur_commit)[:7]}) -> {new_branch} ({str(new_commit)[:7]})")
    else:
      cloudlog.info(f"up to date on {cur_branch} ({str(cur_commit)[:7]})")

  def fetch_update(self) -> None:
    cloudlog.info("attempting git fetch inside staging overlay")

    self.params.put("UpdaterState", "downloading...", block=True)

    # TODO: cleanly interrupt this and invalidate old update
    set_consistent_flag(False)
    self.params.put_bool("UpdateAvailable", False, block=True)

    setup_git_options(OVERLAY_MERGED)

    run(["git", "config", "--replace-all", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"], OVERLAY_MERGED)

    branch = self.target_branch
    git_fetch_output = run(["git", "fetch", "origin", branch], OVERLAY_MERGED)
    cloudlog.info("git fetch success: %s", git_fetch_output)

    cloudlog.info("git reset in progress")
    cmds = [
      ["git", "checkout", "--force", "--no-recurse-submodules", "-B", branch, "FETCH_HEAD"],
      ["git", "branch", "--set-upstream-to", f"origin/{branch}"],
      ["git", "reset", "--hard"],
      ["git", "clean", "-xdff", "-e", ".venv"],
      ["git", "submodule", "sync"],
      ["git", "submodule", "update", "--init", "--recursive"],
      ["git", "submodule", "foreach", "--recursive", "git", "reset", "--hard"],
    ]
    r = [run(cmd, OVERLAY_MERGED) for cmd in cmds]
    cloudlog.info("git reset success: %s", '\n'.join(r))

    # TODO: show agnos download progress
    if AGNOS:
      handle_agnos_update()

    # Create the finalized, ready-to-swap update
    self.params.put("UpdaterState", "finalizing update...", block=True)
    finalize_update()
    cloudlog.info("finalize success!")


def main() -> None:
  params = Params()

  # Defense against a stale flag surviving a crash/previous boot -- a reboot should
  # only ever happen because reboot_watcher() just consumed a fresh request, never
  # because DoReboot was left set from before this process started.
  params.put_bool("DoReboot", False, block=True)
  threading.Thread(target=reboot_watcher, args=(params,), daemon=True).start()

  if params.get_bool("DisableUpdates"):
    cloudlog.warning("updates are disabled by the DisableUpdates param")
    exit(0)

  with open(LOCK_FILE, 'w') as ov_lock_fd:
    try:
      fcntl.flock(ov_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
      raise RuntimeError("couldn't get overlay lock; is another instance running?") from e

    # Check if we just performed an update
    if Path(os.path.join(STAGING_ROOT, "old_openpilot")).is_dir():
      cloudlog.event("update installed")

    if not params.get("InstallDate"):
      t = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
      params.put("InstallDate", t, block=True)

    updater = Updater()
    update_failed_count = 0 # TODO: Load from param?
    wait_helper = WaitTimeHelper()

    # invalidate old finalized update
    set_consistent_flag(False)

    # set initial state
    params.put("UpdaterState", "idle", block=True)

    # Run the update loop
    first_run = True
    while True:
      wait_helper.ready_event.clear()

      # Attempt an update
      exception = None
      try:
        # TODO: reuse overlay from previous updated instance if it looks clean
        init_overlay()

        # ensure we have some params written soon after startup
        updater.set_params(False, update_failed_count, exception)

        if not system_time_valid() or first_run:
          first_run = False
          wait_helper.sleep(60)
          continue

        update_failed_count += 1

        # check for update
        params.put("UpdaterState", "checking...", block=True)
        updater.check_for_update()

        # download update
        last_fetch = params.get("UpdaterLastFetchTime")
        timed_out = last_fetch is None or (datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - last_fetch > datetime.timedelta(days=3))
        user_requested_fetch = wait_helper.user_request == UserRequest.FETCH
        if params.get_bool("NetworkMetered") and not timed_out and not user_requested_fetch:
          cloudlog.info("skipping fetch, connection metered")
        elif wait_helper.user_request == UserRequest.CHECK:
          cloudlog.info("skipping fetch, only checking")
        else:
          updater.fetch_update()
          write_time_to_param(params, "UpdaterLastFetchTime")
        update_failed_count = 0
      except subprocess.CalledProcessError as e:
        cloudlog.event(
          "update process failed",
          cmd=e.cmd,
          output=e.output,
          returncode=e.returncode
        )
        exception = f"command failed: {e.cmd}\n{e.output}"
        OVERLAY_INIT.unlink(missing_ok=True)
      except Exception as e:
        cloudlog.exception("uncaught updated exception, shouldn't happen")
        exception = str(e)
        OVERLAY_INIT.unlink(missing_ok=True)

      try:
        params.put("UpdaterState", "idle", block=True)
        update_successful = (update_failed_count == 0)
        updater.set_params(update_successful, update_failed_count, exception)
      except Exception:
        cloudlog.exception("uncaught updated exception while setting params, shouldn't happen")

      # infrequent attempts if we successfully updated recently
      wait_helper.user_request = UserRequest.NONE
      wait_helper.sleep(5*60 if update_failed_count > 0 else 1.5*60*60)


if __name__ == "__main__":
  main()