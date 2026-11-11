"""Offline integration checks against the installed AgentDuet SDK.

Run: python -W error::DeprecationWarning -m unittest discover -s tests -v
"""
import asyncio
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

from agentduet import Address, IncomingCallNotification
from agentduet.exceptions import CallNotFoundError

ROOT = Path(__file__).resolve().parents[1]


def load_agent(folder, name):
    sys.path.insert(0, str(ROOT / folder))
    try:
        spec = importlib.util.spec_from_file_location(name, ROOT / folder / 'agent.py')
        module = importlib.util.module_from_spec(spec)
        # Do not load credentials or make external requests in tests.
        with patch('dotenv.load_dotenv'):
            spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


resolve = load_agent('resolve-bot', 'resolve_agent')
import handlers
payment = load_agent('driver-payment-assistant', 'payment_agent')


class SDKCompatibility(unittest.IsolatedAsyncioTestCase):
    def notification(self):
        return IncomingCallNotification(
            call_id='test-call', subscriber_id='+15550000001',
            from_address=Address.telco('+15550000002'),
            to_address=Address.telco('+15550000001'), created_at=0,
        )

    async def test_claim_uses_subscriber_id(self):
        for module in (handlers, payment):
            with self.subTest(module=module.__name__):
                session = Mock(process_call=AsyncMock(return_value=object()))
                sm = Mock(open_session=AsyncMock(return_value=session))
                noti = self.notification()
                call = await module.attach_inbound_call(sm, noti)
                self.assertEqual(sm.open_session.call_args.args[1], noti.subscriber_id)
                session.process_call.assert_awaited_once_with(noti)
                self.assertIs(call, session.process_call.return_value)

    async def test_expired_claim_is_not_retried(self):
        for module in (handlers, payment):
            with self.subTest(module=module.__name__):
                session = Mock(process_call=AsyncMock(side_effect=CallNotFoundError('expired')))
                sm = Mock(open_session=AsyncMock(return_value=session))
                with self.assertRaises(CallNotFoundError):
                    await module.attach_inbound_call(sm, self.notification())
                session.process_call.assert_awaited_once()
                sm.open_session.assert_awaited_once()

    async def test_handler_cancellation_cleans_up(self):
        for module in (handlers, payment):
            with self.subTest(module=module.__name__):
                started = asyncio.Event()
                async def prepare():
                    started.set()
                    await asyncio.Event().wait()
                call = Mock(answer=AsyncMock(return_value=True), close=AsyncMock())
                nova = Mock(prepare=AsyncMock(side_effect=prepare), cancel=AsyncMock(),
                            shutdown=AsyncMock(), run_bridge=AsyncMock())
                with patch.object(module, 'attach_inbound_call', AsyncMock(return_value=call)), \
                     patch.object(module, 'NovaSonicIntegration', return_value=nova):
                    task = asyncio.create_task(module.handle_voice_call(
                        Mock(), self.notification(), Mock(), Mock()))
                    await started.wait()
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                call.close.assert_awaited()
                (nova.shutdown if module is handlers else nova.cancel).assert_awaited()
                nova.run_bridge.assert_not_awaited()

    def test_bedrock_clients_construct(self):
        for module in (resolve, payment):
            self.assertIsNotNone(module.create_bedrock_client())


if __name__ == '__main__':
    unittest.main()
