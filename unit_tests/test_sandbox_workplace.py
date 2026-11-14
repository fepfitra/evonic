"""The sandbox machine is a workplace record, and the binding is the only truth.

PIT-408: the dashboard holds the machine's `/exec` endpoint and token and drives
this lifecycle over the plugin's bearer door; evonic must show one `exec`
workplace per project, re-point it on a rebuild, and never fall back to another
backend once the machine is gone.
"""

from flask import Flask

from models.db import db
from plugins.agentapi import sandbox_workplace


def _agent(agent_id='a1', workplace_id=None):
    db.create_agent({'id': agent_id, 'name': agent_id})
    if workplace_id:
        db.update_agent(agent_id, {'workplace_id': workplace_id})
    return agent_id


def _exec_workplaces():
    return [w for w in db.get_workplaces() if w['type'] == 'exec']


def test_open_records_one_exec_workplace_and_binds_the_agent():
    agent_id = _agent()

    record = sandbox_workplace.bind(agent_id, 'fitrafep@crisp-badger',
                                    'https://crisp-badger-sandbox.pitura.id/exec', 'tok-1')

    stored = db.get_workplace(record['id'])
    assert stored['type'] == 'exec' and stored['name'] == 'fitrafep@crisp-badger'
    assert db.get_agent(agent_id)['workplace_id'] == record['id']
    assert [a['id'] for a in db.get_workplace_agents(record['id'])] == [agent_id]
    # The token never leaves the record's config.
    assert 'tok-1' in stored['config'] and 'tok-1' not in record.values()


def test_a_rebuild_repoints_the_same_record_instead_of_adding_one():
    agent_id = _agent()
    first = sandbox_workplace.bind(agent_id, 'fitrafep@crisp-badger',
                                   'https://crisp-badger-sandbox.pitura.id/exec', 'tok-1')

    second = sandbox_workplace.bind(agent_id, 'fitrafep@vivid-egret',
                                    'https://vivid-egret-sandbox.pitura.id/exec', 'tok-2')

    assert second['id'] == first['id'] and second['changed'] is True
    assert len(_exec_workplaces()) == 1
    stored = db.get_workplace(first['id'])
    assert stored['name'] == 'fitrafep@vivid-egret'
    assert 'vivid-egret' in stored['config'] and 'tok-2' in stored['config']
    assert 'crisp-badger' not in stored['config'] and 'tok-1' not in stored['config']


def test_an_unchanged_machine_costs_nothing():
    """The reap tick calls this every minute: no write, no backend drop."""
    agent_id = _agent()
    sandbox_workplace.bind(agent_id, 'fitrafep@crisp-badger',
                           'https://crisp-badger-sandbox.pitura.id/exec', 'tok-1')

    again = sandbox_workplace.bind(agent_id, 'fitrafep@crisp-badger',
                                   'https://crisp-badger-sandbox.pitura.id/exec', 'tok-1')

    assert again['changed'] is False
    assert len(_exec_workplaces()) == 1


def test_the_binding_moves_off_a_legacy_ssh_workplace():
    legacy = db.create_workplace({'name': 'fitrafep', 'type': 'remote',
                                  'config': {'host': 'old.example.com', 'username': 'fitrafep'}})
    agent_id = _agent(workplace_id=legacy)

    record = sandbox_workplace.bind(agent_id, 'fitrafep@crisp-badger',
                                    'https://crisp-badger-sandbox.pitura.id/exec', 'tok-1')

    assert db.get_agent(agent_id)['workplace_id'] == record['id']
    # The SSH entry is not repainted as the machine — it keeps its own type.
    assert db.get_workplace(legacy)['type'] == 'remote'
    assert db.get_workplace_agents(legacy) == []


def test_close_deletes_the_workplace_and_the_turn_names_the_gap():
    agent_id = _agent()
    record = sandbox_workplace.bind(agent_id, 'fitrafep@crisp-badger',
                                    'https://crisp-badger-sandbox.pitura.id/exec', 'tok-1')
    assert sandbox_workplace.missing(db.get_agent(agent_id)) == ''

    assert sandbox_workplace.close(agent_id) is True

    assert db.get_workplace(record['id']) is None
    # The binding stays on the dead id on purpose: a turn now fails with a
    # reason instead of quietly running tools on the docker backend.
    reason = sandbox_workplace.missing(db.get_agent(agent_id))
    assert record['id'] in reason and 'reaped' in reason
    assert sandbox_workplace.close(agent_id) is False


def test_an_agent_with_no_machine_is_not_a_gap():
    agent_id = _agent()

    assert sandbox_workplace.missing(db.get_agent(agent_id)) == ''


def test_the_lifecycle_routes_are_mounted_and_refuse_anonymous_callers():
    from plugins.agentapi.routes import create_blueprint

    app = Flask(__name__)
    app.register_blueprint(create_blueprint())
    client = app.test_client()

    assert client.post('/plugin/agentapi/v1/workplace', json={}).status_code == 401
    assert client.delete('/plugin/agentapi/v1/workplace?model=m').status_code == 401
