# coding: utf-8
"""
voice-setup.sh 部署逻辑的离线单测（不接触网络、不接触真实 Key）

覆盖执行文档 §6.2.1 要求：
- apikey_gates 请求体是列表形状且只含目标流水线（重复运行不覆盖其他站点）；
- 两条流水线分别绑定 voice.* 与 voice-admin.* 子域（ASR 子域独立于 slug）；
- 部署成功/失败/未结束的判定与失败返回码路径；
- domains 从现值合并追加，幂等。
"""

import json
import os
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'deploy'))

import voice_setup_lib as lib  # noqa: E402


class PipelineBodyTests(unittest.TestCase):
    def test_asr_pipeline_binds_voice_subdomain_not_slug(self):
        """复核 #3：流水线 id 用 slug（voice-asr→voice-asr-prod），子域独立绑 voice"""
        body = lib.pipeline_body("voice-asr", "voice", 8111, "192.168.0.104:6016",
                                 "ASR", "faiz.dpdns.org")
        self.assertEqual(body["slug"], "voice-asr")
        self.assertEqual(body["env"], "prod")  # pid = voice-asr-prod
        self.assertEqual(body["domains"], [{"root": "faiz.dpdns.org", "sub": "voice", "bare": False}])
        self.assertEqual(body["type"], "api")
        self.assertEqual(body["port"], 8111)
        self.assertEqual(body["build_args"],
                         {"UPSTREAM": "192.168.0.104:6016", "LISTEN": "8111"})

    def test_admin_pipeline_binds_voice_admin(self):
        body = lib.pipeline_body("voice-admin", "voice-admin", 8112, "192.168.0.104:6017",
                                 "管理", "faiz.dpdns.org")
        self.assertEqual(body["domains"][0]["sub"], "voice-admin")
        self.assertEqual(body["build_args"]["UPSTREAM"], "192.168.0.104:6017")

    def test_empty_slug_rejected(self):
        with self.assertRaises(ValueError):
            lib.pipeline_body("", "voice", 8111, "x", "n", "d")


class GateBodyTests(unittest.TestCase):
    def test_apikey_gates_is_list_shape(self):
        """复核 #2：apikey_gates 必须是列表 [{"pid","enabled","paths"}]"""
        current = {"enabled": True, "hosts": ["deploy"], "domains": ["nav.faiz.dpdns.org"]}
        body = lib.gate_body(current, "voice-asr-prod", "voice-admin.faiz.dpdns.org", ["/*"])
        gates = body["apikey_gates"]
        self.assertIsInstance(gates, list)
        self.assertEqual(gates, [
            {"pid": "voice-asr-prod", "enabled": True, "paths": ["/*"]},
        ])

    def test_only_target_pipeline_touched(self):
        """重复运行不会覆盖其他站点的机器门：请求体只含目标流水线"""
        current = {"domains": []}
        body1 = lib.gate_body(current, "voice-asr-prod", "voice-admin.faiz.dpdns.org", ["/*"])
        self.assertEqual(len(body1["apikey_gates"]), 1)
        # 再跑一次（模拟 GET 返回同样的现值）
        body2 = lib.gate_body(current, "voice-asr-prod", "voice-admin.faiz.dpdns.org", ["/*"])
        self.assertEqual(body1["apikey_gates"], body2["apikey_gates"])

    def test_domains_merged_from_current_value(self):
        """domains 必须从 GET 现值合并追加，不凭空构造整表"""
        current = {"domains": ["nav.faiz.dpdns.org", "wb-api.faiz.dpdns.org"]}
        body = lib.gate_body(current, "voice-asr-prod", "voice-admin.faiz.dpdns.org", ["/*"])
        self.assertEqual(body["domains"],
                         ["nav.faiz.dpdns.org", "wb-api.faiz.dpdns.org", "voice-admin.faiz.dpdns.org"])

    def test_domains_merge_idempotent(self):
        current = {"domains": ["voice-admin.faiz.dpdns.org"]}
        body = lib.gate_body(current, "voice-asr-prod", "voice-admin.faiz.dpdns.org", ["/*"])
        self.assertEqual(body["domains"], ["voice-admin.faiz.dpdns.org"])

    def test_accepts_raw_json_string(self):
        body = lib.gate_body('{"domains": ["a.x"]}', "p", "b.x", ["/*"])
        self.assertEqual(body["domains"], ["a.x", "b.x"])

    def test_corrupt_gate_json_rejected(self):
        with self.assertRaises(Exception):
            lib.gate_body("{not-json", "p", "b.x", ["/*"])


