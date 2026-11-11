"""PIT-254: a turn on a busy agent answers 409, not an empty 200.

A message sent into a running loop is injected into it and the endpoint
returns ``{"content": null}`` — a success the caller cannot tell from a silent
agent. These pin the guard in ``chat_completions`` that turns that into
something a client can queue on.
"""
import unittest
from unittest.mock import patch

from flask import Flask

from plugins.agentapi.routes import create_blueprint

MODEL = 'cheap'
AGENT = 'fitrafep_com'


class BusyAgentTest(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(create_blueprint())
        self.client = app.test_client()

    def post(self, busy, handle_message=None):
        token = {'id': 1}
        patchers = [
            patch('plugins.agentapi.routes._validate_bearer_token', return_value=(token, None)),
            patch('plugins.agentapi.routes._get_model_agent_map', return_value={MODEL: AGENT}),
            patch('plugins.agentapi.routes.TokenDB.token_can_access_model', return_value=True),
            patch('plugins.agentapi.routes._generate_external_user_id', return_value='sess-1'),
            patch('models.db.db.get_agent', return_value={'id': AGENT}),
            patch('backend.agent_runtime.agent_runtime.is_agent_busy', return_value=busy),
        ]
        if handle_message is not None:
            patchers.append(
                patch('backend.agent_runtime.agent_runtime.handle_message', return_value=handle_message())
            )
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        return self.client.post(
            '/plugin/agentapi/v1/chat/completions',
            json={'model': MODEL, 'messages': [{'role': 'user', 'content': 'halo'}], 'stream': False},
        )

    def test_busy_is_409_with_the_session(self):
        res = self.post(busy=True)
        self.assertEqual(res.status_code, 409)
        body = res.get_json()
        self.assertEqual(body['error'], 'agent busy')
        self.assertEqual(body['session'], 'sess-1')

    def test_free_agent_still_answers(self):
        def ok():
            return {'response': 'Baik.'}

        res = self.post(busy=False, handle_message=ok)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()['choices'][0]['message']['content'], 'Baik.')


if __name__ == '__main__':
    unittest.main()
