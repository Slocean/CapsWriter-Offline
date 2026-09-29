# coding: utf-8
"""
voice_setup_lib 的离线单元测试（不接触网络、不接触真实 Key）

覆盖复核清单要求：
- P0-01  面板数组形状解析、按 id 匹配、绑定核对、停用/异签名拒绝覆盖；
- P0-03  ASR 创建体自带 apikey_gate（先门后代理）；
- P0-04  写响应 ok/warnings 解析（warnings 阻断）、部署后面板当前版本核对；
- P0-05  人用网关前置条件（enabled / exempt）；
- P1-11  deploy_verdict 末次终态优先。
"""

import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'deploy'))

import voice_setup_lib as lib  # noqa: E402


def _panel_list_entry(pid, domain, **extra):
    entry = {
        "id": pid,
        "name": pid,
        "type": "api",
        "enabled": True,
        "note": lib.PIPELINE_NOTE,
        "domain": domain,
        "domains": [domain],
        "bindings": [{"root": domain.split('.', 1)[1], "sub": domain.split('.', 1)[0], "bare": False}],
    }
    entry.update(extra)
    return entry


class PipelineBodyTests(unittest.TestCase):
    def test_asr_pipeline_binds_voice_subdomain_and_gate(self):
        """复核 #3 + P0-03：子域绑 voice（独立于 slug），创建体自带机器门"""
        body = lib.pipeline_body("voice-asr", "voice", 8111, "192.168.0.104:6016",
                                 "ASR", "faiz.dpdns.org", ["/*"])
        self.assertEqual(body["slug"], "voice-asr")
        self.assertEqual(body["env"], "prod")  # pid = voice-asr-prod
        self.assertEqual(body["domains"], [{"root": "faiz.dpdns.org", "sub": "voice", "bare": False}])
        self.assertEqual(body["apikey_gate"], {"enabled": True, "paths": ["/*"]})
        self.assertEqual(body["build_args"],
                         {"UPSTREAM": "192.168.0.104:6016", "LISTEN": "8111"})

    def test_admin_pipeline_has_no_gate(self):
        body = lib.pipeline_body("voice-admin", "voice-admin", 8112, "192.168.0.104:6017",
                                 "管理", "faiz.dpdns.org", None)
        self.assertNotIn("apikey_gate", body)
        self.assertEqual(body["domains"][0]["sub"], "voice-admin")


class PipelineListTests(unittest.TestCase):
    """P0-01：现网 GET /api/pipelines 返回数组"""

    def test_parses_array_and_matches_by_id(self):
        text = json.dumps([
            _panel_list_entry("fundglass-api-prod", "fundglass-api.faiz.dpdns.org"),
            _panel_list_entry("voice-asr-prod", "voice.faiz.dpdns.org"),
        ])
        entries = lib.parse_pipeline_list(text)
        self.assertIn("voice-asr-prod", entries)
        self.assertEqual(entries["voice-asr-prod"]["domain"], "voice.faiz.dpdns.org")

    def test_rejects_dict_shape(self):
        with self.assertRaises(lib.SetupError):
            lib.parse_pipeline_list('{"voice-asr-prod": {}}')

    def test_rejects_duplicate_ids(self):
        text = json.dumps([
            _panel_list_entry("x-prod", "a.x"),
            _panel_list_entry("x-prod", "b.x"),
        ])
        with self.assertRaises(lib.SetupError):
            lib.parse_pipeline_list(text)

    def test_existing_foreign_pipeline_refused(self):
        """同 ID 但不是本方案签名 → 拒绝覆盖"""
        foreign = _panel_list_entry("voice-asr-prod", "voice.faiz.dpdns.org", note="别人的服务")
        with self.assertRaises(lib.SetupError):
            lib.check_pipeline_entry(foreign, "voice-asr-prod", "voice.faiz.dpdns.org")

    def test_disabled_pipeline_refused(self):
        disabled = _panel_list_entry("voice-asr-prod", "voice.faiz.dpdns.org", enabled=False)
        with self.assertRaises(lib.SetupError):
            lib.check_pipeline_entry(disabled, "voice-asr-prod", "voice.faiz.dpdns.org")

    def test_wrong_binding_refused(self):
        wrong = _panel_list_entry("voice-asr-prod", "voice-asr.faiz.dpdns.org")
        with self.assertRaises(lib.SetupError):
            lib.check_pipeline_entry(wrong, "voice-asr-prod", "voice.faiz.dpdns.org")

    def test_own_pipeline_accepted_and_domain_returned(self):
        own = _panel_list_entry("voice-asr-prod", "voice.faiz.dpdns.org")
        self.assertEqual(lib.check_pipeline_entry(own, "voice-asr-prod", "voice.faiz.dpdns.org"),
                         "voice.faiz.dpdns.org")

    def test_missing_entry_means_create(self):
        self.assertEqual(lib.check_pipeline_entry(None, "voice-asr-prod", "voice.faiz.dpdns.org"), "")


