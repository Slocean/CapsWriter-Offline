# coding: utf-8
"""
桌面端保存链路行为实测（A02/A03）：编译真实 C# 源码为程序集，经 PowerShell
反射调用实际的 ApplyConfig/ValidateUpdate/TryReadStringLiteral/
RemoteUrlValidationError——编译通过不能代替保存行为验收。

需要本机 .NET Framework csc（build.ps1 同款路径）与 powershell；缺失时跳过。
"""

import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
DESKTOP = REPO / 'desktop'

CSC = pathlib.Path('C:/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe')

PS_HARNESS = r'''
param([string]$Dll,[string]$CaseJson,[string]$OutFile)
$ErrorActionPreference='Stop'
$asm=[Reflection.Assembly]::LoadFrom($Dll)
$t=$asm.GetType('Desktop')
$flags=[Reflection.BindingFlags]'NonPublic,Static'
function Invoke([string]$name,[object[]]$vals) {
  $m=$t.GetMethod($name,$flags)
  if ($m -eq $null) { throw "method not found: $name" }
  return $m.Invoke($null,$vals)
}
$cases=Get-Content -LiteralPath $CaseJson -Raw -Encoding UTF8 | ConvertFrom-Json
$out=@()
foreach ($c in $cases) {
  switch ($c.kind) {
    'literal' {
      $arr=@([string]$c.source,[string]$c.key,$null)
      $ok=[bool](Invoke 'TryReadStringLiteral' $arr)
      $v=$arr[2]          # out 参数值从参数数组取回
      $out += @{kind='literal';name=$c.name;ok=$ok;value=$v}
    }
    'load' {
      $d=Invoke 'LoadConfigValues' @([string]$c.content)
      $out += @{kind='load';name=$c.name;addr=$d['addr'];port=$d['port'];
                server_url=$d['server_url'];context=$d['context'];
                pause_segmented=$d['pause_segmented'];pause_seconds=$d['pause_seconds']}
    }
    'apply_validate' {
      $updated=Invoke 'ApplyConfig' @($c.content,[bool]$c.remoteMode,$c.remoteUrl,$c.hostname,[uint16]$c.port,[double]$c.duration,[bool]$c.liveMode,$c.prompt)
      $err=Invoke 'ValidateUpdate' @($updated,[bool]$c.remoteMode,$c.remoteUrl,$c.hostname,[uint16]$c.port,$c.prompt)
      $arr=@([string]$updated,[string]'server_url',$null)
      [void](Invoke 'TryReadStringLiteral' $arr)
      $out += @{kind='apply_validate';name=$c.name;error=$err;server_url=$arr[2];updated=$updated}
    }
    'urlcheck' {
      $err=Invoke 'RemoteUrlValidationError' @([string]$c.url)
      $out += @{kind='urlcheck';name=$c.name;error=$err}
    }
    'entryvalue' {
      $v=Invoke 'EntryValue' @([string]$c.entry,[string]$c.keyName,[string]$c.fallback)
      $out += @{kind='entryvalue';name=$c.name;value=[string]$v}
    }
  }
}
# 结果写 UTF-8 文件：PS 控制台输出跟随本地代码页（GBK），管道解码会失败
ConvertTo-Json -InputObject @{results=$out} -Depth 4 -Compress | Out-File -FilePath $OutFile -Encoding utf8
'''


def _run_cases(cases, dll):
    import json
    with tempfile.NamedTemporaryFile('w', suffix='.ps1', delete=False, encoding='utf-8') as f:
        f.write(PS_HARNESS)
        script = f.name
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as f:
        json.dump(cases, f, ensure_ascii=False)
        cases_path = f.name
    out_path = script + '.out'
    try:
        proc = subprocess.run(
            ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
             '-File', script, '-Dll', str(dll), '-CaseJson', cases_path,
             '-OutFile', out_path],
            capture_output=True, timeout=120)
        # PS 管道输出是本地代码页（GBK），容错解码仅用于失败诊断
        stdout = (proc.stdout or b'').decode('utf-8', 'replace')
        stderr = (proc.stderr or b'').decode('utf-8', 'replace')
        if proc.returncode != 0:
            raise AssertionError(f'PowerShell 反射调用失败：rc={proc.returncode}\n{stderr}\n{stdout}')
        try:
            data = pathlib.Path(out_path).read_text(encoding='utf-8-sig')
        except OSError as e:
            raise AssertionError(f'PowerShell 未产出结果文件：{e}\n{stderr}\n{stdout}')
        return json.loads(data)['results']
    finally:
        pathlib.Path(script).unlink(missing_ok=True)
        pathlib.Path(cases_path).unlink(missing_ok=True)
        pathlib.Path(out_path).unlink(missing_ok=True)


