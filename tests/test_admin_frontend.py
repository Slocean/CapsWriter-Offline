# coding: utf-8
"""
管理端前端逻辑测试（R03/R13/R14）：用 Node VM 执行真实的 admin/static/app.js，
DOM/fetch 用假对象（独立验收 §2.6 的方法收编为正式测试）。

需要 node；本机未装或 PATH 无 node 时跳过（已知安装位置 fnm 也会探测）。
"""

import glob
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]

HARNESS = r'''
const fs=require('fs');
const vm=require('vm');
const APP=process.argv[2];
const elements=new Map();
const el=id=>{
  if(!elements.has(id)) elements.set(id,{classList:{add(){},remove(){},toggle(){}},style:{},listeners:{},addEventListener(n,f){this.listeners[n]=f},appendChild(){},textContent:'',disabled:false,value:'',className:''});
  return elements.get(id);
};
const calls=[];
const statusPayload={asr:{state:'stopped',controllable:false,can_start:true,can_stop:false,can_restart:false},schema:{},values:{},entries:[],lines:[],action:null};
const context={
  document:{getElementById:el,createElement:()=>el('newElement'),documentElement:{classList:{toggle(){}}},hidden:false},
  localStorage:{getItem(){return 'light'},setItem(){}},
  fetch:(url,opts)=>{
    calls.push({url,opts:opts||{}});
    return Promise.resolve({status:200,ok:true,json:()=>Promise.resolve(url==='/api/v1/bootstrap'
      ?{mode:'password',authenticated:true,csrf:'csrf-from-bootstrap'}
      :statusPayload)});
  },
  setInterval(){return 1},clearInterval(){},Date,Math,Promise,console,
  window:{confirm(){return true}}
};
vm.createContext(context);
let src=fs.readFileSync(APP,'utf8');
// CRLF/LF 检出都能命中锚点（工作树按 CRLF 检出时固定 \n 锚点会失配）
src=src.replace(/  applyTheme\(\);\r?\n  \/\/ 启动/,
  '  globalThis.auditRenderStatus = renderStatus;\n  globalThis.auditApi = api;\n  globalThis.auditPollLogs = pollLogs;\n  applyTheme();\n  // 启动');
vm.runInContext(src,context);
const fail=msg=>{console.error('FAIL: '+msg);process.exit(1)};
const ok=msg=>console.log('PASS: '+msg);

// R03：按钮能力按 can_start / can_stop 拆分——已停止的新版必须能启动
context.auditRenderStatus({state:'stopped',controllable:false,can_start:true,can_stop:false,can_restart:false});
if(el('startBtn').disabled!==false) fail('stopped+can_start 应允许启动');
if(el('stopBtn').disabled!==true) fail('stopped 应禁用停止');
ok('R03 stopped(can_start=true) 启动可用、停止禁用');

context.auditRenderStatus({state:'online',controllable:true,can_start:false,can_stop:true,can_restart:true,detail:{connections:0}});
if(el('startBtn').disabled!==true) fail('online 应禁用启动');
if(el('stopBtn').disabled!==false) fail('online+can_stop 应允许停止');
if(el('restartBtn').disabled!==false) fail('online+can_restart 应允许重启');
ok('R03 online(can_stop=true) 停止/重启可用、启动禁用');

// R03 反例：旧版（不可核身）三个按钮全禁用
context.auditRenderStatus({state:'stale',controllable:false,can_start:false,can_stop:false,can_restart:false});
if(el('startBtn').disabled!==true||el('stopBtn').disabled!==true||el('restartBtn').disabled!==true)
  fail('stale（旧版不可核身）必须全禁用');
ok('R03 stale 全禁用');

// R13：password 页面刷新恢复会话后，写请求必须带上 bootstrap 下发的 CSRF
setImmediate(async()=>{
  context.auditRenderStatus({state:'stale',can_start:false,can_stop:false,can_restart:false,
    can_resolve_port:true,can_auto_resolve:true,port_owners:{'123':[1,'C:/test.exe']}});
  if(el('portAutoBtn').disabled || el('portStopBtn').disabled) fail('端口冲突选项应可用');
  el('portAutoBtn').listeners.click();
  el('portStopBtn').listeners.click();
  const starts=calls.filter(c=>c.url==='/api/v1/actions/start').map(c=>JSON.parse(c.opts.body));
  if(!starts.some(x=>x.port_conflict==='auto')) fail('自动换端口未提交');
  if(!starts.some(x=>x.port_conflict==='stop' && x.port_owners['123'][1]==='C:/test.exe'))
    fail('关闭占用进程未提交核身快照');
  ok('端口冲突选项均提交正确参数');
  await context.auditApi('PUT','/api/v1/settings',{});
  const post=calls.find(c=>c.opts.method==='PUT');
  if(!post||post.opts.headers['X-CSRF-Token']!=='csrf-from-bootstrap')
    fail('刷新后写请求未携带 bootstrap 下发的 CSRF');
  ok('R13 password 刷新后写请求携带 CSRF');

  // R14：暂停后不得再发日志请求
  const before=calls.filter(c=>c.url.startsWith('/api/v1/logs')).length;
  el('logPause').listeners.click();          // 进入暂停
  context.auditPollLogs();
  await new Promise(r=>setImmediate(r));
  const after=calls.filter(c=>c.url.startsWith('/api/v1/logs')).length;
  if(after!==before) fail('暂停期间仍发送了日志请求');
  ok('R14 暂停后 pollLogs 不再请求');

  // 恢复后应继续拉取（取消暂停的点击本身会触发一次 pollLogs）
  el('logPause').listeners.click();          // 取消暂停
  await new Promise(r=>setImmediate(r));
  const resumed=calls.filter(c=>c.url.startsWith('/api/v1/logs')).length;
  if(resumed!==before+1) fail('取消暂停后未恢复日志拉取');
  ok('R14 恢复后继续拉取日志');
  console.log('ALL_OK');
});
'''


def _find_node():
    exe = shutil.which('node')
    if exe:
        return exe
    # 全局规则：本机 node 由 fnm 管理，不在默认 PATH
    for pattern in (
        os.path.join(os.environ.get('APPDATA', ''), 'fnm', 'node-versions', 'v*', 'installation', 'node.exe'),
        os.path.expanduser('~/.fnm/node-versions/v*/installation/node.exe'),
    ):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]
    return None


@unittest.skipIf(_find_node() is None, '本机无 node，跳过前端 VM 测试')
class AdminFrontendTests(unittest.TestCase):
    def test_app_js_r03_r13_r14(self):
        node = _find_node()
        with tempfile.TemporaryDirectory() as td:
            harness = pathlib.Path(td) / 'harness.js'
            harness.write_text(HARNESS, encoding='utf-8', newline='\n')
            proc = subprocess.run(
                [node, str(harness), str(REPO / 'admin' / 'static' / 'app.js')],
                capture_output=True, text=True, timeout=60, cwd=str(REPO))
            out = (proc.stdout or '') + (proc.stderr or '')
            self.assertEqual(proc.returncode, 0, f'node 测试失败：\n{out}')
            self.assertIn('ALL_OK', proc.stdout)
            self.assertIn('R03 stopped', proc.stdout)
            self.assertIn('R03 stale 全禁用', proc.stdout)
            self.assertIn('R13', proc.stdout)
            self.assertIn('R14', proc.stdout)


if __name__ == '__main__':
    unittest.main()
