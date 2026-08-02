#!/bin/sh
set -eu

if [ -d /opt/corporate-ca ]; then
  find /opt/corporate-ca -type f -name '*.crt' -exec cp {} /usr/local/share/ca-certificates/ \;
  update-ca-certificates >/dev/null
fi

exec "$@"