@unittest.skipIf(not CSC.exists(), '本机无 .NET Framework csc，跳过桌面保存行为实测')
class DesktopSaveBehaviorTests(unittest.TestCase):
    """A02：模式化保存 + 严格字面量解析/序列化，用真实编译程序集验证"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dll = pathlib.Path(cls._tmp.name) / 'CapsWriterDesktop.test.dll'
        FX = CSC.parent
        args = [str(CSC), '-nologo', '-target:library', f'-out:{cls.dll}',
                f'-reference:{FX}/WPF/PresentationFramework.dll',
                f'-reference:{FX}/WPF/PresentationCore.dll',
                f'-reference:{FX}/WPF/WindowsBase.dll',
                f'-reference:{FX}/System.Xaml.dll',
                '-reference:System.Drawing.dll', '-reference:System.Security.dll',
                '-reference:System.Windows.Forms.dll',
                str(DESKTOP / 'CapsWriterDesktop.cs'), str(DESKTOP / 'Shortcuts.cs'),
                str(DESKTOP / 'Visuals.cs'), str(DESKTOP / 'Update.cs'),
                str(DESKTOP / 'AppVersion.generated.cs')]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=180)
        cls.compile_output = proc.stdout + proc.stderr
        assert proc.returncode == 0, f'C# 编译失败：{cls.compile_output}'

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _cases(self, cases):
        return _run_cases(cases, self.dll)

    def test_literal_roundtrips(self):
        """字符串字面量严格解析：# 不截断、末尾转义引号不剥、反斜杠还原"""
        saved = "'wss://voice.example.com/O\\'Reilly'"
        results = self._cases([
            {'kind': 'literal', 'name': 'quote_path', 'source': f'server_url = {saved}',
             'key': 'server_url'},
            {'kind': 'literal', 'name': 'hash_kept',
             'source': "server_url = 'wss://voice.example.com/a#fragment'", 'key': 'server_url'},
            {'kind': 'literal', 'name': 'trailing_quote',
             'source': "server_url = 'wss://voice.example.com/end\\''", 'key': 'server_url'},
            {'kind': 'literal', 'name': 'backslash',
             'source': "context = 'a\\\\b'", 'key': 'context'},
            {'kind': 'literal', 'name': 'unclosed',
             'source': "server_url = 'wss://open", 'key': 'server_url'},
        ])
        by_name = {r['name']: r for r in results}
        self.assertTrue(by_name['quote_path']['ok'])
        self.assertEqual(by_name['quote_path']['value'], 'wss://voice.example.com/O\'Reilly')
        self.assertEqual(by_name['hash_kept']['value'], 'wss://voice.example.com/a#fragment')
        self.assertEqual(by_name['trailing_quote']['value'], "wss://voice.example.com/end'")
        self.assertEqual(by_name['backslash']['value'], 'a\\b')
        self.assertFalse(by_name['unclosed']['ok'], '未闭合字面量必须解析失败')

    def test_load_reads_unquoted_addr_and_port(self):
        """A02 第二轮反例：LoadConfigValues（LoadSettings 的数据层）读
        addr/port 必须去掉引号——带引号值会让 LAN 保存的输入校验失败"""
        content = ("class ClientConfig:\r\n"
                   "    addr = '127.0.0.1'\r\n"
                   "    port = '6016'\r\n"
                   "    server_url = ''\r\n"
                   "    context = ''\r\n")
        (result,) = self._cases([{'kind': 'load', 'name': 'lan_base', 'content': content}])
        self.assertEqual(result['addr'], '127.0.0.1', 'addr 带引号即 A02 反例')
        self.assertEqual(result['port'], '6016', 'port 带引号即 A02 反例')

    def test_real_load_edit_save_chain(self):
        """
        A02 正式回归：真实 config_client.py 为起点，走「加载 → 编辑 → 保存」
        连续状态链（每一步消费上一步的真实输出，不从同一 base 重复调用），
        每一步经 ValidateUpdate，且整份配置用真实 Python 语法编译。
        """
        import tempfile
        base = (REPO / 'config_client.py').read_text(encoding='utf-8')
        compile(base, 'config_client.py', 'exec')   # 基线本身可编译

        remote_url = "wss://voice.example.com/O'Reilly"
        remote_url2 = 'wss://voice.example.com/a#frag'   # 字面量层无损（输入校验层另测）
        cases = [
            # 1) 真实配置加载（远程态：server_url 已设置）
            {'kind': 'load', 'name': 'load1', 'content': base},
        ]
        results = self._cases(cases)
        load1 = results[0]
        self.assertNotIn("'", load1['addr'], 'addr 不得带引号')
        self.assertNotIn("'", load1['port'], 'port 不得带引号')

        # 2) 远程保存（LAN 输入框为空、host/port 沿用 load1 值）
        (r2,) = self._cases([{'kind': 'apply_validate', 'name': 'remote_save',
                              'content': base, 'remoteMode': True,
                              'remoteUrl': remote_url,
                              'hostname': load1['addr'], 'port': int(load1['port']),
                              'duration': 0.75, 'liveMode': True, 'prompt': '词#带井号'}])
        self.assertIsNone(r2['error'])
        self.assertEqual(r2['server_url'], remote_url)
        content2 = r2['updated']
        compile(content2, 'config_client.py', 'exec')     # 整份语法可解析

        # 3) 真实加载远程态配置 → 切回 LAN（host/port 来自上一步的加载）
        (load2,) = self._cases([{'kind': 'load', 'name': 'load2', 'content': content2}])
        self.assertEqual(load2['server_url'], remote_url)
        (r3,) = self._cases([{'kind': 'apply_validate', 'name': 'lan_save',
                              'content': content2, 'remoteMode': False,
                              'remoteUrl': load2['server_url'],   # 隐藏字段保留远程地址
                              'hostname': load2['addr'], 'port': int(load2['port']),
                              'duration': 0.75, 'liveMode': True, 'prompt': '词#带井号'}])
        self.assertIsNone(r3['error'], '远程切 LAN 必须能保存（A02 反例）')
        self.assertEqual(r3['server_url'], '')
        content3 = r3['updated']
        compile(content3, 'config_client.py', 'exec')

        # 4) 再真实加载 LAN 态 → 切回远程（含 # 的路径在字面量层往返无损）
        (load3,) = self._cases([{'kind': 'load', 'name': 'load3', 'content': content3}])
        self.assertEqual(load3['server_url'], '')
        (r4,) = self._cases([{'kind': 'apply_validate', 'name': 'remote_again',
                              'content': content3, 'remoteMode': True,
                              'remoteUrl': remote_url2,
                              'hostname': load3['addr'], 'port': int(load3['port']),
                              'duration': 0.75, 'liveMode': True, 'prompt': '词#带井号'}])
        self.assertIsNone(r4['error'])
        self.assertEqual(r4['server_url'], remote_url2)
        compile(r4['updated'], 'config_client.py', 'exec')
        # 5) 末尾转义单引号同样走完整链
        (r5,) = self._cases([{'kind': 'apply_validate', 'name': 'remote_quote',
                              'content': r4['updated'], 'remoteMode': True,
                              'remoteUrl': "wss://voice.example.com/end'",
                              'hostname': load3['addr'], 'port': int(load3['port']),
                              'duration': 0.75, 'liveMode': True, 'prompt': "它's ok"}])
        self.assertIsNone(r5['error'])
        self.assertEqual(r5['server_url'], "wss://voice.example.com/end'")
        compile(r5['updated'], 'config_client.py', 'exec')

    def test_hash_and_trailing_quote_save(self):
        """合法输入含 # / 末尾单引号必须能保存（A02 反例：此前被校验拒绝）"""
        base = ("class ClientConfig:\r\n    server_url = ''\r\n    context = ''\r\n"
                "    addr = 'x'\r\n    port = '1'\r\n    udp_control_addr = '127.0.0.1'\r\n")
        cases = []
        for i, url in enumerate([
            'wss://voice.example.com/a#fragment',          # fragment → 拒绝（A03）
            "wss://voice.example.com/end'",                # 末尾单引号 → 保存成功
            'wss://voice.example.com/a#frag',              # 同 fragment
        ]):
            cases.append({'kind': 'apply_validate', 'name': f'u{i}', 'content': base,
                          'remoteMode': True, 'remoteUrl': url, 'hostname': 'x',
                          'port': 1, 'duration': 0.75, 'liveMode': True, 'prompt': '词#带井号'})
        results = self._cases(cases)
        # fragment 被输入校验拒绝（urlcheck 由 SaveSettings 前置），但
        # ApplyConfig/ValidateUpdate 层面对含 # 的字面量本身必须无损
        for i, r in enumerate(results):
            if '#' in cases[i]['remoteUrl']:
                # fragment URL：字面量往返必须无损（拒绝发生在 URL 输入校验层）
                self.assertIsNone(r['error'])
                self.assertEqual(r['server_url'], cases[i]['remoteUrl'])
            else:
                self.assertIsNone(r['error'])
                self.assertEqual(r['server_url'], cases[i]['remoteUrl'])
        # 含 # 的 context 提示词往返无损
        self.assertTrue(all(r['error'] is None for r in results))

    def test_remote_url_validation_rules(self):
        """A03：fragment/凭据查询/userinfo/越界端口被输入校验拒绝；普通 query 放行；
        查询名解码后匹配（x-api-key、%编码形式不能绕过）"""
        results = self._cases([
            {'kind': 'urlcheck', 'name': 'ok_plain', 'url': 'wss://voice.example.com'},
            {'kind': 'urlcheck', 'name': 'ok_path_query', 'url': 'wss://voice.example.com/asr?room=1'},
            {'kind': 'urlcheck', 'name': 'fragment', 'url': 'wss://voice.example.com/a#frag'},
            {'kind': 'urlcheck', 'name': 'cred_query', 'url': 'wss://voice.example.com/?api_key=dpk-fake-secret-123456'},
            {'kind': 'urlcheck', 'name': 'token_query', 'url': 'wss://voice.example.com/?token=abc'},
            {'kind': 'urlcheck', 'name': 'x_api_key_query', 'url': 'wss://voice.example.com/?x-api-key=dpk-FAKE-ONLY-12345'},
            {'kind': 'urlcheck', 'name': 'pct_encoded_query', 'url': 'wss://voice.example.com/?%61pi_key=dpk-FAKE-ONLY-12345'},
            {'kind': 'urlcheck', 'name': 'pct_encoded_x', 'url': 'wss://voice.example.com/?%78-api-key=dpk-FAKE-ONLY-12345'},
            {'kind': 'urlcheck', 'name': 'userinfo', 'url': 'wss://user:pass@voice.example.com'},
            {'kind': 'urlcheck', 'name': 'port_overflow', 'url': 'wss://voice.example.com:99999'},
        ])
        by_name = {r['name']: r for r in results}
        self.assertIsNone(by_name['ok_plain']['error'])
        self.assertIsNone(by_name['ok_path_query']['error'], '经验证的普通路径/查询应放行')
        for name in ('fragment', 'cred_query', 'token_query', 'x_api_key_query',
                     'pct_encoded_query', 'pct_encoded_x', 'userinfo', 'port_overflow'):
            self.assertIsNotNone(by_name[name]['error'], f'{name} 必须被拒绝')

    def test_remote_url_double_encoded_credential_rejected(self):
        """A03 一致性：双重编码（%2561pi_key）、%5f、'+' 前缀、大小写形式
        桌面端同样拒绝；普通查询 q=normal 仍放行——与客户端
        _normalize_query_name 同一策略（假 Key，不读真实值）"""
        results = self._cases([
            {'kind': 'urlcheck', 'name': 'double_pct', 'url': 'wss://voice.example.com/?%2561pi_key=dpk-FAKE-ONLY-12345'},
            {'kind': 'urlcheck', 'name': 'pct_underscore', 'url': 'wss://voice.example.com/?api%5fkey=dpk-FAKE-ONLY-12345'},
            {'kind': 'urlcheck', 'name': 'plus_prefix', 'url': 'wss://voice.example.com/?+api_key=dpk-FAKE-ONLY-12345'},
            {'kind': 'urlcheck', 'name': 'upper_case', 'url': 'wss://voice.example.com/?API-KEY=dpk-FAKE-ONLY-12345'},
            {'kind': 'urlcheck', 'name': 'normal_query', 'url': 'wss://voice.example.com/?q=normal'},
        ])
        by_name = {r['name']: r for r in results}
        for name in ('double_pct', 'pct_underscore', 'plus_prefix', 'upper_case'):
            self.assertIsNotNone(by_name[name]['error'], f'{name} 必须被拒绝')
        self.assertIsNone(by_name['normal_query']['error'], '普通查询应放行')


