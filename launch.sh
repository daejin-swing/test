#!/usr/bin/env bash

# 1. 절대 경로 및 환경변수
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null && pwd )"
source "$DIR/env_setup.sh"

function launch {
  [ -f "$DIR/.git/index.lock" ] && rm -f $DIR/.git/index.lock

  # 2. OTA 업데이트 스왑 로직
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

  # 3. 파이썬 경로 및 라이브러리 링크
  ln -sfn $(pwd) /data/pythonpath

  LOCAL_VENV="$DIR/.venv"
  if [ -d "$LOCAL_VENV" ]; then
    PYVER=$(python3 -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}")')
    export PYTHONPATH="$LOCAL_VENV/lib/$PYVER/site-packages:$PWD"
  else
    export PYTHONPATH="$PWD"
  fi

  # 4. 백그라운드 서비스 실행 (크래시 시 자동 재시작 루프)
  # 카메라 데몬
  ( while true; do [ -f "$DIR/camerad/run_camerad.sh" ] && ./camerad/run_camerad.sh; sleep 2; done ) &

  # 와이파이 UI
  ( while true; do [ -f "$DIR/ui/wifi_ui.py" ] && python3 ui/wifi_ui.py; sleep 2; done ) &

  # 에이전트 & 로그 전송기
  ( while true; do python3 agent/agentd.py; sleep 3; done ) &
  ( while true; do python3 agent/log_sender.py; sleep 3; done ) &

  # 블랙박스 (녹화, 삭제 관리, 이벤트 업로더)
  ( while true; do python3 dashcam/recorder.py; sleep 2; done ) &
  ( while true; do python3 dashcam/deleter.py; sleep 5; done ) &
  ( while true; do python3 dashcam/uploader.py; sleep 5; done ) &

  # 실시간 스트리머 (1단계 썸네일, 2단계 온디맨드 라이브)
  ( while true; do python3 stream/thumbnail_streamer.py; sleep 3; done ) &
  ( while true; do python3 stream/live_streamer.py; sleep 3; done ) &

  # 5. OTA 메인 프로세스 실행
  exec python3 updater/ota.py
}

launch