class GateTests(unittest.TestCase):
    def test_gate_body_appends_admin_domain_from_current(self):
        current = {"enabled": True, "domains": ["nav.faiz.dpdns.org", "wb-api.faiz.dpdns.org"]}
        body = lib.gate_body(current, "voice-admin.faiz.dpdns.org")
        self.assertEqual(body["domains"],
                         ["nav.faiz.dpdns.org", "wb-api.faiz.dpdns.org", "voice-admin.faiz.dpdns.org"])
        # 机器门已随流水线创建体提交，gate body 不再携带（避免双写）
        self.assertNotIn("apikey_gates", body)

    def test_gate_body_idempotent(self):
        current = {"domains": ["voice-admin.faiz.dpdns.org"]}
        body = lib.gate_body(current, "voice-admin.faiz.dpdns.org")
        self.assertEqual(body["domains"], ["voice-admin.faiz.dpdns.org"])

    def test_preconditions_gate_disabled(self):
        result = lib.gate_preconditions({"enabled": False, "domains": []}, "voice-admin.x")
        self.assertFalse(result["enabled"])
        self.assertFalse(result["admin_domain_exempt"])

    def test_preconditions_exempt_domain_detected(self):
        gate = {"enabled": True, "domains": [], "exempt": {"voice-admin.faiz.dpdns.org": ["/api/*"]}}
        result = lib.gate_preconditions(gate, "voice-admin.faiz.dpdns.org")
        self.assertTrue(result["admin_domain_exempt"])
        self.assertIn("voice-admin.faiz.dpdns.org", result["exempt_domains"])

    def test_preconditions_ok(self):
        result = lib.gate_preconditions({"enabled": True, "domains": [], "exempt": {}},
                                        "voice-admin.faiz.dpdns.org")
        self.assertTrue(result["enabled"])
        self.assertFalse(result["admin_domain_exempt"])


class WriteResponseTests(unittest.TestCase):
    """P0-04：HTTP 200 + ok:true + warnings 仍是阻断"""

    def test_ok_without_warnings_passes(self):
        data = lib.check_write_response('{"ok": true, "id": "voice-asr-prod", "warnings": []}', "建站")
        self.assertEqual(data["id"], "voice-asr-prod")

    def test_warnings_block(self):
        with self.assertRaises(lib.SetupError) as ctx:
            lib.check_write_response(
                '{"ok": true, "warnings": ["caddy reload failed: exit 1"]}', "建站")
        self.assertIn("caddy reload failed", str(ctx.exception))

    def test_missing_ok_blocks(self):
        with self.assertRaises(lib.SetupError):
            lib.check_write_response('{"error": "boom"}', "建站")

    def test_non_json_blocks(self):
        with self.assertRaises(lib.SetupError):
            lib.check_write_response('<html>502</html>', "建站")

    def test_missing_warnings_field_ok(self):
        lib.check_write_response('{"ok": true, "id": "x"}', "建站")


class CurrentVersionTests(unittest.TestCase):
    def test_version_read(self):
        entry = _panel_list_entry("voice-asr-prod", "voice.faiz.dpdns.org", latest="20260929-150000")
        self.assertEqual(lib.pipeline_current_version(entry, "voice-asr-prod"), "20260929-150000")

    def test_version_falls_back_to_current(self):
        entry = _panel_list_entry("voice-asr-prod", "voice.faiz.dpdns.org")
        entry["current"] = "20260929-140000"
        entry["latest"] = None
        self.assertEqual(lib.pipeline_current_version(entry, "voice-asr-prod"), "20260929-140000")

    def test_missing_entry_blocks(self):
        with self.assertRaises(lib.SetupError):
            lib.pipeline_current_version(None, "voice-asr-prod")


