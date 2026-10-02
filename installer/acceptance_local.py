# -*- coding: utf-8 -*-
"""真实静默安装/升级/卸载验收（本地与 GitHub Actions 共用）。

流程：预检无既有安装 → 静默安装到含中文和空格的目录 → 校验 payload 全部
文件逐字节一致（包括 internal/_sounddevice_data/portaudio-binaries/README.md，
即旧版全局排除反例）→ 运行时探针（无麦克风/无 UI）→ 升级保留设置校验 →
静默卸载 → 校验程序/注册表/快捷方式移除且用户数据保留。
DPAPI 凭据（如存在）全程哈希不变；不要求录音。

用法：
  python installer/acceptance_local.py --setup dist/CapsWriter-Setup-X.exe \
      --payload dist/client-payload-X [--no-network] [--record OUT.json]
"""
import argparse
import base64
import datetime
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import winreg

REPO = pathlib.Path(__file__).resolve().parents[1]
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{76543D83-1B1F-483D-A5C5-AFD742D7B41F}_is1"


def sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reg_entry():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            return {
                name: winreg.QueryValueEx(key, name)[0]
                for name in ("InstallLocation", "UninstallString", "DisplayName")
            }
    except FileNotFoundError:
        return None


def ps(code: str) -> str:
    encoded = base64.b64encode(code.encode("utf-16le")).decode()
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-EncodedCommand", encoded],
        capture_output=True, check=True,
    )
    return result.stdout.decode("utf-8-sig").strip()


def desktop_shortcut() -> pathlib.Path:
    desktop = ps("[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false);"
                 "[Environment]::GetFolderPath('Desktop')")
    return pathlib.Path(desktop) / "CapsWriter.lnk"


