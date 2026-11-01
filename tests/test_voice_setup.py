# coding: utf-8
"""
voice_setup_lib 的离线单元测试（不接触网络、不接触真实 Key）

覆盖复核清单与独立验收要求：
- P0-01  面板数组形状解析、按 id 匹配、绑定核对、停用/异签名拒绝覆盖；
- P0-03  ASR 创建体自带 apikey_gate（先门后代理）；
- P0-04  写响应 ok/warnings 解析（warnings 阻断）、部署后面板当前版本核对；
- P0-05  人用网关前置条件（enabled / exempt）；
- P1-11  deploy_verdict 末次终态优先；
- R02    current 才是已部署版本（latest 不可信）；Caddy 重载失败独立阻断；
- R05    输出脱敏；
- R08    登录墙 301/302 必须指向登录门户。
"""

import json
import os
import pathlib
import sys
import unittest
from unittest import mock

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
        body = lib.pipeline_body("voice-asr", "voice", 8111, "/tmp/capswriter-endpoint",
                                 "ASR", "faiz.dpdns.org", ["/*"])
        self.assertEqual(body["slug"], "voice-asr")
        self.assertEqual(body["env"], "prod")  # pid = voice-asr-prod
        self.assertEqual(body["domains"], [{"root": "faiz.dpdns.org", "sub": "voice", "bare": False}])
        self.assertEqual(body["apikey_gate"], {"enabled": True, "paths": ["/*"]})
        self.assertEqual(body["build_args"],
                         {"ENDPOINT_ROLE": "asr", "LISTEN": "8111"})
        self.assertEqual(body['container_volumes'], ['/tmp/capswriter-endpoint:/capswriter-endpoint:ro'])

    def test_admin_pipeline_has_no_gate(self):
        body = lib.pipeline_body("voice-admin", "voice-admin", 8112, "/tmp/capswriter-endpoint",
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

    def test_gate_verify_passes_when_config_intact(self):
        gate = {"enabled": True, "domains": ["nav.faiz.dpdns.org", "voice-admin.faiz.dpdns.org"],
                "exempt": {}}
        result = lib.gate_verify(gate, "voice-admin.faiz.dpdns.org")
        self.assertTrue(result["enabled"])
        self.assertTrue(result["admin_domain_present"])
        self.assertFalse(result["admin_domain_exempt"])

    def test_gate_verify_flags_missing_domain_or_disabled(self):
        gate = {"enabled": True, "domains": [], "exempt": {}}
        self.assertFalse(lib.gate_verify(gate, "voice-admin.faiz.dpdns.org")["admin_domain_present"])
        self.assertFalse(lib.gate_verify({"enabled": False, "domains": ["voice-admin.faiz.dpdns.org"]},
                                         "voice-admin.faiz.dpdns.org")["enabled"])
        exempt = {"enabled": True, "domains": ["voice-admin.faiz.dpdns.org"],
                  "exempt": {"voice-admin.faiz.dpdns.org": ["/"]}}
        self.assertTrue(lib.gate_verify(exempt, "voice-admin.faiz.dpdns.org")["admin_domain_exempt"])


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

    def test_warning_text_redacted(self):
        """warnings 原样展示前先脱敏（R05）"""
        with self.assertRaises(lib.SetupError) as ctx:
            lib.check_write_response(
                '{"ok": true, "warnings": ["authelia: X-API-Key: dpk-fake-secret-999 拒绝"]}', "建站")
        self.assertNotIn('dpk-fake-secret-999', str(ctx.exception))
        self.assertIn('<redacted>', str(ctx.exception))


class CurrentVersionTests(unittest.TestCase):
    """R02：current 才是已部署版本；latest 只是最新上传目录，不可作为部署证据"""

    def test_current_version_read(self):
        entry = _panel_list_entry("voice-asr-prod", "voice.faiz.dpdns.org",
                                  current="20260929-150000", latest="20260929-160000")
        self.assertEqual(lib.pipeline_current_version(entry, "voice-asr-prod"), "20260929-150000")

    def test_latest_new_current_old_returns_old_current(self):
        """latest 新 / current 旧 → 只认 current（返回旧值，由部署侧比对出不一致并阻断）"""
        entry = _panel_list_entry("voice-asr-prod", "voice.faiz.dpdns.org",
                                  current="20260101-000000", latest="20260929-160000")
        self.assertEqual(lib.pipeline_current_version(entry, "voice-asr-prod"),
                         "20260101-000000")

    def test_latest_new_current_missing_blocks(self):
        entry = _panel_list_entry("voice-asr-prod", "voice.faiz.dpdns.org",
                                  latest="20260929-160000")
        with self.assertRaises(lib.SetupError):
            lib.pipeline_current_version(entry, "voice-asr-prod")

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


class DeployHealthTests(unittest.TestCase):
    """R02：Caddy 重载失败是独立阻断信号，不能被后续成功标记覆盖"""

    PANEL_CONTRACT_LOG = (
        "构建中…\n"
        "⚠️  部署本身完成了，但 Caddy 重载失败：exit 1\n"
        "   路由可能没更新。手动执行：\n"
        "   docker exec caddy caddy reload --config /etc/caddy/Caddyfile\n"
        "===== deploy 成功 =====\n"
    )

    def test_caddy_reload_failure_beats_success_marker(self):
        """现场真实契约：重载失败后日志仍以「===== deploy 成功 =====」收尾"""
        self.assertEqual(lib.deploy_health(self.PANEL_CONTRACT_LOG), "caddy_reload_failed")

    def test_plain_success_stays_success(self):
        self.assertEqual(lib.deploy_health("构建中…\n===== deploy 成功 ====="), "success")

    def test_plain_failure_stays_failed(self):
        self.assertEqual(lib.deploy_health("构建中…\n!!!!! 失败：镜像构建失败"), "failed")

    def test_running_untouched(self):
        self.assertEqual(lib.deploy_health("正在构建…"), "running")

    def test_case_insensitive_marker(self):
        self.assertEqual(lib.deploy_health("Caddy Reload Failed: boom\n===== deploy 成功 ====="),
                         "caddy_reload_failed")


class RedactTests(unittest.TestCase):
    """R05/A03：到达 stdout/stderr 的文本先脱敏——含 JSON 字段与已知 Key"""

    MARK = 'dpk-fake-secret-123456'

    def test_redacts_api_key_header(self):
        out = lib.redact_secrets("upstream replied X-API-Key: dpk-fake-secret-123456")
        self.assertNotIn('dpk-fake-secret-123456', out)
        self.assertIn('<redacted>', out)

    def test_redacts_query_and_bearer(self):
        self.assertNotIn('secret123456', lib.redact_secrets('https://x/?api_key=secret12345678'))
        self.assertNotIn('abc123456789', lib.redact_secrets('Authorization: Bearer abc123456789'))
        self.assertNotIn('tok12345678', lib.redact_secrets('wss://x/?token=tok12345678'))

    def test_redacts_json_field_forms(self):
        """A03 反例：JSON/字面量形式的敏感字段必须打码"""
        for text in (
            '{"api_key": "dpk-fake-secret-123456"}',
            '{"X-API-Key": "dpk-fake-secret-123456"}',
            "{'server_token': 'dpk-fake-secret-123456'}",
            "error for api_key = 'dpk-fake-secret-123456' in panel",
        ):
            out = lib.redact_secrets(text)
            self.assertNotIn(self.MARK, out, f'JSON 形式未脱敏: {text!r} -> {out!r}')
            self.assertIn('<redacted>', out)

    def test_redacts_known_secret_exact_value(self):
        """A03：本次已知 Key 做精确替换（经环境变量，不经 argv）"""
        with unittest.mock.patch.dict(os.environ, {'PANEL_KEY': 'dpk-live-value-987654'}):
            out = lib.redact_secrets('deploy 用 dpk-live-value-987654 失败')
        self.assertNotIn('dpk-live-value-987654', out)
        self.assertIn('<redacted>', out)

    def test_safe_summary_redacts_before_truncation(self):
        """A03：清洗先于截断——原始截断点切开敏感值也不能漏出片段"""
        # 构造：假 Key 在原始 JSON 中恰跨越 200 字节截断点
        # （旧实现先 [:200] 再正则，会留下不完整但可拼回的片段）
        head = '{"ok": false, "pad": "'
        marker_start = len(head) + 150 + len('"api_key": "')  # < 200
        payload = head + 'x' * 150 + '"api_key": "' + self.MARK + '"}'
        self.assertLess(marker_start, 200, '测试构造失败：标记必须跨越截断点')
        self.assertGreater(marker_start + len(self.MARK), 200)
        out = lib.safe_summary(payload)
        self.assertNotIn('dpk-fake', out, '截断后的敏感值片段泄漏')
        # 短载荷：脱敏结果落在窗口内，应能看到 <redacted>
        short = '{"ok": false, "api_key": "' + self.MARK + '"}'
        self.assertIn('<redacted>', lib.safe_summary(short))
        self.assertNotIn(self.MARK, lib.safe_summary(short))

    def test_safe_summary_fixed_text_when_redaction_fails(self):
        """A03：清洗失败（已知 Key 残留）输出固定摘要，不回传原文"""
        with mock.patch.dict(os.environ, {'PANEL_KEY': 'dpk-live-value-987654'}), \
             mock.patch.object(lib, 'redact_secrets', side_effect=lambda t: t):
            out = lib.safe_summary('raw with dpk-live-value-987654 inside')
        self.assertNotIn('dpk-live-value-987654', out)
        self.assertIn('已省略', out)

    def test_keeps_normal_text(self):
        self.assertEqual(lib.redact_secrets("普通部署日志，不含凭据"), "普通部署日志，不含凭据")

    def test_short_values_untouched(self):
        """过短片段（如 id、路径）不属于密钥样式，不打码"""
        self.assertEqual(lib.redact_secrets("app-voice-asr-prod Up"), "app-voice-asr-prod Up")

    def test_check_write_response_error_redacts_json_fields(self):
        """A03 反例：错误响应体里的 api_key 字段不得原样进入异常文本"""
        with unittest.mock.patch.dict(os.environ, {'PANEL_KEY': ''}):
            with self.assertRaises(lib.SetupError) as ctx:
                lib.check_write_response(
                    '{"ok": false, "api_key": "dpk-fake-secret-123456"}', "部署")
        self.assertNotIn('dpk-fake-secret-123456', str(ctx.exception))
        self.assertIn('<redacted>', str(ctx.exception))


class CheckAuthPortalTests(unittest.TestCase):
    """R08：301/302 必须指向登录门户 auth.<根域名>，任意应用跳转不算被拦"""

    ADMIN = 'voice-admin.faiz.dpdns.org'
    PORTAL = f'http://auth.faiz.dpdns.org/?rd=%2F'

    def test_portal_redirect_accepted(self):
        self.assertEqual(lib.check_auth_portal('302', self.PORTAL, self.ADMIN), 'auth.faiz.dpdns.org')
        self.assertEqual(lib.check_auth_portal('301', 'https://auth.faiz.dpdns.org/#x', self.ADMIN),
                         'auth.faiz.dpdns.org')

    def test_app_redirect_rejected(self):
        with self.assertRaises(lib.SetupError):
            lib.check_auth_portal('301', f'http://{self.ADMIN}/login', self.ADMIN)

    def test_redirect_to_other_host_rejected(self):
        with self.assertRaises(lib.SetupError):
            lib.check_auth_portal('302', 'http://evil.example.com/', self.ADMIN)

    def test_missing_location_rejected(self):
        with self.assertRaises(lib.SetupError):
            lib.check_auth_portal('302', '', self.ADMIN)

    def test_non_redirect_rejected(self):
        for code in ('200', '401', '403', '500', ''):
            with self.assertRaises(lib.SetupError):
                lib.check_auth_portal(code, self.PORTAL, self.ADMIN)

    def test_wrong_root_portal_rejected(self):
        with self.assertRaises(lib.SetupError):
            lib.check_auth_portal('302', 'http://auth.other.example/', self.ADMIN)


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

    def test_lock_cleanup_only_for_owner(self):
        """R04：只有取得锁的实例才清理锁（OWNED_LOCK 门控），信号同样走清理"""
        self.assertIn('OWNED_LOCK=1', self.text)
        owner_pos = self.text.find('OWNED_LOCK=1')
        mkdir_pos = self.text.find('mkdir "$LOCK_DIR"')
        self.assertLess(mkdir_pos, owner_pos, 'OWNED_LOCK 必须在成功取得锁之后置位')
        self.assertIn("[ \"$OWNED_LOCK\" = \"1\" ]", self.text)
        self.assertIn("trap 'exit 2' INT TERM", self.text)
        # 未取得锁的分支先退出，cleanup 时不满足 OWNED_LOCK=1
        self.assertIn('OWNED_LOCK=""', self.text)

    def test_r01_all_panel_gets_authenticated(self):
        """R01：面板读取统一走带 Key 的 api_get；裸 curl 只允许用于上传/部署/探测
        阶段九：认证头经 stdin（key_hdr | curl -H @-）注入，Key 不进 argv"""
        self.assertIn('api_get() {', self.text)
        self.assertIn('get_pipelines() { api_get /api/pipelines; }', self.text)
        self.assertIn('api_get /api/gate', self.text)
        # api_get 封装必须带认证（stdin 头注入统一入口，绝不展开进 argv）
        api_get_body = self.text[self.text.find('api_get() {'):self.text.find('api_get() {') + 400]
        self.assertIn('key_hdr | $CURL -H @-', api_get_body)
        self.assertNotIn('X-API-Key: $PANEL_KEY', api_get_body,
                         'api_get 不得把 Key 展开进 curl argv')

    def test_r02_version_check_uses_current_and_image(self):
        self.assertIn('check-current-version', self.text)
        self.assertIn("{{.Image}}", self.text)
        self.assertIn('app-$pid:$ver', self.text)

    def test_r02_deploy_health_not_bare_verdict(self):
        self.assertIn('deploy-health', self.text)

    def test_r05_summary_output_redacted(self):
        """R05/A03：失败摘要输出必须经 redact 过滤，且没有 `|| cat` 原文降级"""
        self.assertIn('| redact', self.text)
        self.assertIn('redact() { python3 "$LIB" redact -; }', self.text)
        self.assertNotIn('|| cat', self.text, '脱敏不得有回传原文的降级路径')

    def test_a04_dependency_check_before_http(self):
        """A04：任何网络请求之前必须核验 curl/docker/python3 存在"""
        check_pos = self.text.find('for _tool in curl docker python3')
        first_http = self.text.find('$CURL')
        self.assertTrue(0 < check_pos < first_http, '依赖检查必须先于第一个 HTTP 调用')

    def test_r08_portal_check_in_probe_and_final(self):
        self.assertIn('check-portal', self.text)
        self.assertEqual(self.text.count('check-portal'), 2, '第 3 步与第 5 步各核验一次登录门户')

    def test_r02_gate_verify_after_deploy(self):
        self.assertIn('gate-verify', self.text)

    def test_curl_timeouts(self):
        self.assertIn('--connect-timeout 2 -m 8', self.text)

    def test_no_unbound_gate_vars(self):
        """P0-02：gate-body 调用不得在 set -u 下展开未赋值变量"""
        self.assertIn('gate-body "$TMP_DIR/gate-before.json" "$ADMIN_DOMAIN"', self.text)
        self.assertNotIn('ASR_PID=python3', self.text)

    def test_no_key_in_file(self):
        self.assertNotIn('dpk_', self.text.replace('dpk_xxx', ''))


if __name__ == '__main__':
    unittest.main()
