#!/usr/bin/env bash

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null && pwd )"
cd "$DIR"

# camerad creates its VisionIPC shm segment AND its /tmp/visionipc_camerad unix
# socket (used for the initial buffer-fd handshake) as root; relax both once they
# appear so the (non-root) UI process can connect and read frames.
(
  for _ in $(seq 1 50); do
    [ -e /dev/shm/msgq_visionipc_camerad_1 ] && break
    sleep 0.2
  done
  sudo chmod 666 /dev/shm/msgq_visionipc_camerad_1 2>/dev/null

  for _ in $(seq 1 50); do
    [ -S /tmp/visionipc_camerad ] && break
    sleep 0.2
  done
  sudo chmod 666 /tmp/visionipc_camerad 2>/dev/null
) &

export DISABLE_ROAD=1
export DISABLE_WIDE_ROAD=1
exec sudo -E ./camerad
