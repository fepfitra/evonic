"""
AgentAPI Plugin — Flask Route Handlers

Consumer endpoints (/plugin/ prefix → bypass global session auth — plugin handles its own auth):
  POST /plugin/agentapi/v1/chat/completions   — OpenAI-compatible chat completion
  GET  /plugin/agentapi/v1/models             — List available models (filtered by token scope)

Admin endpoints (/api/ prefix → session auth required):
  GET    /api/agentapi/admin              — Admin dashboard page
  GET    /api/agentapi/admin/tokens       — List all tokens
  POST   /api/agentapi/admin/tokens       — Create a new token
  PUT    /api/agentapi/admin/tokens/<id>  — Update a token
  DELETE /api/agentapi/admin/tokens/<id>  — Delete a token
  GET    /api/agentapi/admin/tokens/<id>/stats — Token usage stats
"""

import hashlib
import itertools
import json
import logging
import os
import queue
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta

_logger = logging.getLogger(__name__)

from flask import (
    Blueprint, Response, jsonify, render_template, request,
    stream_with_context,
)

from plugins.agentapi.db import TokenDB, hash_token

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
token_db = TokenDB()

# A Pitura project subdomain, the same shape the dashboard validates
# (`server-rs/src/create_project.rs` `check_slug`): the provisioning call carries
# it, so it is checked again here rather than trusted.
_PROJECT_SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$')

# In-memory cache: maps token_id → plaintext (for the /reveal endpoint)
_plaintext_cache: dict[int, str] = {}
_PLAINTEXT_CACHE_TTL = 3600  # seconds

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# One tool result travels through SSE and into `chat_turn_events`, so a 5 MB
# build log is cut here rather than stored.
_TOOL_RESULT_LIMIT = 2048

def _tool_result_text(result) -> str:
    """Text for a tool result: an error message when there is one, else the
    output — compact JSON for dicts, capped at 2 KB with a `… (+N bytes)` tail."""
    if result is None:
        return ''
    if isinstance(result, dict):
        err = result.get('error')
        if isinstance(err, str) and err:
            text = err
        else:
            text = json.dumps(result, ensure_ascii=False)
    elif isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, ensure_ascii=False)
    raw = text.encode('utf-8')
    if len(raw) <= _TOOL_RESULT_LIMIT:
        return text
    cut = raw[:_TOOL_RESULT_LIMIT].decode('utf-8', 'ignore')
    return cut + f'\u2026 (+{len(raw) - len(cut.encode("utf-8"))} bytes)'

def _tool_call_frames(call_id: str, data: dict) -> list:
    """The three SSE records for one finished tool call (PIT-431).

    One call is one transcript row: `tool_use` names it, `tool_args` carries the
    JSON it was called with, `tool_result` its output. They share `call_id`, so
    two calls with the same tool name stay two rows. Arguments are omitted when
    the call had none, and a result is only the truncated text — the record is
    not a notification and cannot be merged with its neighbour.
    """
    frames = [('tool_use', {'id': call_id, 'name': data.get('tool_name', '')})]
    args = data.get('tool_args') or {}
    if args:
        frames.append(('tool_args', {
            'id': call_id,
            'delta': json.dumps(args, ensure_ascii=False),
        }))
    frames.append(('tool_result', {
        'id': call_id,
        'content': _tool_result_text(data.get('tool_result')),
        'is_error': bool(data.get('has_error')),
    }))
    return frames

def _parse_allowed_models(raw) -> list:
    """Parse allowed_models from DB storage (JSON string or '*')."""
    if raw == '*' or raw is None:
        return ['*']
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return []

def _get_model_agent_map():
    """Load MODEL_AGENT_MAP from plugin config, parse as JSON."""
    from backend.plugin_manager import plugin_manager
    cfg = plugin_manager.get_plugin_config('agentapi')
    raw = cfg.get('MODEL_AGENT_MAP', '{}')
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return {}

def _validate_bearer_token():
    """Extract and validate Bearer token from Authorization header.

    Returns (token_row, error_tuple).
    error_tuple is (body_dict, status_code) or None if valid.
    """
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return None, ({'error': 'Missing or invalid Authorization header'}, 401)

    token_str = auth_header[7:]  # strip 'Bearer '
    token_h = hash_token(token_str)
    token_row = token_db.get_token_by_hash(token_h)

    if not token_row:
        return None, ({'error': 'Invalid token'}, 401)

    if token_row.get('status') == 'suspended':
        return None, ({'error': 'Token suspended'}, 403)

    if token_db.is_token_expired(token_row):
        return None, ({'error': 'Token expired'}, 403)

    # Check/reset quota
    token_db.reset_quota_if_needed(token_row)
    quota_limit = token_row.get('quota_limit')
    quota_used = token_row.get('quota_used', 0)
    if quota_limit is not None and quota_used >= quota_limit:
        return None, ({
            'error': 'Quota exceeded',
            'quota_limit': quota_limit,
            'quota_used': quota_used,
        }, 429)

    return token_row, None