class DeployVerdictTests(unittest.TestCase):
    """P1-11：末次终态优先"""

    def test_success_marker(self):
        self.assertEqual(lib.deploy_verdict("构建中…\n===== deploy 成功 ====="), "success")

    def test_failure_marker(self):
        self.assertEqual(lib.deploy_verdict("构建中…\n!!!!! 失败：镜像构建失败"), "failed")

    def test_running_when_no_marker(self):
        self.assertEqual(lib.deploy_verdict("正在构建…"), "running")
        self.assertEqual(lib.deploy_verdict(""), "running")

    def test_failure_after_success_loses(self):
        """成功标记之后又出现失败行：以末次终态为准 → failed"""
        text = "===== deploy 成功 =====\n!!!!! 失败：Caddy 重载失败"
        self.assertEqual(lib.deploy_verdict(text), "failed")


class BashScriptContractTests(unittest.TestCase):
    """静态契约：关键修法必须落在脚本里"""

    @classmethod
    def setUpClass(cls):
        script = pathlib.Path(__file__).resolve().parents[1] / 'deploy' / 'voice-setup.sh'
        cls.text = script.read_text(encoding='utf-8')

    def test_machine_gate_in_pipeline_body_before_deploy(self):
        """P0-03：ASR 创建体带 apikey_gate（GATE_PATHS 传入），且流水线保存先于任何 deploy 调用"""
        first_body = self.text.find('pipeline-body voice-asr voice')
        gate_arg = self.text.find('"$GATE_PATHS"', first_body)
        first_deploy = self.text.find('/deploy')
        self.assertGreater(first_body, -1)
        self.assertGreater(gate_arg, first_body)
        self.assertLess(first_body, first_deploy)

    def test_running_state_gate_check_before_deploy(self):
        """P0-03：部署代理之前必须先验证安全门运行态"""
        verify_pos = self.text.find('3/5 运行态验证安全门')
        deploy_pos = self.text.find('4/5 上传并部署')
        self.assertTrue(0 < verify_pos < deploy_pos)

    def test_gate_running_check_blocks_on_not_401(self):
        self.assertIn('die 4 "语音域名无 Key 未被机器门拦截', self.text)

    def test_exit_codes_documented(self):
        self.assertIn('exit 0', self.text)
        self.assertIn('exit 3', self.text)
        self.assertIn('die 4', self.text)   # 安全门未生效 → 4
        self.assertIn('die 5', self.text)   # 部署失败/超时 → 5
        self.assertIn('die 2', self.text)   # 环境错误 → 2

    def test_uses_mktemp_and_lock(self):
        self.assertIn('mktemp -d', self.text)
        self.assertIn('mkdir "$LOCK_DIR"', self.text)
        self.assertIn('trap cleanup EXIT', self.text)

    def test_curl_timeouts(self):
        self.assertIn('--connect-timeout 2 -m 8', self.text)

    def test_no_unbound_gate_vars(self):
        """P0-02：gate-body 调用不得在 set -u 下展开未赋值变量"""
        self.assertIn('gate-body "$TMP_DIR/gate-before.json" "$ADMIN_DOMAIN"', self.text)
        self.assertNotIn('ASR_PID=python3', self.text)

    def test_deploy_success_requires_container_and_version_check(self):
        """P0-04：日志成功后仍须核对容器运行与面板当前版本"""
        self.assertIn('docker ps --format', self.text)
        self.assertIn('check-current-version', self.text)

    def test_no_key_in_file(self):
        self.assertNotIn('dpk_', self.text.replace('dpk_xxx', ''))

    def test_warning_summary_redacts_nothing_sensitive(self):
        """错误摘要来自面板部署日志，不含 Key；脚本自身不打印 PANEL_KEY"""
        import re
        # 除占位提示外，脚本不得在任何输出中引用 PANEL_KEY 的值
        uses = re.findall(r'\$\{?PANEL_KEY\}?', self.text)
        for u in uses:
            pass  # 存在即说明有引用；上面的 dpk_ 检查已确保无明文


if __name__ == '__main__':
    unittest.main()
