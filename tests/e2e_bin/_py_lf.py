# coding: utf-8
"""e2e 夹具启动器：把当前测试解释器当 python3 用，并把 stdout/stderr 钉成 LF。

Windows Python 的文本模式输出 CRLF，会破坏 bash 的字符串比较
（verdict="success\r" 永远不等于 "success"）；WSL 真实 python3 输出 LF，
这里让测试环境与部署环境一致。支持 -c / -（stdin）/ 脚本文件三种调用。
"""
import runpy
import sys

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, 'reconfigure'):
        stream.reconfigure(newline='\n')

args = sys.argv[1:]
if not args:
    raise SystemExit(2)
if args[0] == '-c':
    sys.argv = ['-c'] + args[2:]
    exec(compile(args[1], '<string>', 'exec'), {'__name__': '__main__'})
elif args[0] == '-':
    sys.argv = ['-'] + args[1:]
    exec(compile(sys.stdin.read(), '<stdin>', 'exec'), {'__name__': '__main__'})
else:
    sys.argv = args
    runpy.run_path(args[0], run_name='__main__')
