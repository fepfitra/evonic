"""A project's sandbox machine as a Workplace the operator can see (PIT-408).

Pitura opens one Fly machine per project and holds its `POST /exec` endpoint
plus the `SANDBOX_EXEC_TOKEN` minted at create. Until this module existed those
credentials reached evonic per chat request as a header, which built a backend
for the turn and forgot it — so the machine the agent worked in never appeared
on the Workplaces page, and the five legacy `Remote SSH` entries were the only
thing an operator could see.

Now the machine is a record: the dashboard opens it (`bind`) after the machine
answers ready, re-points the same record at a new machine on a rebuild, and
closes it (`close`) in the same pass that deletes the machine. One workplace per
project is not enforced by name — it is the agent's own `workplace_id`, so a
rebuild can never leave a second, stale entry beside it.

`close` deliberately leaves the agent's binding pointing at the deleted id: the
token died with the machine, and a turn that still tries to use it must fail
with a reason (`missing`) instead of quietly running tools on another backend.
"""

import json
import logging
from typing import Optional

_logger = logging.getLogger(__name__)

EXEC_TYPE = 'exec'
WORKSPACE_PATH = '/workspace'


def _disconnect(workplace_id: str) -> None:
    """Drop the cached backend: a re-pointed machine must not answer with the
    dead one's url and token, which is what the per-id cache holds."""
    try:
        from backend.workplaces.manager import workplace_manager
        workplace_manager.disconnect(workplace_id)
    except Exception:
        _logger.warning('[sandbox_workplace] could not drop backend %s', workplace_id,
                        exc_info=True)


def _config(workplace: Optional[dict]) -> dict:
    try:
        cfg = json.loads((workplace or {}).get('config') or '{}')
    except (TypeError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def bind(agent_id: str, name: str, url: str, token: str) -> dict:
    """Point this project's agent at `url` as its own `exec` workplace.

    Idempotent on purpose: the dashboard calls this every reap tick, and an
    unchanged machine must not cost a backend disconnect (which would drop a
    live turn's backend once a minute).
    """
    from models.db import db

    config = {'url': url, 'token': token, 'workspace_path': WORKSPACE_PATH}
    agent = db.get_agent(agent_id) or {}
    current = db.get_workplace(agent['workplace_id']) if agent.get('workplace_id') else None

    if current and current.get('type') == EXEC_TYPE:
        if current.get('name') == name and _config(current) == config:
            return {'id': current['id'], 'name': name, 'type': EXEC_TYPE, 'changed': False}
        db.update_workplace(current['id'], {'name': name, 'config': config})
        # Before the next tool call, not after: the cached backend holds the old
        # machine's url and token, and that machine is the one being replaced.
        _disconnect(current['id'])
        return {'id': current['id'], 'name': name, 'type': EXEC_TYPE, 'changed': True}

    # No exec workplace yet — including the case where the agent still sits on
    # one of the legacy `Remote SSH` workplaces: the machine is a new record and
    # the binding moves to it, so the SSH entry keeps no claim on the agent.
    workplace_id = db.create_workplace({'name': name, 'type': EXEC_TYPE, 'config': config})
    db.update_agent(agent_id, {'workplace_id': workplace_id})
    _logger.info('[sandbox_workplace] agent=%s workplace=%s machine=%s', agent_id, workplace_id, url)
    return {'id': workplace_id, 'name': name, 'type': EXEC_TYPE, 'changed': True}


def close(agent_id: str) -> bool:
    """Delete the agent's `exec` workplace after its machine is gone.

    A no-op for an agent with no exec workplace, so the reap path can call it
    unconditionally. The record goes, the binding stays (see the module note).
    """
    from models.db import db

    agent = db.get_agent(agent_id) or {}
    workplace_id = (agent.get('workplace_id') or '').strip()
    workplace = db.get_workplace(workplace_id) if workplace_id else None
    if not workplace or workplace.get('type') != EXEC_TYPE:
        return False
    _disconnect(workplace_id)
    db.delete_workplace(workplace_id)
    _logger.info('[sandbox_workplace] agent=%s workplace=%s closed with its machine',
                 agent_id, workplace_id)
    return True


def missing(agent: dict) -> str:
    """Why a turn must refuse, or '' when the binding is fine — or absent.

    Absent is fine: an agent nobody has opened a machine for (any agent that is
    not a pitura project) keeps the backend it always had. A binding that points
    at nothing is not fine — that is `close` having run, and the only honest
    answer is a failed turn naming it.
    """
    from models.db import db

    workplace_id = ((agent or {}).get('workplace_id') or '').strip()
    if not workplace_id or db.get_workplace(workplace_id):
        return ''
    return (f"workplace '{workplace_id}' no longer exists — the project's sandbox "
            "machine was reaped; open the project to start a new one")
