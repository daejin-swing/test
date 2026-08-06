#!/usr/bin/env bash

# 1. 절대 경로 및 환경변수 (Line 3, 5)
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null && pwd )"
source "$DIR/env_setup.sh"

function launch {
  [ -f "$DIR/.git/index.lock" ] && rm -f $DIR/.git/index.lock

  # 2. OTA 업데이트 스왑 로직 (Line 43 ~ 66)
  if [ -f "${DIR}/.overlay_init" ]; then
    find ${DIR}/.git -newer ${DIR}/.overlay_init | grep -q '.' 2> /dev/null
    if [ $? -ne 0 ] && [ -f "${STAGING_ROOT}/finalized/.overlay_consistent" ]; then
      if [ ! -d /data/safe_staging/old_openpilot ]; then
        LAUNCHER_LOCATION="${BASH_SOURCE[0]}"
        mv $DIR /data/swing_safe_staging/old_openpilot
        mv "${STAGING_ROOT}/finalized" $DIR
        cd $DIR
        unset AGNOS_VERSION
        exec "${LAUNCHER_LOCATION}"
      fi
    fi
  fi

  # 3. 파이썬 경로 및 라이브러리 링크 (Line 68 ~ 78)
  ln -sfn $(pwd) /data/pythonpath

  # .venv (if present) takes priority over the default loading path, which
  # already resolves to /usr/local/venv -- ota.py's finalize_update() only
  # ever fetches into .venv whatever /usr/local/venv doesn't already have.
  LOCAL_VENV="$DIR/.venv"
  if [ -d "$LOCAL_VENV" ]; then
    PYVER=$(python3 -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}")')
    export PYTHONPATH="$LOCAL_VENV/lib/$PYVER/site-packages:$PWD"
  else
    export PYTHONPATH="$PWD"
  fi

  # 5. 카메라 데몬 (크래시 시 재시작)
  ( while true; do ./camerad/run_camerad.sh; sleep 2; done ) &

  # 6. 와이파이 설정 UI (크래시 시 재시작)
  ( while true; do python3 ui/wifi_ui.py; sleep 2; done ) &

  # 4. 앱 실행 (Line 86 ~ 90)
  exec python3 updater/ota.py
}

launch