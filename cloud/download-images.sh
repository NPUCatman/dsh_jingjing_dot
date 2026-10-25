#!/usr/bin/env bash
# Run as root before the stack starts. An optional loopback proxy is temporary.
set -euo pipefail
if [ -n "$(docker ps -q)" ]; then
  echo 'Existing containers detected; do not restart Docker for image download.' >&2
  exit 2
fi
proxy_file=/etc/systemd/system/docker.service.d/jingjing-download-proxy.conf
cleanup() {
  if [ "${proxy_added:-0}" = 1 ]; then
    rm -f -- "$proxy_file"
    systemctl daemon-reload
    systemctl restart docker
  fi
}
trap cleanup EXIT
if [ -n "${JINGJING_IMAGE_PROXY:-}" ]; then
  case "$JINGJING_IMAGE_PROXY" in
    http://127.0.0.1:[0-9]*) ;;
    *) echo 'Image proxy must be a loopback HTTP endpoint.' >&2; exit 2;;
  esac
  test ! -e "$proxy_file"
  install -d /etc/systemd/system/docker.service.d
  printf '[Service]\nEnvironment="HTTP_PROXY=%s" "HTTPS_PROXY=%s" "NO_PROXY=localhost,127.0.0.1"\n' \
    "$JINGJING_IMAGE_PROXY" "$JINGJING_IMAGE_PROXY" > "$proxy_file"
  proxy_added=1
  systemctl daemon-reload
  systemctl restart docker
fi
astrbot_image='soulter/astrbot:v4.29.3@sha256:c93a6d19507ba45474c3dfb82aad9d36eb432a478112f13c7e6325b5ebffc5f2'
snowluma_image='motricseven7/snowluma:v2.2.0@sha256:34b4ec03738a698231cbad39c5650747330f5a96d953494fb6c12e1273974c0b'
docker pull "$astrbot_image" > /tmp/jingjing-astrbot-pull.log 2>&1 &
astrbot_pid=$!
docker pull "$snowluma_image" > /tmp/jingjing-snowluma-pull.log 2>&1 &
snowluma_pid=$!
astrbot_status=0
snowluma_status=0
wait "$astrbot_pid" || astrbot_status=$?
wait "$snowluma_pid" || snowluma_status=$?
if [ "$astrbot_status" -ne 0 ] || [ "$snowluma_status" -ne 0 ]; then
  tail -n 8 /tmp/jingjing-astrbot-pull.log /tmp/jingjing-snowluma-pull.log
  exit 1
fi
docker image inspect --format '{{.RepoTags}} {{.Id}}' "$astrbot_image" "$snowluma_image"
