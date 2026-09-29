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
Compress-Archive -Path deploy/voice-gateway/Dockerfile, deploy/voice-gateway/Caddyfile.template -DestinationPath deploy/voice-gateway.zip -Force
```

注意：两条流水线上传的是同一个 zip，区别在面板流水线的「构建参数」。

## 配置执行

面板登记、部署与机器门配置用 `deploy/voice-setup.sh`（在服务器 WSL 运行，
`PANEL_KEY=dpk_xxx bash voice-setup.sh`）。脚本依赖同目录的
`voice_setup_lib.py`，两个文件一起分发。脚本已按 2026-09-29 复核修法修复
（多网络 IP 逐个探测、机器门列表请求体、ASR 子域绑 `voice`、部署成败按
面板收尾标记判定），并通过 `tests/test_voice_setup.py` 离线单测；带真实
Key 的现场执行与真机录音验收仍未发生，勿据单元测试宣称远程已可用。
