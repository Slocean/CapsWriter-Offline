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
| `voice-asr-prod` | 语音 WebSocket | `ENDPOINT_ROLE=asr` `LISTEN=8111` | `8111` | ASR `start_server.py` |
| `voice-admin-prod` | 网页管理 | `ENDPOINT_ROLE=admin` `LISTEN=8112` | `8112` | `python -m admin` |

- Caddy 自动转发 WebSocket Upgrade；`flush_interval -1` 保证音频流低延迟。
- 打包上传：把本目录打成 zip（Dockerfile 在压缩包根目录）。
- 两个 Windows 进程分别在 ASR 工作目录的 `gateway-endpoint/` 发布实时地址；
  面板将该目录只读挂载到容器。网关探测候选地址并重新加载配置，状态文件
  超过 12 秒未更新或时间超前超过 5 秒时停止转发。ASR 与管理服务的状态文件相互独立。
- 部署脚本从唯一运行中的 `start_server.exe` 定位目录，也可用
  `UPSTREAM_STATE_DIR` 指定无空格的 WSL 绝对目录。
- 以管理员运行 `deploy/install_gateway_firewall.ps1 -AppDir <服务目录>`，
  安装防火墙同步任务；可先加 `-CheckOnly` 只做预检。任务根据当前本机地址、
  监听端口与进程身份更新专用规则，不放宽现有管理端来源限制。

## 打包

```powershell
Compress-Archive -Path deploy/voice-gateway/Dockerfile, deploy/voice-gateway/Caddyfile.template, deploy/voice-gateway/entrypoint.sh -DestinationPath deploy/voice-gateway.zip -Force
```

注意：两条流水线上传的是同一个 zip，区别在面板流水线的「构建参数」。

## 配置执行

面板登记、部署与机器门配置用 `deploy/voice-setup.sh`（在服务器 WSL 运行，
`PANEL_KEY=dpk_xxx bash voice-setup.sh`）。脚本依赖同目录的
`voice_setup_lib.py`，两个文件一起分发。

脚本状态（2026-09-30）：独立验收 R01–R14 与第二轮 A01–A05 缺陷已修复——面板
GET 统一带 Key、部署核对只认 current 且 Caddy 重载失败独立阻断、并发锁
ownership、输出脱敏（含 JSON 字段与已知 Key，无原文降级）、登录门户校验、
依赖工具在任何 HTTP 前自检等；`tests/test_voice_setup_e2e.py` 全部场景与
全目录测试原样通过（见交付记录 §7）。**脚本级模拟通过仍不等于现场可用**：
真实面板上的首次部署、真实 WS 握手/录音、管理端上线与便携包验收仍未执行，
现场执行前须对本轮修复做复核（见 `docs/单密钥接入修复独立验收-2026-09-30.md` §6 与
`docs/单密钥接入第二轮修复验收-2026-09-30.md` §6）。
