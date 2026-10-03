# coding: utf-8
"""
录音输出静音的打包守护测试

打包事实（installer/stage_payload.py）：
- 客户端源码按目录白名单整树复制（CORE_SUBDIRS 含 'client'），
  core/client/audio/output_mute.py 自动随包分发，无需登记文件清单；
- 真实后端只用 ctypes（标准库），零第三方依赖——requirements-client.txt
  与 build-client.spec 无需新增收集项。

本测试守护这两个前提：若有人给 output_mute.py 引入第三方依赖或挪动
位置，必须同步检查打包规格，测试立即失败提醒。
"""

import ast
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "core/client/audio/output_mute.py"
STAGE_PAYLOAD_PATH = ROOT / "installer/stage_payload.py"


class OutputMutePackagingTests(unittest.TestCase):
    def test_module_exists_in_client_source_tree(self):
        self.assertTrue(MODULE_PATH.is_file(),
                        'core/client/audio/output_mute.py 必须存在于客户端源码树')

    def test_module_is_stdlib_only(self):
        """零第三方依赖：任何新 import 都必须停留在标准库范围内"""
        source = MODULE_PATH.read_text(encoding='utf-8')
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split('.')[0])
        non_stdlib = sorted(
            name for name in imported
            if name not in getattr(sys, 'stdlib_module_names', frozenset())
        )
        self.assertEqual(non_stdlib, [],
                         'output_mute.py 出现非标准库依赖，请检查 requirements-client.txt '
                         '与 build-client.spec 是否需要同步收集')

    def test_payload_staging_copies_whole_client_tree(self):
        """payload 白名单按目录整树复制 core/client，新模块自动随包"""
        tree = ast.parse(STAGE_PAYLOAD_PATH.read_text(encoding='utf-8'))
        core_subdirs = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id == 'CORE_SUBDIRS':
                    core_subdirs = ast.literal_eval(node.value)
        self.assertIsNotNone(core_subdirs, 'stage_payload.py 缺少 CORE_SUBDIRS 定义')
        self.assertIn('client', core_subdirs)
