import base64
import json
from pathlib import Path
import tempfile
import unittest
import threading
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from xangi_dots.server import make_server

from xangi_dots.events import Events
from xangi_dots.mcp import rpc
from xangi_dots.store import Store


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'inbox.sqlite'
        self.store = Store(self.path)
        self.sent = []

        def send(url, body, headers):
            data = json.loads(body)
            self.sent.append(data)
            return 200, json.dumps({'challenge': data.get('challenge')}).encode()

        self.events = Events(self.store, send)
        self.params = dict(name='job.created', arguments={'queue_id': 'dots'},
                           delivery={'mode': 'webhook', 'url': 'https://chatgpt.com/callback',
                                     'secret': 'whsec_' + base64.b64encode(b's' * 32).decode()})
        self.job = self.store.enqueue('test', 'work', {'platform': 'discord', 'channel': 'origin'})
        self.claim = self.store.claim(self.job['id'], 'owner')

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_running_delivery_dedup_and_claim_preservation(self):
        self.events.subscribe(self.params)
        note = self.store.followup(self.job['id'], 'followup1', 'status please')
        self.assertEqual(note, self.store.followup(self.job['id'], 'followup1', 'status please'))
        with self.assertRaises(ValueError):
            self.store.followup(self.job['id'], 'followup1', 'different')
        self.events.dispatch()
        self.events.dispatch()
        messages = [d for d in self.sent if d.get('name') == 'job.created']
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]['data']['job_id'], self.job['id'])
        self.assertEqual(self.store.claim(self.job['id'], 'owner'), self.claim)
        self.assertEqual(len(self.store.jobs()), 1)
        self.assertIsNone(self.store.get(self.job['id'])['followups'][0]['acknowledged_at'])
        with self.assertRaises(ValueError):
            self.store.acknowledge_followup(self.job['id'], 'bad', note['id'])
        args = dict(job_id=self.job['id'], claim_token=self.claim['claim_token'], message_id=note['id'])
        result = rpc(self.store, self.events, {'method': 'tools/call', 'params': {
            'name': 'acknowledge_followup', 'arguments': args}})
        self.assertNotIn('isError', result)
        first = self.store.acknowledge_followup(**args)
        self.assertEqual(first, self.store.acknowledge_followup(**args))
        self.assertIsNotNone(first['acknowledged_at'])
        self.assertEqual(self.store.get(self.job['id'])['status'], 'running')

    def test_persistence_late_subscription_and_terminal_rejection(self):
        self.store.followup(self.job['id'], 'followup1', 'status please')
        self.store.db.close()
        self.store = Store(self.path)
        self.events.store = self.store
        self.events.subscribe(self.params)
        self.events.dispatch()
        self.assertTrue(any(d.get('name') == 'job.created' for d in self.sent))
        self.store.complete(self.job['id'], self.claim['claim_token'], 'done')
        with self.assertRaises(ValueError):
            self.store.followup(self.job['id'], 'followup2', 'continue')

    def test_terminal_cancels_pending_delivery(self):
        self.events.subscribe(self.params)
        self.store.followup(self.job['id'], 'followup1', 'status please')
        self.store.cancel(self.job['id'])
        self.events.dispatch()
        self.assertFalse(any(d.get('name') == 'job.created' for d in self.sent))

    def test_admin_http_endpoint_and_auth(self):
        server = make_server(self.store, self.events, 'admin-secret')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/jobs/{self.job["id"]}/followups'
            body = json.dumps({'request_key': 'http', 'message': 'status'}).encode()
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(url, data=body, headers={'Content-Type': 'application/json'}))
            self.assertEqual(error.exception.code, 401)
            with urlopen(Request(url, data=body, headers={
                'Content-Type': 'application/json', 'Authorization': 'Bearer admin-secret'
            })) as response:
                self.assertEqual(response.status, 201)
                note = json.load(response)
            self.assertEqual(note['message'], 'status')
            self.assertEqual(len(self.store.get(self.job['id'])['followups']), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
