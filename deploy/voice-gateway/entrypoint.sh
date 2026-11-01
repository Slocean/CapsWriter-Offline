#!/bin/sh
set -eu

routes=/capswriter-endpoint/routes.txt
[ "$ENDPOINT_ROLE" = admin ] && routes=/capswriter-endpoint/admin-routes.txt
template=/etc/caddy/Caddyfile.template
config=/etc/caddy/Caddyfile
current=127.0.0.1:1

render() {
    sed -e "s|__UPSTREAM__|$1|g" -e "s|__LISTEN__|$LISTEN|g" "$template" > "$config.new"
    mv "$config.new" "$config"
}

render "$current"
caddy run --config "$config" --adapter caddyfile &
caddy_pid=$!
trap 'kill "$caddy_pid" 2>/dev/null || true; wait "$caddy_pid" || true; exit 0' TERM INT

while kill -0 "$caddy_pid" 2>/dev/null; do
    target=127.0.0.1:1
    modified=$(stat -c %Y "$routes" 2>/dev/null || echo 0)
    now=$(date +%s)
    age=$((now - modified))
    if [ -r "$routes" ] && [ "$age" -ge -5 ] && [ "$age" -le 12 ]; then
        contents=$(cat "$routes" 2>/dev/null || true)
        while IFS=: read -r host port; do
            port=$(printf '%s' "$port" | tr -d '\r')
            printf '%s\n' "$host" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$' || continue
            printf '%s\n' "$port" | grep -Eq '^[0-9]{1,5}$' || continue
            [ "$port" -gt 0 ] && [ "$port" -le 65535 ] || continue
            if printf 'GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n' | \
                timeout 3 nc -w 2 "$host" "$port" >/dev/null 2>&1; then
                target="$host:$port"
                break
            fi
        done <<EOF
$contents
EOF
    fi
    if [ "$target" != "$current" ]; then
        render "$target"
        if caddy reload --config "$config" --adapter caddyfile; then
            current="$target"
            echo "网关上游已更新: $current"
        fi
    fi
    sleep 3 & wait $! || true
done
wait "$caddy_pid"
