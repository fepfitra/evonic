"""The chat proxy's exec headers put the agent's tools in the machine (PIT-401).

The dashboard sends the machine's `POST /exec` endpoint and the token it minted
(`server-rs/src/agent.rs` `exec_headers`); evonic turns that into the workplace
the agent's tools run in. One check of the three boundaries that matter:
first call creates and moves, second call is idempotent, no headers change
nothing, and a Docker-sandboxed agent is left alone (that sandbox wins).
"""

import json

from flask import Flask

from models.db import db
from plugins.agentapi import routes as agentapi

URL = 'https://crisp-badger-sandbox.pitura.id/exec'
TOKEN = 'a' * 32


def _agent(agent_id, **extra):
    db.create_agent({'id': agent_id, 'name': agent_id, **extra})
    return db.get_agent(agent_id)


def _call(agent, url=URL, token=TOKEN):
    app = Flask(__name__)
    headers = {}
    if url:
        headers[agentapi.EXEC_URL_HEADER] = url
    if token:
        headers[agentapi.EXEC_TOKEN_HEADER] = token
    with app.test_request_context('/plugin/agentapi/v1/chat/completions', headers=headers):
        agentapi._apply_sandbox_exec(agent)


def test_exec_headers_move_the_agent_onto_the_machine():
    agent = _agent('pit401-agent')
    assert agent['workplace_id'] is None

    _call(agent)

    wp_id = agentapi._exec_workplace_id('pit401-agent')
    assert agent['workplace_id'] == wp_id
    assert db.get_agent('pit401-agent')['workplace_id'] == wp_id
    row = db.get_workplace(wp_id)
    assert row['type'] == 'exec'
    assert json.loads(row['config']) == {'url': URL, 'token': TOKEN}

    # Same machine again: no second row, no write, agent stays put.
    _call(db.get_agent('pit401-agent'))
    assert db.get_workplace(wp_id)['type'] == 'exec'
    assert db.get_agent('pit401-agent')['workplace_id'] == wp_id


def test_a_new_machine_rewrites_the_same_row():
    agent = _agent('pit401-replaced')
    _call(agent)
    wp_id = agentapi._exec_workplace_id('pit401-replaced')

    newer = URL.replace('crisp-badger', 'vivid-egret')
    _call(db.get_agent('pit401-replaced'), url=newer, token='b' * 32)

    row = db.get_workplace(wp_id)
    assert json.loads(row['config']) == {'url': newer, 'token': 'b' * 32}
    rows = [w for w in db.get_workplace_summaries() if w['type'] == 'exec']
    assert len(rows) == 1


def test_no_headers_change_nothing():
    agent = _agent('pit401-plain')
    _call(agent, url='', token='')
    assert agent['workplace_id'] is None
    assert [w for w in db.get_workplace_summaries() if w['type'] == 'exec'] == []


def test_a_docker_sandboxed_agent_keeps_its_backend():
    agent = _agent('pit401-docker', sandbox_enabled=True)
    _call(agent)
    assert db.get_agent('pit401-docker')['workplace_id'] is None
    assert [w for w in db.get_workplace_summaries() if w['type'] == 'exec'] == []
