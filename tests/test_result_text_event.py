"""Final display events must follow output/clipboard restoration, with no real keyboard/clipboard."""
import asyncio
import json
import pytest
from types import SimpleNamespace
from unittest import mock

from core.client.output import result_processor as rp
from core.protocol import RecognitionMessage


def test_final_text_event_after_output():
    events = []

    async def output(text, paste=None):
        events.append(('output_and_restore', text))

    processed = '修正后的文字\n带"引号"与\\路径'
    corrector = SimpleNamespace(correct=lambda *a, **k: SimpleNamespace(
        text=processed, matches=[], similars=[]))
    app = SimpleNamespace(
        state=SimpleNamespace(set_output_text=mock.Mock()),
        hotword=SimpleNamespace(get_phoneme_corrector=lambda: corrector,
                               get_rule_corrector=lambda: SimpleNamespace(substitute=lambda t: t)),
        output=SimpleNamespace(output=output))

    async def run():
        processor = rp.ResultProcessor(app)
        with mock.patch.multiple(rp.Config, hot=True, live_output=False, pause_segmented=True,
                                 llm_enabled=False, traditional_convert=False, save_audio=False,
                                 enter_apps=[], paste_apps=[], paste=True), \
                mock.patch.object(rp, 'get_active_window_info', return_value={'process_name': ''}), \
                mock.patch.object(rp.TextOutput, 'strip_punc', side_effect=lambda t: t), \
                mock.patch.object(rp, 'broadcast_output_udp'), \
                mock.patch.object(rp, 'console'), \
                mock.patch.object(processor, '_log_modifier_key_state'), \
                mock.patch.object(rp.lifecycle, 'info', side_effect=lambda text: events.append(('event', text))):
            message = RecognitionMessage('test', True, 1, 0, 1, 2, '原始结果')
            await processor._handle_message(message)
            final_events = [entry for entry in events if entry[0] == 'event'
                            and entry[1].startswith(rp.LIFECYCLE_PREFIX + '文字 ')]
            assert len(final_events) == 1
            event = final_events[0]
            assert json.loads(event[1].removeprefix(rp.LIFECYCLE_PREFIX + '文字 ')) == processed
            assert events.index(('output_and_restore', processed)) < events.index(event)
            assert '\n' not in event[1], 'embedded newlines must not split the log event'
            events.clear()
            message.is_final = False
            await processor._handle_message(message)
            assert not events, 'interim results must not trigger automatic copying'
            message.is_final = True
            message.text = ' /sil '
            await processor._handle_message(message)
            assert all(not e[1].startswith(rp.LIFECYCLE_PREFIX + '文字 ') for e in events)
            events.clear()
            message.text = '输入失败的结果'
            with mock.patch.object(app.output, 'output', side_effect=RuntimeError('input failed')):
                with pytest.raises(RuntimeError, match='input failed'):
                    await processor._handle_message(message)
            assert any(e[1].startswith(rp.LIFECYCLE_PREFIX + '文字 ') for e in events), \
                'failed output must still leave selectable final text'

    asyncio.run(run())
