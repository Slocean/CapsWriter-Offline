# CapsWriter 语音网关容器（极薄反代）

面板的流水线只能把域名代理到 `app-<id>` 容器，不能直连 Windows 上游
（`src/panel/caddy/render.py` 的 `site_block` 硬编码 `app-{pid}:{port}`）。
本目录是一个只做转发、不加载模型、不存音频、不持有密钥的极薄 Caddy 容器：

```
浏览器/客户端 → 面板 Caddy（机器门/人用网关在此） → app-voice-gateway(本容器) → Windows ASR/管理端
```

同一份源码上传两条流水线，用「构建参数」区分上游：

| 流水线 | 用途 | build_args | 对外端口 | 上游 |
| --- | --- | --- | --- | --- |
| `voice-asr-prod` | 语音 WebSocket | `UPSTREAM=192.168.0.104:6016` `LISTEN=8111` | `8111` | ASR `start_server.py` |
| `voice-admin-prod` | 网页管理 | `UPSTREAM=192.168.0.104:6017` `LISTEN=8112` | `8112` | `python -m admin` |

- Caddy 自动转发 WebSocket Upgrade；`flush_interval -1` 保证音频流低延迟。
- 打包上传：把本目录打成 zip（Dockerfile 在压缩包根目录）。
- 上游地址用局域网 IP `192.168.0.104`；容器经 WSL 出站可达（已实测 426 响应）。
  若日后 Windows IP 变化，重新以新 build_args 上传部署即可。

## 打包

```powershell
Compress-Archive -Path deploy/voice-gateway/Dockerfile, deploy/voice-gateway/Caddyfile.template -DestinationPath voice-gateway.zip -Force
```

注意：两条流水线上传的是同一个 zip，区别在面板流水线的「构建参数」。
