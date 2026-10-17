"""Real audio callback and read-only UDP handler, with no microphone or input hooks."""
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pytest

from core.client.audio.stream import AudioStreamManager
from core.client.state import ClientState
from core.client.udp.udp_control import UDPController


def test_callback_measures_audio_and_recording_lifecycle_clears_it():
    state = ClientState()
    stream = AudioStreamManager(SimpleNamespace(state=state, loop=None))
    frame = np.full((2400, 2), .04, dtype=np.float32)
    stream._audio_callback(frame, len(frame), None, None)
    assert state.audio_level == (0, 0), 'idle callbacks do not publish microphone levels'
    state.start_recording(100)
    with mock.patch('core.client.audio.stream.time.monotonic', return_value=101):
        stream._audio_callback(frame, len(frame), None, None)
    assert state.audio_level[0] == 101
    assert state.audio_level[1] == pytest.approx(.04)
    stream._audio_callback(np.empty((0, 2)), 0, None, None)
    assert state.audio_level[1] == 0
    stream._audio_callback(np.full((2400, 1), np.nan), 2400, None, None)
    assert state.audio_level[1] == 0, 'invalid input cannot poison waveform state'
    state.audio_level = (101, .2)
    state.stop_recording()
    assert state.audio_level == (0, 0)
    state.audio_level = (101, .2)
    state.start_recording(102)
    assert state.audio_level == (0, 0), 'a new recording cannot inherit old levels'
    state.audio_level = (101, .2)
    state.reset()
    assert state.audio_level == (0, 0)


def test_udp_meter_is_fresh_bounded_and_read_only():
    state = ClientState(recording=True, audio_level=(100, .05))
    manager = SimpleNamespace(state=state, app=SimpleNamespace(ws=None))
    controller = UDPController(manager)
    sent = []
    controller._sock = SimpleNamespace(sendto=lambda data, addr: sent.append(data))
    address = ('127.0.0.1', 50001)
    with mock.patch('core.client.udp.udp_control.time.monotonic', return_value=100.1):
        controller._handle_command('LEVEL', address)
        assert sent[-1] == b'LEVEL|0.050000'
        assert state.audio_level == (100, .05) and state.recording
        state.audio_level = (100, 5)
        controller._handle_command('LEVEL', address)
        assert sent[-1] == b'LEVEL|1.000000'
        state.recording = False
        controller._handle_command('LEVEL', address)
        assert sent[-1] == b'LEVEL|0.000000'
        state.recording = True
        state.audio_level = (99, .05)
        controller._handle_command('LEVEL', address)
        assert sent[-1] == b'LEVEL|0.000000', 'stalled capture returns silence'
    controller._handle_command('STATE', address)
    assert b'meter=1' in sent[-1].split(b'|')