@unittest.skipIf(not CSC.exists(), '本机无 .NET Framework csc，跳过桌面保存行为实测')
class ShortcutEntryValueTests(unittest.TestCase):
    """快捷键字典项解析回归（桌面端 EntryValue）：
    行内注释不得把 `'enabled': True  # 启用` 误判为 False——旧正则把
    注释一起吃进值里，导致默认配置的快捷键被判定为禁用。"""

    @classmethod
    def setUpClass(cls):
        DesktopSaveBehaviorTests.setUpClass()
        cls.dll = DesktopSaveBehaviorTests.dll

    @classmethod
    def tearDownClass(cls):
        pass   # DLL 生命周期由 DesktopSaveBehaviorTests 管理

    def _cases(self, cases):
        return _run_cases(cases, self.dll)

    def test_commented_bool_and_key_parsing(self):
        results = self._cases([
            # 仓库默认配置形状：enabled 值后跟行内注释
            {'kind': 'entryvalue', 'name': 'commented_true',
             'entry': "{'key': 'caps_lock',     # 监听大写锁定键\r\n"
                      "  'type': 'keyboard',     # 是键盘快捷键\r\n"
                      "  'suppress': True,      # 阻塞按键\r\n"
                      "  'hold_mode': True,      # 长按模式\r\n"
                      "  'enabled': True         # 启用此快捷键\r\n}",
             'keyName': 'enabled', 'fallback': ''},
            {'kind': 'entryvalue', 'name': 'commented_key',
             'entry': "{'key': 'caps_lock',     # 监听大写锁定键\n  'enabled': True}",
             'keyName': 'key', 'fallback': ''},
            # 现场用户配置形状：显式禁用必须解析为 False（不得被盲目启用）
            {'kind': 'entryvalue', 'name': 'explicit_false',
             'entry': "{'key': 'caps_lock',  'enabled': False        # 启用此快捷键\n}",
             'keyName': 'enabled', 'fallback': ''},
            # 带引号键名（值内含逗号也不会被截断）
            {'kind': 'entryvalue', 'name': 'quoted_key',
             'entry': "{'key': 'ctrl+alt+space',  'enabled': True,}",
             'keyName': 'key', 'fallback': ''},
            # 缺失字段回退
            {'kind': 'entryvalue', 'name': 'missing_fallback',
             'entry': "{'enabled': True}", 'keyName': 'key', 'fallback': 'caps_lock'},
            # 注释里含 enabled 字样不得干扰
            {'kind': 'entryvalue', 'name': 'comment_noise',
             'entry': "{'hold_mode': False, # enabled: True 干扰\n  'enabled': True}",
             'keyName': 'hold_mode', 'fallback': 'True'},
        ])
        by = {r['name']: r for r in results}
        self.assertEqual(by['commented_true']['value'], 'True',
                         "行内注释把 True 误判为 False（原始缺陷）")
        self.assertEqual(by['commented_key']['value'], 'caps_lock')
        self.assertEqual(by['explicit_false']['value'], 'False')
        self.assertEqual(by['quoted_key']['value'], 'ctrl+alt+space')
        self.assertEqual(by['missing_fallback']['value'], 'caps_lock')
        self.assertEqual(by['comment_noise']['value'], 'False')


if __name__ == '__main__':
    unittest.main()
