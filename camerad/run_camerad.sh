#!/usr/bin/env bash

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null && pwd )"
cd "$DIR"

# camerad creates one VisionIPC shm segment PER STREAM TYPE (msgq_visionipc_camerad_0,
# _1, _2, ...) AND its /tmp/visionipc_camerad unix socket (used for the initial
# buffer-fd handshake) as root; relax all of them once they appear so the
# (non-root) UI/streamer processes can connect and read frames. Segments for
# different streams can appear at different times as each camera finishes
# bringing up, so keep sweeping for new ones instead of waiting for a single
# hardcoded segment.
(
  for _ in $(seq 1 50); do
    compgen -G "/dev/shm/msgq_visionipc_camerad_*" > /dev/null && sudo chmod 666 /dev/shm/msgq_visionipc_camerad_* 2>/dev/null
    [ -S /tmp/visionipc_camerad ] && sudo chmod 666 /tmp/visionipc_camerad 2>/dev/null
    sleep 0.2
  done
) &

exec sudo -E ./camerad
