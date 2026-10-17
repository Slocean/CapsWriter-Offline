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


def test_recording_handoff_waits_for_final_submission():
    import asyncio
    from types import SimpleNamespace
    from unittest import mock
    import numpy as np
    from core.client.audio import recorder as rec

    async def run(segmented):
        events = []
        sent = []
        submitting, release = asyncio.Event(), asyncio.Event()

        async def send(message):
            sent.append(message.is_final)
            if message.is_final:
                submitting.set()
                await release.wait()
            return True

        state = SimpleNamespace(queue_in=asyncio.Queue(), pop_audio_file=mock.Mock())
        app = SimpleNamespace(state=state, ws=SimpleNamespace(is_connected=True, send=send))
        state.queue_in.put_nowait({'type': 'begin', 'time': 1})
        for i in range(8):
            state.queue_in.put_nowait({'type': 'data', 'time': 1.05 + i*.05,
                                      'data': np.full((2400, 1), .04, dtype=np.float32)})
        state.queue_in.put_nowait({'type': 'finish', 'time': 1.5})
        with mock.patch.multiple(rec.Config, live_output=segmented, pause_segmented=segmented,
                                 save_audio=False, threshold=0), \
                mock.patch.object(rec, 'console'), \
                mock.patch.object(rec.lifecycle, 'info', side_effect=events.append):
            task = asyncio.create_task(rec.AudioRecorder(app).record_and_send())
            await asyncio.wait_for(submitting.wait(), 2)
            assert sent[-1] and not any(sent[:-1]), 'final must follow queued audio chunks'
            assert not task.done(), 'capture handoff must wait for final submission'
            assert any(e.startswith(LIFECYCLE_PREFIX + '提交 ') for e in events)
            assert not any(e.startswith(LIFECYCLE_PREFIX + '录音结束 ') for e in events)
            release.set()
            await task
        session = events[0].removeprefix(LIFECYCLE_PREFIX + '录音开始 ')
        assert events[-1] == LIFECYCLE_PREFIX + '录音结束 ' + session

    for segmented in (False, True):
        asyncio.run(run(segmented))


def test_recording_handoff_has_terminal_event_for_empty_cancelled_and_failed_capture():
    import asyncio
    from types import SimpleNamespace
    from unittest import mock
    from core.client.audio import recorder as rec

    async def run(mode):
        events = []
        state = SimpleNamespace(queue_in=asyncio.Queue())
        state.queue_in.put_nowait({'type': 'begin', 'time': 1})
        if mode == 'empty':
            state.queue_in.put_nowait({'type': 'finish', 'time': 1.5})
        if mode == 'failed':
            state.queue_in.put_nowait({'type': 'data', 'time': 2, 'data': None})
        app = SimpleNamespace(state=state)
        with mock.patch.multiple(rec.Config, live_output=True, pause_segmented=True, save_audio=False), \
                mock.patch.object(rec, 'console'), \
                mock.patch.object(rec.lifecycle, 'info', side_effect=events.append):
            task = asyncio.create_task(rec.AudioRecorder(app).record_and_send())
            await asyncio.sleep(0)
            if mode == 'cancelled':
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            else:
                await task
        session = events[0].removeprefix(LIFECYCLE_PREFIX + '录音开始 ')
        assert events[-1] == LIFECYCLE_PREFIX + '录音结束 ' + session
        assert len(events) == 2, 'empty/cancelled/failed capture must not invent recognition tasks'

    for mode in ('empty', 'cancelled', 'failed'):
        asyncio.run(run(mode))