def _build_user_message(messages: list) -> str:
    """Extract the last user message from an OpenAI-style messages array.

    System messages are merged with the user message by prepending them
    as context, since this endpoint routes to an agent that already has
    its own internal system prompt.
    """
    if not messages:
        return ''

    # Collect system messages
    system_parts = []
    for msg in messages:
        if msg.get('role') == 'system' and msg.get('content'):
            content = msg['content']
            if isinstance(content, list):
                text_parts = [p.get('text', '') for p in content if p.get('type') == 'text']
                content = '\n'.join(text_parts)
            if content:
                system_parts.append(content)

    system_prefix = ''
    if system_parts:
        system_prefix = f"[System Instructions]\n{''.join(system_parts)}\n\n"

    # Prefer the last user message
    for msg in reversed(messages):
        if msg.get('role') == 'user' and msg.get('content'):
            user_content = msg['content']
            if system_prefix:
                return f"{system_prefix}[User Message]\n{user_content}"
            return user_content

    # Fallback: concatenate all non-system content
    parts = []
    for msg in messages:
        if msg.get('role') == 'system':
            continue
        content = msg.get('content', '')
        if isinstance(content, list):
            text_parts = [p.get('text', '') for p in content if p.get('type') == 'text']
            content = '\n'.join(text_parts)
        if content:
            role = msg.get('role', 'user')
            parts.append(f"[{role}]: {content}")

    combined = '\n'.join(parts)
    return f"{system_prefix}{combined}" if system_prefix else combined

def _generate_external_user_id(token_row: dict, agent_id: str, request) -> str:
    """Generate an external_user_id for the API consumer.

    Uses X-Session-Id header if present (opt-in stateful);
    otherwise creates a deterministic ID from token_hash + agent_id.
    The caller is responsible for clearing the session when stateless
    behavior is desired — this function only produces the ID.
    """
    session_header = request.headers.get('X-Session-Id', '').strip()
    if session_header:
        return f"api:{session_header}"
    # Deterministic: same token + agent = same session
    token_part = token_row.get('token_hash', '')[:16]
    return f"api:{token_part}:{agent_id}"

def _format_openai_response(content: str, model: str,
                            usage: dict = None) -> dict:
    """Build an OpenAI-compatible chat completion response."""
    resp = {
        'id': f"chatcmpl-{uuid.uuid4().hex[:12]}",
        'object': 'chat.completion',
        'created': int(time.time()),
        'model': model,
        'choices': [{
            'index': 0,
            'message': {
                'role': 'assistant',
                'content': content,
            },
            'finish_reason': 'stop',
        }],
    }
    if usage:
        resp['usage'] = usage
    return resp


# ---------------------------------------------------------------------------
# Blueprint
# ---------------------------------------------------------------------------

def _cors_enabled():
    """Check if DISABLE_CORS is enabled in plugin config."""
    from backend.plugin_manager import plugin_manager
    cfg = plugin_manager.get_plugin_config('agentapi')
    return bool(cfg.get('DISABLE_CORS', False))

def _add_cors_headers(response):
    """Inject CORS headers into response when DISABLE_CORS is enabled."""
    if not _cors_enabled():
        return response
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Session-Id'
    response.headers['Access-Control-Max-Age'] = '86400'
    return response


