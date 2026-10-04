# coding: utf-8
"""生命周期遥测通道：INFO 语义事件在用户 log_level=WARNING/ERROR 档仍落盘；
普通 INFO 被正常过滤；真实失败（ERROR）照常落盘。文件写在 tmp 目录。"""
import logging
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.logger import Logger, LIFECYCLE_PREFIX


def _fresh(name):
    for h in list(logging.getLogger(name + '.lifecycle').handlers):
        logging.getLogger(name + '.lifecycle').removeHandler(h)
    base = logging.getLogger(name)
    for h in list(base.handlers):
        h.close()
        base.removeHandler(h)
    Logger._loggers.pop(name, None)


def _run(level, tmp_path):
    name = 'telemetry-' + level.lower()
    _fresh(name)
    log_dir = tmp_path / level
    log_dir.mkdir()
    base = Logger.setup(name, log_dir=str(log_dir), level=level)
    telemetry = Logger.telemetry(name)
    telemetry.info(LIFECYCLE_PREFIX + '提交 task-1')       # 正常事件：必须落盘
    base.info('普通info-' + level)                          # 随级别过滤
    base.error(LIFECYCLE_PREFIX + '发送失败 task-2')        # 真实失败：必须落盘
    text = (log_dir / (name + '_latest.log')).read_text(encoding='utf-8')
    try:
        assert LIFECYCLE_PREFIX + '提交 task-1' in text, level + ': 生命周期INFO被过滤'
        assert LIFECYCLE_PREFIX + '发送失败 task-2' in text, level + ': ERROR丢失'
        if level == 'ERROR':
            assert '普通info-' + level not in text, level + ': 普通INFO未过滤'
        assert 'INFO' in text, '生命周期事件应为INFO语义（非ERROR）'
    finally:
        for h in list(telemetry.handlers):
            telemetry.removeHandler(h)
        for h in list(base.handlers):
            h.close()
            base.removeHandler(h)
        Logger._loggers.pop(name, None)


def test_lifecycle_info_survives_warning(tmp_path):
    _run('WARNING', tmp_path)


def test_lifecycle_info_survives_error(tmp_path):
    _run('ERROR', tmp_path)


def test_lifecycle_info_at_default_debug(tmp_path):
    _run('DEBUG', tmp_path)
