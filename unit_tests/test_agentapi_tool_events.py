"""PIT-431: the chat's tool rows carry a call id, its arguments and its result.

The SSE chat stream used to forward a coalesced notification
(`bash (1x), read_file (1x)`) — no id, no arguments, no output, two distinct
calls merged into one label. The dashboard builds one card per `tool_use` id, so
every card had a header and an empty body. These guard the replacement record.
"""

from plugins.agentapi.routes import _tool_call_frames, _tool_result_text


def test_one_call_is_three_records_sharing_one_id():
    frames = _tool_call_frames('call-7', {
        'tool_name': 'bash',
        'tool_args': {'cmd': 'bun run build'},
        'tool_result': {'stdout': 'built'},
        'has_error': False,
    })
    assert [kind for kind, _ in frames] == ['tool_use', 'tool_args', 'tool_result']
    assert {f['id'] for _, f in frames} == {'call-7'}
    assert frames[0][1]['name'] == 'bash'
    assert frames[1][1]['delta'] == '{"cmd": "bun run build"}'
    assert frames[2][1] == {'id': 'call-7', 'content': '{"stdout": "built"}', 'is_error': False}


def test_two_calls_of_one_tool_are_two_records():
    first = _tool_call_frames('call-1', {'tool_name': 'bash', 'tool_args': {'cmd': 'ls'}})
    second = _tool_call_frames('call-2', {'tool_name': 'bash', 'tool_args': {'cmd': 'pwd'}})
    assert [f['id'] for _, f in first] != [f['id'] for _, f in second]
    assert first[0][1]['name'] == second[0][1]['name'] == 'bash'


def test_a_call_without_arguments_has_no_args_record():
    frames = _tool_call_frames('call-3', {'tool_name': 'list_skills', 'tool_result': 'ok'})
    assert [kind for kind, _ in frames] == ['tool_use', 'tool_result']


def test_an_error_result_carries_the_message_not_an_empty_body():
    frames = _tool_call_frames('call-4', {
        'tool_name': 'read_file',
        'tool_result': {'error': 'no such file: src/pages/index.astro'},
        'has_error': True,
    })
    result = frames[-1][1]
    assert result['is_error'] is True
    assert result['content'] == 'no such file: src/pages/index.astro'


def test_a_huge_result_is_cut_at_two_kilobytes():
    text = 'x' * 5000
    out = _tool_result_text(text)
    assert out.startswith('x' * 2048)
    assert out.endswith('… (+2952 bytes)')
    assert len(out.encode('utf-8')) < 2100


def test_truncation_never_splits_a_multibyte_character():
    out = _tool_result_text('é' * 3000)  # 2 bytes each: 2048 bytes is 1024 chars
    assert out.startswith('é' * 1024)
    out.encode('utf-8')  # decoded cleanly, so no replacement char crept in
    assert '\ufffd' not in out


def test_the_sse_stream_no_longer_coalesces_tool_calls():
    source = (__file__.rsplit('/unit_tests/', 1)[0]
              + '/plugins/agentapi/routes.py')
    body = open(source, encoding='utf-8').read()
    assert '_flush_tool_notify' not in body
    assert "'tool_args', 'tool_result'" in body or "('tool_use'" in body
    # The Telegram channel keeps its own coalescer; this stream must not touch it.
    assert 'backend/channels/telegram.py' in body