def create_blueprint():
    bp = Blueprint('agentapi', __name__,
                   template_folder=os.path.join(PLUGIN_DIR, 'templates'))

    # CORS after_request hook — inject headers on every response
    bp.after_request(_add_cors_headers)

    # Handle CORS preflight (OPTIONS) for all consumer routes
    @bp.route('/plugin/agentapi/v1/chat/completions', methods=['OPTIONS'])
    @bp.route('/plugin/agentapi/v1/models', methods=['OPTIONS'])
    @bp.route('/api/agentapi/admin', methods=['OPTIONS'])
    @bp.route('/api/agentapi/admin/tokens', methods=['OPTIONS'])
    @bp.route('/api/agentapi/admin/tokens/<int:token_id>', methods=['OPTIONS'])
    @bp.route('/api/agentapi/admin/tokens/<int:token_id>/stats', methods=['OPTIONS'])
    @bp.route('/api/agentapi/admin/tokens/<int:token_id>/reveal', methods=['OPTIONS'])
    @bp.route('/api/agentapi/admin/tokens/<int:token_id>/reset', methods=['OPTIONS'])
    @bp.route('/api/agentapi/admin/model-agent-map', methods=['OPTIONS'])
    def cors_preflight(**kwargs):
        return '', 204

    # =======================================================================
    # Consumer endpoints — /plugin/ prefix bypasses global enforce_auth
    # Plugin handles its own authentication via Bearer token
    # =======================================================================

    @bp.route('/plugin/agentapi/v1/chat/completions', methods=['POST'])
    def chat_completions():
        """OpenAI-compatible chat completion endpoint.

        Auth: Bearer token — validated inline.
        Routes to the agent specified by the `model` field in the request body,
        resolved via MODEL_AGENT_MAP.
        """
        # --- Auth ---
        token_row, error = _validate_bearer_token()
        if error:
            body, status = error
            return jsonify(body), status

        # --- Parse request ---
        data = request.get_json(silent=True)
        if not data:
            return jsonify({'error': 'Invalid JSON body'}), 400

        model = data.get('model', '').strip()
        if not model:
            return jsonify({'error': 'model field is required'}), 400

        messages = data.get('messages', [])
        stream = data.get('stream', False)

        # --- Resolve model → agent ---
        model_agent_map = _get_model_agent_map()
        agent_id = model_agent_map.get(model)
        if not agent_id:
            return jsonify({'error': f'Unknown model: {model}'}), 400

        # --- Check model scope ---
        if not TokenDB.token_can_access_model(token_row, model):
            return jsonify({'error': f'Token not authorized for model: {model}'}), 403

        # --- Verify agent exists ---
        from models.db import db
        agent = db.get_agent(agent_id)
        if not agent:
            return jsonify({'error': f'Agent not found: {agent_id}'}), 404
        if not agent.get('is_super') and not agent.get('enabled', True):
            return jsonify({'error': f'Agent is disabled: {agent_id}'}), 503

        # --- The machine this agent works in (PIT-408) ---
        # A project agent is bound to its sandbox machine's `exec` workplace. When
        # that machine is reaped the workplace is deleted in the same pass and the
        # binding is left dangling on purpose: refusing here is the point, because
        # the alternative is a tool call that quietly lands somewhere else.
        from plugins.agentapi import sandbox_workplace
        gap = sandbox_workplace.missing(agent)
        if gap:
            return jsonify({'error': gap}), 503

        # --- Build user message ---
        user_message = _build_user_message(messages)
        if not user_message:
            return jsonify({'error': 'No user message found in messages array'}), 400

        # --- Build external_user_id ---
        external_user_id = _generate_external_user_id(token_row, agent_id, request)

        # Sessions are stateful by default — Evonic manages history &
        # auto-summarization. Use X-Session-Id to isolate conversations.
        # X-Clear-Session header can force a fresh session on demand.

        # --- Call agent ---
        from backend.agent_runtime import agent_runtime

        # A message sent while the agent is mid-turn is injected into the
        # running loop, and this endpoint answers `content: null` — an empty
        # success the caller cannot tell from a silent agent. Refuse it with
        # something actionable instead: 409, so a client can queue the message
        # and retry when the turn ends (PIT-254).
        if agent_runtime.is_agent_busy(agent_id):
            return jsonify({'error': 'agent busy', 'session': external_user_id}), 409

        if stream:
            return _stream_response(token_row, agent_id, external_user_id,
                                    user_message, model, model_agent_map)

        try:
            result = agent_runtime.handle_message(
                agent_id=agent_id,
                external_user_id=external_user_id,
                message=user_message,
                channel_id=None,
                skip_buffer=True,
            )
        except Exception as e:
            return jsonify({'error': f'Agent processing failed: {str(e)}'}), 500

        response_text = result.get('response', '')
        if result.get('error'):
            return jsonify({'error': result['error']}), 500

        # handle_message returns response=None for buffered/async paths
        # (message_buffer_seconds > 0). Poll the session's chatlog for the
        # assistant reply instead of returning an empty completion.
        if not response_text and not result.get('async'):
            try:
                from models.db import db
                session_id = db.get_or_create_session(agent_id, external_user_id, None)
                # Anchor: this message was saved before the buffered return,
                # so the reply is any assistant message with id > this user msg.
                user_msgs = [m for m in db.get_session_messages(session_id) if m.get('role') == 'user']
                anchor_id = user_msgs[-1]['id'] if user_msgs else 0
                import time as _time
                deadline = _time.time() + 120
                while _time.time() < deadline:
                    for m in db.get_messages_after(session_id, anchor_id):
                        if m.get('role') == 'assistant' and m.get('content'):
                            response_text = m['content']
                            break
                    if response_text:
                        break
                    _time.sleep(0.3)
            except Exception as e:
                _logger.warning("agentapi poll reply failed for %s: %s", agent_id, e)

        if not response_text:
            return jsonify({'error': 'Agent returned no response'}), 504

        # --- Increment quota + log ---
        token_db.increment_quota(token_row)
        token_db.log_usage(
            token_id=token_row['id'],
            agent_id=agent_id,
            model=model,
            session_id=external_user_id,
            prompt_tokens=0,   # TODO: extract from agent response if available
            completion_tokens=0,
            duration_ms=0,
        )

        # --- Format OpenAI response ---
        return jsonify(_format_openai_response(response_text, model))

    def _stream_response(token_row, agent_id, external_user_id,
                         user_message, model, model_agent_map):
        """Handle streaming SSE response.

        Pre-computes the session_id so event stream handlers can filter
        by session.  The agent is run in a background thread while the
        main thread yields SSE chunks.
        """
        from backend.event_stream import event_stream
        from backend.agent_runtime import agent_runtime
        from models.db import db as _db

        # Pre-compute session_id the same way handle_message does so we can
        # filter events by session.
        session_id = _db.get_or_create_session(agent_id, external_user_id, None)

        # Sessions are stateful by default — Evonic manages history &
        # auto-summarization. Use X-Session-Id to isolate conversations.

        chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())

        q = queue.Queue(maxsize=500)
        _SENTINEL = object()

        def on_chunk(data):
            if data.get('session_id') != session_id:
                return
            content = data.get('content', '')
            if content:
                q.put_nowait(('chunk', content))

        def on_turn_complete(data):
            if data.get('session_id') != session_id:
                return
            q.put_nowait((_SENTINEL, None))

        # Tool calls (PIT-431): one transcript record per call, not a coalesced
        # notification. A card needs the call's own id, its arguments and its
        # output; the coalesced label `bash (1x), read_file (1x)` has none of
        # those and merges two different calls into one row. The Telegram
        # channel keeps its own coalescer (backend/channels/telegram.py), so
        # one line per burst there is unchanged.
        _tool_seq = itertools.count(1)

        def on_tool_executed(data):
            if data.get('session_id') != session_id:
                return
            call_id = f"call-{next(_tool_seq)}"
            for frame in _tool_call_frames(call_id, data):
                q.put_nowait(frame)

        # Thread to run the agent and forward chunks via event_stream
        events_registered = []

        def run_agent():
            event_stream.on('llm_response_chunk', on_chunk)
            event_stream.on('turn_complete', on_turn_complete)
            event_stream.on('tool_executed', on_tool_executed)
            events_registered.extend(['llm_response_chunk', 'turn_complete', 'tool_executed'])

            result = None
            try:
                result = agent_runtime.handle_message(
                    agent_id=agent_id,
                    external_user_id=external_user_id,
                    message=user_message,
                    channel_id=None,
                    skip_buffer=True,
                )
            except Exception:
                result = None
            finally:
                # Only end the stream early when handle_message returned a real
                # reply (non-buffered path). Buffered/async agents return
                # response=None immediately — the real reply arrives later via
                # event_stream (llm_response_chunk/turn_complete), so we must
                # stay subscribed. If we put the sentinel here, generate()
                # exits, the finally below unsubscribes, and the reply events
                # are lost (SSE shows only stop + [DONE]).
                if not result or result.get('response'):
                    q.put_nowait((_SENTINEL, None))

        agent_thread = threading.Thread(target=run_agent, daemon=True)
        agent_thread.start()

        def generate():
            # Send an immediate SSE comment so proxies and clients establish
            # the stream before the first model token is available.
            yield ": open\n\n"
            deadline = time.monotonic() + 600
            try:
                while time.monotonic() < deadline:
                    try:
                        item = q.get(timeout=10)
                    except queue.Empty:
                        # Comments are valid SSE frames and do not change the
                        # OpenAI-compatible payload stream.
                        yield ": keepalive\n\n"
                        if not agent_thread.is_alive() and q.empty():
                            break
                        continue

                    if item[0] is _SENTINEL:
                        break

                    event_type, payload = item
                    if event_type == 'chunk':
                        chunk = {
                            'id': chat_id,
                            'object': 'chat.completion.chunk',
                            'created': created,
                            'model': model,
                            'choices': [{
                                'index': 0,
                                'delta': {'content': payload},
                                'finish_reason': None,
                            }],
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"
                    elif event_type in ('tool_use', 'tool_args', 'tool_result'):
                        # A tool transcript record (PIT-431): the call's own id,
                        # then its arguments, then its output —
                        #   {"type":"tool_use","id":"call-1","name":"bash"}
                        #   {"type":"tool_args","id":"call-1","delta":"{\"cmd\":\"ls\"}"}
                        #   {"type":"tool_result","id":"call-1","content":"a\\nb","is_error":false}
                        # The id is what the client groups the three by; two
                        # calls that share a name stay two records.
                        yield f"data: {json.dumps({'type': event_type, **payload})}\n\n"

                # Send final [DONE] chunk
                final_chunk = {
                    'id': chat_id,
                    'object': 'chat.completion.chunk',
                    'created': created,
                    'model': model,
                    'choices': [{
                        'index': 0,
                        'delta': {},
                        'finish_reason': 'stop',
                    }],
                }
                yield f"data: {json.dumps(final_chunk)}\n\n"
                yield "data: [DONE]\n\n"

                # Increment quota after streaming completes
                token_db.increment_quota(token_row)
                token_db.log_usage(
                    token_id=token_row['id'],
                    agent_id=agent_id,
                    model=model,
                    session_id=external_user_id,
                )
            finally:
                # Clean up event subscriptions
                for ev_name in events_registered:
                    try:
                        if ev_name == 'llm_response_chunk':
                            event_stream.off(ev_name, on_chunk)
                        elif ev_name == 'turn_complete':
                            event_stream.off(ev_name, on_turn_complete)
                        elif ev_name == 'tool_executed':
                            event_stream.off(ev_name, on_tool_executed)
                    except Exception:
                        pass

        return Response(
            stream_with_context(generate()),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
            },
        )

    @bp.route('/plugin/agentapi/v1/models', methods=['GET'])
    def list_models():
        """Return the list of available models filtered by token scope.

        Lightweight auth: token validated but quota NOT consumed.
        """
        token_row, error = _validate_bearer_token()
        if error:
            body, status = error
            return jsonify(body), status

        model_agent_map = _get_model_agent_map()
        visible = TokenDB.token_visible_models(token_row, model_agent_map)

        models = [{
            'id': name,
            'object': 'model',
            'created': 0,
            'owned_by': 'evonic',
        } for name in sorted(visible)]

        return jsonify({
            'object': 'list',
            'data': models,
        })

    # =======================================================================
    # Sandbox machine ↔ workplace (PIT-408)
    #
    # The dashboard holds a machine's `/exec` endpoint and per-session token and
    # has no evonic session, so the lifecycle it drives arrives here, on the one
    # door its bearer token opens. The records are the ordinary workplaces the
    # page renders (`routes/workplaces.py`) — nothing here is a second store.
    # =======================================================================

    def _agent_for_model(model: str, token_row: dict):
        """(agent_id, (body, status)) for a caller that may use `model`."""
        agent_id = _get_model_agent_map().get(model)
        if not agent_id:
            return None, ({'error': f'Unknown model: {model}'}, 400)
        if not TokenDB.token_can_access_model(token_row, model):
            return None, ({'error': f'Token not authorized for model: {model}'}, 403)
        from models.db import db
        if not db.get_agent(agent_id):
            return None, ({'error': f'Agent not found: {agent_id}'}, 404)
        return agent_id, None

    @bp.route('/plugin/agentapi/v1/workplace', methods=['POST'])
    def sandbox_workplace_open():
        """Open, or re-point, the agent's `exec` workplace at a machine.

        Body: `{model, name, url, token, workspace_path?}`. `name` is what the
        operator sees (`<login>@<machine>`); `url`/`token` are that machine's
        `/exec` endpoint and `SANDBOX_EXEC_TOKEN`. Answers 200 with the record.
        """
        token_row, error = _validate_bearer_token()
        if error:
            body, status = error
            return jsonify(body), status
        data = request.get_json(silent=True) or {}
        model = (data.get('model') or '').strip()
        agent_id, error = _agent_for_model(model, token_row)
        if error:
            body, status = error
            return jsonify(body), status

        name = (data.get('name') or '').strip()
        url = (data.get('url') or '').strip()
        machine_token = (data.get('token') or '').strip()
        if not (name and url and machine_token):
            return jsonify({'error': 'name, url and token are required'}), 400

        from plugins.agentapi import sandbox_workplace
        try:
            record = sandbox_workplace.bind(agent_id, name, url, machine_token)
        except Exception as e:
            _logger.warning('[sandbox_workplace] bind failed for agent %s: %s', agent_id, e,
                            exc_info=True)
            return jsonify({'error': f'could not record the workplace: {e}'}), 503
        return jsonify(record), 200

    @bp.route('/plugin/agentapi/v1/workplace', methods=['DELETE'])
    def sandbox_workplace_close():
        """Close the agent's `exec` workplace — the machine it named is gone.

        Body/query: `{model}`. Idempotent: an agent with no exec workplace answers
        `{"closed": false}`, so the reap pass can call it for every project it
        stops without reading the current bindings first.
        """
        token_row, error = _validate_bearer_token()
        if error:
            body, status = error
            return jsonify(body), status
        payload = request.get_json(silent=True) or {}
        model = (payload.get('model') or request.args.get('model') or '').strip()
        agent_id, error = _agent_for_model(model, token_row)
        if error:
            body, status = error
            return jsonify(body), status

        from plugins.agentapi import sandbox_workplace
        try:
            closed = sandbox_workplace.close(agent_id)
        except Exception as e:
            _logger.warning('[sandbox_workplace] close failed for agent %s: %s', agent_id, e,
                            exc_info=True)
            return jsonify({'error': f'could not close the workplace: {e}'}), 503
        return jsonify({'closed': closed}), 200

    # =======================================================================
    # Provisioning (PIT-430) — the agent a new project chats with
    # =======================================================================
    # A dashboard create must answer with a project that can chat on its first
    # turn, and the dashboard holds no evonic session — only this plugin's
    # bearer tokens. So the one agent-shaped thing a create needs (an agent, its
    # model alias, its token) is minted here: same door as the workplace route
    # above, same reason. Only the fleet token (`allowed_models: "*"`) may
    # provision; a project token is scoped to one model and is refused, so a
    # leaked project token cannot make agents.

    def _is_fleet_token(token_row: dict) -> bool:
        return token_row.get('allowed_models') in (None, '', '*')

    def _write_model_alias(model: str, agent_id):
        """Point, or unpoint, a model name at an agent in MODEL_AGENT_MAP."""
        from models.db import db
        cfg = _get_model_agent_map()
        if agent_id is None:
            cfg.pop(model, None)
        else:
            cfg[model] = agent_id
        db.set_setting('plugin_config:agentapi:MODEL_AGENT_MAP',
                       json.dumps(cfg, indent=2, sort_keys=True))

    def _purge_agent(agent_id: str) -> bool:
        """Remove an agent and every row that outlives its `agents` row.

        `db.delete_agent` deletes the agents table row and nothing else, so a
        re-provision of the same slug would collide on `agent_tools`; and the
        agent's own tree only goes with its directories.
        """
        from models.db import db
        from routes.agents import AGENTS_DIR, WORKSPACE_DIR
        db.set_agent_tools(agent_id, [])
        db.set_agent_skills(agent_id, [])
        db.set_agent_variables_bulk(agent_id, [])
        removed = bool(db.get_agent(agent_id)) and db.delete_agent(agent_id)
        for d in (os.path.join(AGENTS_DIR, agent_id),
                  os.path.join(WORKSPACE_DIR, agent_id)):
            shutil.rmtree(d, ignore_errors=True)
        # A purge that leaves no trace cannot be told apart from rows that
        # vanished: log every one, including the rollback path's.
        _logger.info('[provision] agent %s: purged (removed=%s)', agent_id, removed)
        return removed

    # What a project agent is told: generic engineering discipline plus the two
    # facts that are this site's (its project and the tree it works in). The
    # source agent's own prose is not copied — `demo`'s names its repo, its
    # deploy pipeline and a content file a new site does not have.
    PROJECT_PROMPT = """You are the engineer for {name}, a Pitura site (project `{slug}`).

You work in `/workspace` inside the project's machine — that checkout is the
site, and a turn's edits are committed there for you. Use relative paths.

## Guidelines

- Always git commit after completing a task, unless explicitly told not to, and
  report the commit hash. Stage files explicitly; never `git add -A` or `.`.
- Make sure you only commit the changes you just made.
- Do not push git unless explicitly asked.
- Always do a sanity check after a coding task: verify the change took effect
  and report what you verified.
- If a task is ambiguous or the workspace differs from what you expected, say so
  instead of guessing.
- Your site's repository on the Pitura host is /srv/clients/{slug}.
"""

    @bp.route('/plugin/agentapi/v1/provision', methods=['POST'])
    def provision_agent():
        """Create a project's agent and mint its token.

        Body: `{slug, name, source?}`. `slug` is the project's subdomain; the
        agent id is that slug with `-` → `_` (evonic ids are `^[a-z0-9_]+$`) and
        the model alias is the agent id, which is what the dashboard sends as
        the chat `model`. `source` names the agent whose tool set and settings
        are cloned, default `demo`.

        Answers `201 {agent_id, model, agent_url_hint, token}` — `token` is the
        plaintext bearer token, the only time it exists. `409` when the agent id
        is already taken: an agent is a project's, so a create that collides is
        a create that must roll back, not one that adopts a stranger's agent.
        """
        token_row, error = _validate_bearer_token()
        if error:
            body, status = error
            return jsonify(body), status
        if not _is_fleet_token(token_row):
            return jsonify({'error': 'provisioning needs a fleet token'}), 403

        data = request.get_json(silent=True) or {}
        slug = (data.get('slug') or '').strip().lower()
        name = (data.get('name') or '').strip() or slug
        source = (data.get('source') or 'demo').strip()
        if not slug or not _PROJECT_SLUG_RE.match(slug):
            return jsonify({'error': 'slug must be a project slug'}), 400
        agent_id = slug.replace('-', '_')

        from models.db import db
        if db.get_agent(agent_id):
            return jsonify({'error': f"agent '{agent_id}' already exists"}), 409
        if not db.get_agent(source):
            return jsonify({'error': f"source agent '{source}' not found"}), 400

        try:
            db.clone_agent(source, agent_id, name, f"Pitura site {slug}")
            prompt = PROJECT_PROMPT.format(name=name, slug=slug)
            # SYSTEM.md is what the runtime reads (`routes/agents.py`
            # `_read_system_prompt`); `update_agent` refuses `system_prompt`
            # (its allowed set), so the file is the one place it is written.
            from routes.agents import WORKSPACE_DIR, _write_system_prompt
            _write_system_prompt(agent_id, prompt)
            os.makedirs(os.path.join(WORKSPACE_DIR, agent_id), exist_ok=True)
        except Exception as e:  # noqa: BLE001 - a half-made agent must not stay
            _logger.warning('[provision] %s: could not create agent: %s', slug, e,
                            exc_info=True)
            try:
                _purge_agent(agent_id)
            except Exception:  # noqa: BLE001
                pass
            return jsonify({'error': f'could not create the agent: {e}'}), 500

        row = token_db.create_token({
            'name': f'pitura:{agent_id}',
            'allowed_models': [agent_id],
        })
        if not row or not row.get('token'):
            db.delete_agent(agent_id)
            return jsonify({'error': 'could not mint the agent token'}), 500
        _write_model_alias(agent_id, agent_id)
        _logger.info('[provision] project %s: agent %s + token %s',
                     slug, agent_id, row.get('token_prefix'))
        return jsonify({
            'agent_id': agent_id,
            'model': agent_id,
            'token': row['token'],
        }), 201

    @bp.route('/plugin/agentapi/v1/provision', methods=['DELETE'])
    def deprovision_agent():
        """Remove a project's agent, its token and its model alias.

        Body/query: `{slug}`. Idempotent — a create that rolled back before the
        agent existed, or a retry, answers `{"removed": false}` rather than an
        error.
        """
        token_row, error = _validate_bearer_token()
        if error:
            body, status = error
            return jsonify(body), status
        if not _is_fleet_token(token_row):
            return jsonify({'error': 'provisioning needs a fleet token'}), 403

        payload = request.get_json(silent=True) or {}
        slug = (payload.get('slug') or request.args.get('slug') or '').strip().lower()
        if not slug or not _PROJECT_SLUG_RE.match(slug):
            return jsonify({'error': 'slug must be a project slug'}), 400
        agent_id = slug.replace('-', '_')

        from models.db import db
        removed = _purge_agent(agent_id)
        dropped = 0
        for t in token_db.list_tokens():
            if t.get('name') == f'pitura:{agent_id}':
                token_db.delete_token(t['id'])
                dropped += 1
        _write_model_alias(agent_id, None)
        _logger.info('[provision] project %s: deprovisioned, agent removed=%s, %d token(s)',
                     slug, removed, dropped)
        return jsonify({'removed': removed}), 200

    # =======================================================================
    # Admin endpoints — /api/ prefix → session auth via global enforce_auth
    # =======================================================================

    @bp.route('/api/agentapi/admin')
    def admin_dashboard():
        """Admin dashboard page for token management."""
        return render_template('admin.html',
                               model_agent_map=_get_model_agent_map())

    @bp.route('/api/agentapi/admin/tokens', methods=['GET'])
    def admin_list_tokens():
        """List all tokens. Query params: ?status=active|suspended"""
        status = request.args.get('status', '').strip() or None
        tokens = token_db.list_tokens(status=status)
        # Mask: never expose token_hash or full token
        result = []
        for t in tokens:
            result.append({
                'id': t['id'],
                'name': t['name'],
                'token_prefix': t['token_prefix'],
                'quota_limit': t.get('quota_limit'),
                'quota_used': t.get('quota_used', 0),
                'status': t.get('status', 'active'),
                'expires_at': t.get('expires_at'),
                'allowed_models': _parse_allowed_models(t.get('allowed_models')),
                'last_used_at': t.get('last_used_at'),
                'created_at': t.get('created_at'),
            })
        return jsonify({'tokens': result})

    @bp.route('/api/agentapi/admin/tokens', methods=['POST'])
    def admin_create_token():
        """Create a new bearer token.

        Body: {"name": "...", "quota_limit": 1000, "expires_at": "...",
               "allowed_models": ["gpt-4-assistant"]}
        quota_limit, expires_at, allowed_models are optional.
        allowed_models defaults to "*" (all models).
        Returns the plaintext token ONCE.
        """
        data = request.get_json(silent=True)
        if not data:
            return jsonify({'error': 'Invalid JSON body'}), 400

        name = data.get('name', '').strip()
        if not name:
            return jsonify({'error': 'name is required'}), 400

        # Validate allowed_models against MODEL_AGENT_MAP
        model_agent_map = _get_model_agent_map()
        allowed_models = data.get('allowed_models') or '*'
        if isinstance(allowed_models, list):
            unknown = [m for m in allowed_models if m not in model_agent_map]
            if unknown:
                return jsonify({
                    'error': f'Unknown model(s): {", ".join(unknown)}',
                }), 400

        # Validate expires_at if provided
        expires_at = data.get('expires_at')
        if expires_at:
            try:
                datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
            except (ValueError, TypeError):
                return jsonify({'error': 'Invalid expires_at format. Use ISO 8601.'}), 400

        row = token_db.create_token({
            'name': name,
            'quota_limit': data.get('quota_limit'),
            'expires_at': expires_at,
            'allowed_models': allowed_models,
        })

        if not row:
            return jsonify({'error': 'Failed to create token'}), 500

        plaintext_token = row.pop('token', None)
        row['allowed_models'] = _parse_allowed_models(row.get('allowed_models'))

        # Cache plaintext temporarily for the /reveal endpoint
        if plaintext_token and row.get('id'):
            _plaintext_cache[row['id']] = plaintext_token

        return jsonify({
            'token': row,
            'plaintext': plaintext_token,
        }), 201

    @bp.route('/api/agentapi/admin/tokens/<int:token_id>', methods=['PUT'])
    def admin_update_token(token_id):
        """Update a token's mutable fields. All fields optional."""
        data = request.get_json(silent=True)
        if not data:
            return jsonify({'error': 'Invalid JSON body'}), 400

        # Validate / normalise allowed_models if provided
        if 'allowed_models' in data:
            am = data['allowed_models']
            if am is None or am == '':
                data['allowed_models'] = '*'
            elif isinstance(am, list):
                model_agent_map = _get_model_agent_map()
                unknown = [m for m in am if m not in model_agent_map]
                if unknown:
                    return jsonify({
                        'error': f'Unknown model(s): {", ".join(unknown)}',
                    }), 400

        # Validate expires_at if provided
        if 'expires_at' in data and data['expires_at'] is not None:
            try:
                datetime.fromisoformat(
                    data['expires_at'].replace('Z', '+00:00'))
            except (ValueError, TypeError):
                return jsonify({
                    'error': 'Invalid expires_at format. Use ISO 8601.',
                }), 400

        ok = token_db.update_token(token_id, data)
        if not ok:
            return jsonify({'error': 'Token not found or no fields to update'}), 404

        row = token_db._get_by_id(token_id)
        if row:
            row['allowed_models'] = _parse_allowed_models(row.get('allowed_models'))

        return jsonify({'token': row})

    @bp.route('/api/agentapi/admin/tokens/<int:token_id>', methods=['DELETE'])
    def admin_delete_token(token_id):
        """Delete a token permanently."""
        ok = token_db.delete_token(token_id)
        if not ok:
            return jsonify({'error': 'Token not found'}), 404
        return '', 204

    @bp.route('/api/agentapi/admin/tokens/<int:token_id>/stats',
              methods=['GET'])
    def admin_token_stats(token_id):
        """Return detailed usage stats for a token."""
        stats = token_db.get_token_stats(token_id)
        if not stats:
            return jsonify({'error': 'Token not found'}), 404
        stats['allowed_models'] = _parse_allowed_models(
            stats.get('allowed_models'))
        return jsonify({'stats': stats})

    @bp.route('/api/agentapi/admin/tokens/<int:token_id>/reveal',
              methods=['GET'])
    def admin_reveal_token(token_id):
        """Return the plaintext token if cached (creation-time only)."""
        plaintext = _plaintext_cache.get(token_id)
        if not plaintext:
            return jsonify({'error': 'Plaintext token no longer available'}), 404
        return jsonify({'plaintext': plaintext})

    @bp.route('/api/agentapi/admin/tokens/<int:token_id>/reset',
              methods=['POST'])
    def admin_reset_token(token_id):
        """Regenerate the secret key for a token. Returns the new plaintext
        once; the old key is immediately invalidated."""
        plaintext = token_db.reset_token(token_id)
        if not plaintext:
            return jsonify({'error': 'Token not found'}), 404
        # Cache for /reveal endpoint
        _plaintext_cache[token_id] = plaintext
        return jsonify({'plaintext': plaintext})

    # =======================================================================
    # Model-agent mapping helper (for admin UI)
    # =======================================================================

    @bp.route('/api/agentapi/admin/model-agent-map', methods=['GET'])
    def admin_model_agent_map():
        """Return the current MODEL_AGENT_MAP for the admin UI."""
        return jsonify({'map': _get_model_agent_map()})

    @bp.route('/plugin/agentapi/v1/usage', methods=['GET'])
    def api_usage():
        """Self-serve usage for the calling bearer token.

        Returns the API-token quota (request count) plus the LLM token usage
        for the agent(s) this token can reach (via MODEL_AGENT_MAP), so the
        client dashboard can show used/limit visibility.
        """
        token_row, err = _validate_bearer_token()
        if err:
            return jsonify(err[0]), err[1]

        quota_limit = token_row.get('quota_limit')
        quota_used = token_row.get('quota_used', 0)

        # LLM token usage — sum across agents this token can reach.
        # Prefer the single mapped agent; fall back to all allowed models.
        model_agent_map = _get_model_agent_map()
        agent_ids = []
        allowed = _parse_allowed_models(token_row.get('allowed_models'))
        if allowed == ['*']:
            # Token can reach every agent — sum usage across all mapped agents.
            agent_ids = list(dict.fromkeys(model_agent_map.values()))
        else:
            agent_ids = [model_agent_map.get(m) for m in allowed if m in model_agent_map]
        agent_ids = [a for a in dict.fromkeys(agent_ids) if a]

        used_today = 0
        limit_daily = None
        from plugins.token_monitor.db import usage_db
        now_utc = datetime.now(timezone.utc)
        day_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        next_midnight = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        for aid in agent_ids:
            t = usage_db.agent_total_tokens(aid, since_iso=day_start)
            if t is not None:
                used_today += t
        if agent_ids:
            from models.db import db as _db
            _agent = _db.get_agent(agent_ids[0])
            if _agent:
                limit_daily = _agent.get('token_limit_daily')
                if limit_daily is None:
                    import os as _os
                    try:
                        limit_daily = int(_os.environ.get('EVONIC_TOKEN_LIMIT_DAILY_DEFAULT', '500000') or '500000')
                    except (TypeError, ValueError):
                        limit_daily = 500000

        return jsonify({
            'quota': {
                'limit': quota_limit,
                'used': quota_used,
            },
            'llm_tokens': {
                'used_today': used_today,
                'limit_daily': limit_daily,
                'agents': agent_ids,
                'reset_at': next_midnight.isoformat(),
            },
        })

    return bp
