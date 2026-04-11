#!/bin/sh
if [ -n "$SENTRY_DSN" ]; then
    echo "window.SENTRY_DSN = '${SENTRY_DSN}';" > /usr/share/nginx/html/js/config.js
fi
exec "$@"
