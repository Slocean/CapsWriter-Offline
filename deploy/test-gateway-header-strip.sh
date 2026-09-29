#!/usr/bin/env bash
# 网关容器剥离 X-API-Key 的容器级验证（复核清单 P1-09）
# 在服务器 WSL 运行：假上游回显收到的请求头，经网关容器转发后必须看不到
# X-API-Key / X-Gate-Trust / X-Gate-Key。测试产物用完即清理。
set -euo pipefail
cd "$(mktemp -d)"
echo "workdir: $PWD"

mkdir -p up gw
cat > up/server.py <<'PYEOF'
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"received_headers": {k.lower(): v for k, v in self.headers.items()}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

HTTPServer(("0.0.0.0", 18999), H).serve_forever()
PYEOF

python3 -c "import zipfile; zipfile.ZipFile('/tmp/voice-gateway.zip').extractall('gw')"

# 假上游跑在 WSL host 网络；网关容器（docker_proxy 桥）经桥网关访问 WSL 宿主
UP_IP=$(docker network inspect docker_proxy --format '{{(index .IPAM.Config 0).Gateway}}')
echo "upstream expected at $UP_IP:18999 (docker_proxy bridge gateway)"
docker rm -f up-hdr vgw-hdr >/dev/null 2>&1 || true
docker run -d --rm --name up-hdr --network host \
  -v "$PWD/up/server.py:/srv/server.py:ro" \
  python:3-alpine python3 /srv/server.py >/dev/null
sleep 2
echo "--- 桥内预检（直连假上游）:"
docker run --rm --network docker_proxy python:3-alpine \
  wget -qO- -T 3 "http://$UP_IP:18999/" | head -c 120; echo

docker build -q --build-arg UPSTREAM="$UP_IP:18999" --build-arg LISTEN=8111 \
  -t vgw-hdr gw >/dev/null
docker run -d --rm --name vgw-hdr --network docker_proxy vgw-hdr >/dev/null
sleep 2

GW_IP=$(docker inspect vgw-hdr --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' | head -1)
echo "--- 经网关转发（带 X-API-Key 与机器门注入头）:"
RESULT=$(curl -s -m 5 -H "X-API-Key: dpk-fake-test-key" \
  -H "X-Gate-Trust: fake-trust-token" -H "X-Gate-Key: fake-key-name" \
  "http://$GW_IP:8111/")
echo "$RESULT"

python3 - "$RESULT" <<'PYEOF'
import json, sys
data = json.loads(sys.argv[1])
headers = data["received_headers"]
leaked = [h for h in ("x-api-key", "x-gate-trust", "x-gate-key") if h in headers]
assert not leaked, f"泄漏的请求头: {leaked}"
print("OK: 假上游未收到 X-API-Key / X-Gate-Trust / X-Gate-Key")
print("收到的头:", sorted(headers.keys()))
PYEOF

docker rm -f up-hdr vgw-hdr >/dev/null
docker rmi vgw-hdr >/dev/null
cd / && rm -rf "$OLDPWD"
echo "cleaned"