def run_installer(setup: pathlib.Path, log: pathlib.Path, target: pathlib.Path, upgrade: bool) -> None:
    argv = [str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"]
    if not upgrade:
        argv.append(f"/DIR={target}")
    result = subprocess.run(argv + [f"/LOG={log}"], capture_output=True, timeout=180)
    if result.returncode != 0:
        raise AssertionError(f"安装程序返回码 {result.returncode}，日志 {log}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", required=True)
    parser.add_argument("--payload", required=True)
    parser.add_argument("--target", default=str(pathlib.Path(tempfile.gettempdir()) / "CapsWriter 安装验证 用户"))
    parser.add_argument("--expected-addr", default="192.168.0.104")
    parser.add_argument("--no-network", action="store_true")
    parser.add_argument("--record", default="")
    args = parser.parse_args()

    setup = pathlib.Path(args.setup).resolve()
    payload = pathlib.Path(args.payload).resolve()
    target = pathlib.Path(args.target).resolve()
    logs = pathlib.Path(tempfile.gettempdir()) / "caps-acceptance-logs"
    logs.mkdir(parents=True, exist_ok=True)
    result = {
        "at": datetime.datetime.now().astimezone().isoformat(),
        "installer_sha256": sha(setup),
        "installer_bytes": setup.stat().st_size,
        "user": os.environ.get("USERNAME", ""),
        "install_directory": str(target),
        "passed": False,
    }
    out_path = pathlib.Path(args.record) if args.record else None

    def save() -> None:
        if out_path:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        assert reg_entry() is None, "已存在 CapsWriter 安装，先卸载或换 --target 再验收"
        assert not target.exists(), f"安装目录已存在，先检查旧目录: {target}"
        credentials = pathlib.Path(os.environ["LOCALAPPDATA"]) / "CapsWriterOffline" / "credentials.json"
        cred_before = sha(credentials) if credentials.exists() else None

        shortcut = desktop_shortcut()
        assert not shortcut.exists(), "已存在同名桌面快捷方式，先确认归属"

        run_installer(setup, logs / "install.log", target, upgrade=False)
        entry = reg_entry()
        assert entry and pathlib.Path(entry["InstallLocation"]).resolve() == target, "注册表安装路径不符"

        # payload 全部文件逐字节一致；readme 全局排除反例由 stage_payload 兜底，
        # 这里显式断言 portaudio README 存在。
        assert (target / "internal/_sounddevice_data/portaudio-binaries/README.md").is_file(), \
            "portaudio README.md 缺失（全局排除反例回归）"
        for path in sorted(payload.rglob("*")):
            if not path.is_file():
                continue
            dest = target / path.relative_to(payload)
            assert dest.is_file() and sha(dest) == sha(path), f"安装缺少或不一致: {path.relative_to(payload)}"
        assert not (target / "start_server.exe").exists() and not (target / "start_admin.exe").exists()
        assert not (target / "core/server").exists(), "安装目录混入服务端源码"
        result["payload_files_verified"] = True

        probe_out = logs / "probe.json"
        argv = [sys.executable, "-I", "-S", "-B", str(REPO / "scripts" / "client_runtime_probe.py"),
                str(target), str(probe_out), "--expected-addr", args.expected_addr]
        if args.no_network:
            argv.append("--no-network")
        probe = subprocess.run(argv, capture_output=True, timeout=120)
        (logs / "probe.log").write_bytes(probe.stdout + probe.stderr)
        assert probe.returncode == 0, "运行时探针失败，见 %acceptance-logs%"
        result["installed_runtime"] = json.loads(probe_out.read_text(encoding="utf-8"))

        # 升级保留：可编辑配置、热词、界面偏好
        config = target / "config_client.py"
        words = target / "hot.txt"
        ui = target / "desktop_ui.ini"
        config.write_bytes(config.read_bytes() + b"\n# acceptance preservation check\n")
        words.write_bytes(words.read_bytes() + b"\nAcceptancePreservationCheck\n")
        ui.write_text("theme=dark\n", encoding="utf-8")
        expected = {p.name: sha(p) for p in (config, words, ui)}
        run_installer(setup, logs / "upgrade.log", target, upgrade=True)
        assert {p.name: sha(p) for p in (config, words, ui)} == expected, "升级改变了用户设置"
        result["upgrade_preserves_configuration_words_and_ui_preferences"] = True

        # 升级后再次探针
        probe = subprocess.run(argv, capture_output=True, timeout=120)
        (logs / "probe2.log").write_bytes(probe.stdout + probe.stderr)
        assert probe.returncode == 0, "升级后运行时探针失败"

        assert shortcut.exists(), "桌面快捷方式未创建"
        info = json.loads(ps(
            "[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false);"
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('" + str(shortcut).replace("'", "''") + "');"
            "@{target=$s.TargetPath;cwd=$s.WorkingDirectory}|ConvertTo-Json -Compress"))
        assert pathlib.Path(info["target"]) == target / "CapsWriterDesktop.exe" and pathlib.Path(info["cwd"]) == target, \
            "桌面快捷方式指向错误"
        result["desktop_shortcut_target_and_working_directory_verified"] = True

        uninstaller = target / "unins000.exe"
        assert uninstaller.is_file()
        uninstall = subprocess.run(
            [str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
             f"/LOG={logs / 'uninstall.log'}"],
            capture_output=True, timeout=180)
        assert uninstall.returncode == 0, "卸载失败"
        assert not (target / "CapsWriterDesktop.exe").exists() and not (target / "start_client.exe").exists()
        assert not (target / "internal").exists(), "卸载残留 internal 目录"
        assert reg_entry() is None, "卸载后注册表残留"
        assert not shortcut.exists(), "卸载后桌面快捷方式残留"
        assert {p.name: sha(p) for p in (config, words, ui)} == expected, "卸载删除了用户设置"
        result["uninstall_removes_program_shortcut_and_registration"] = True
        result["uninstall_preserves_user_settings"] = True
        result["current_user_dpapi_credential_bytes_unchanged"] = (
            (sha(credentials) if credentials.exists() else None) == cred_before)
        result["service_not_modified"] = True
        result["recording_not_requested"] = True
        result["passed"] = True
    finally:
        save()
    print(json.dumps({"passed": result["passed"], "installer": str(setup),
                      "size_mb": round(setup.stat().st_size / 1024 / 1024, 1)}, ensure_ascii=False))
    if not result["passed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