class PickPanelIpTests(unittest.TestCase):
    """复核 #1：多网络 IP 逐个探测，选可用者；不再把无分隔拼接串当单个 IP"""

    def test_picks_first_reachable(self):
        candidates = "172.18.0.3\n172.19.0.13\n"
        tried = []

        def probe(ip):
            tried.append(ip)
            return len(tried) >= 2  # 第一个不通，第二个通

        self.assertEqual(lib.pick_panel_ip(candidates, probe), "172.19.0.13")
        self.assertEqual(tried, ["172.18.0.3", "172.19.0.13"])

    def test_returns_none_when_all_fail(self):
        self.assertIsNone(lib.pick_panel_ip("172.18.0.3\n", lambda ip: False))
        self.assertIsNone(lib.pick_panel_ip("", lambda ip: True))

    def test_skips_blank_lines(self):
        self.assertEqual(lib.pick_panel_ip("\n172.20.0.5\n\n", lambda ip: True), "172.20.0.5")


class DeployVerdictTests(unittest.TestCase):
    """复核 #4：面板对成功/失败都返回 done=true，必须按收尾标记区分"""

    def test_success_marker(self):
        self.assertEqual(lib.deploy_verdict("构建中…\n===== deploy 成功 ====="), "success")

    def test_failure_marker(self):
        self.assertEqual(lib.deploy_verdict("构建中…\n!!!!! 失败：模型拉取失败"), "failed")

    def test_running_when_no_marker(self):
        self.assertEqual(lib.deploy_verdict("正在构建… done=true 但没有收尾标记"), "running")
        self.assertEqual(lib.deploy_verdict(""), "running")

    def test_success_marker_wins_when_both_present(self):
        # 正常日志不会同时出现；防御性：成功标记优先于历史失败行
        text = "!!!!! 失败：旧尝试\n===== deploy 成功 ====="
        self.assertEqual(lib.deploy_verdict(text), "success")


class BashScriptContractTests(unittest.TestCase):
    """静态检查 voice-setup.sh：关键修法必须落在脚本里"""

    @classmethod
    def setUpClass(cls):
        script = pathlib.Path(__file__).resolve().parents[1] / 'deploy' / 'voice-setup.sh'
        cls.text = script.read_text(encoding='utf-8')

    def test_uses_lib_for_gate_body(self):
        self.assertIn('gate-body', self.text)
        self.assertIn('voice_setup_lib.py', self.text)

    def test_probes_each_panel_ip(self):
        self.assertIn('println .IPAddress', self.text)
        self.assertIn('/api/me', self.text)

    def test_asr_subdomain_is_voice(self):
        self.assertIn('create_pipe "voice-asr" "voice"', self.text)

    def test_deploy_failure_exits_before_gate_config(self):
        verdict_pos = self.text.find('3/5')
        gate_pos = self.text.find('4/5')
        fail_exit = self.text.find('exit 1', verdict_pos)
        self.assertTrue(0 < fail_exit < gate_pos,
                        "部署失败分支必须在网关配置之前非零退出")

    def test_no_key_in_file(self):
        self.assertNotIn('dpk_', self.text.replace('dpk_xxx', ''))


if __name__ == '__main__':
    unittest.main()
