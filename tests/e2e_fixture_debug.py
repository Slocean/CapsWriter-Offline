# coding: utf-8
"""e2e 手工调试夹具生成器（与 tests/test_voice_setup_e2e.py 的 run_script 等价）"""
import json
import os
import pathlib
import sys

tmp = pathlib.Path(os.environ['TEMP']) / 'vstest'
if tmp.exists():
    shutil = __import__('shutil')
    shutil.rmtree(tmp, ignore_errors=True)
bin_dir = tmp / 'bin'
bin_dir.mkdir(parents=True)
for name in ('docker', 'curl', 'python3'):
    text = (pathlib.Path('tests/e2e_bin') / name).read_text(encoding='utf-8')
    text = text.replace('@PYTHON@', sys.executable.replace(chr(92), '/'))
    text = text.replace(chr(13) + chr(10), chr(10))
    dst = bin_dir / name
    dst.write_text(text, encoding='utf-8', newline=chr(10))
    os.chmod(dst, 0o755)

scen = {
    "panel_ips": ["172.18.0.3", "172.19.0.13"], "valid_ip": "172.19.0.13",
    "pipelines": [
        {"id": "fundglass-api-prod", "name": "f", "slug": "fundglass-api", "env": "prod",
         "type": "api", "enabled": True, "note": "CapsWriter voice gateway",
         "domain": "fundglass-api.faiz.dpdns.org",
         "domains": ["fundglass-api.faiz.dpdns.org"], "latest": "20260929-160000"},
        {"id": "voice-asr-prod", "name": "a", "slug": "voice-asr", "env": "prod",
         "type": "api", "enabled": True, "note": "CapsWriter voice gateway",
         "domain": "voice.faiz.dpdns.org", "domains": ["voice.faiz.dpdns.org"],
         "latest": "20260929-160000"},
        {"id": "voice-admin-prod", "name": "m", "slug": "voice-admin", "env": "prod",
         "type": "api", "enabled": True, "note": "CapsWriter voice gateway",
         "domain": "voice-admin.faiz.dpdns.org",
         "domains": ["voice-admin.faiz.dpdns.org"], "latest": "20260929-160000"},
    ],
    "gate": {"enabled": True, "domains": ["nav.faiz.dpdns.org"], "exempt": {}},
    "caddy": {"voice_nokey": 401, "voice_withkey_pre": 502, "voice_withkey": 426,
              "admin_noauth": 302},
    "containers_running": True, "deploy_result": "success",
}
(tmp / 'scenario.json').write_text(json.dumps(scen, ensure_ascii=False), newline=chr(10))
(tmp / 'calls.jsonl').write_text('', newline=chr(10))
(tmp / 'state').mkdir()
print('fixture ready at', tmp)
print('export VOICE_E2E_SCENARIO="%s" VOICE_E2E_CALLLOG="%s" VOICE_E2E_STATE="%s" PATH="/tmp/vstest/bin:$PATH" MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL="*"'
      % (tmp / 'scenario.json', tmp / 'calls.jsonl', tmp / 'state'))
