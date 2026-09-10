"""Single long-poll consumer. Logs no invitations, confirmation codes or chat IDs."""
from __future__ import annotations
import logging
import signal
import threading
import time
from guardian_store import GuardianStore
from telegram_api import TelegramClient, TelegramError
from telegram_worker import DATABASE_PATH, read_credential, retry_delay, worker_lock

LOGGER = logging.getLogger('carecall_registration')

def send_reply(store, client):
    job = store.claim_reply()
    if job is None:
        return False, 0
    try:
        message_id = client.send(job['chat_id'], job['text'])
    except TelegramError as exc:
        retryable = exc.retryable or exc.fatal
        delay = max(300 if exc.fatal else 0, retry_delay(job['attempts'], exc.retry_after))
        store.finish_reply(job['id'], error=exc.code,
                           retry_at=time.time()+delay if retryable else None)
        LOGGER.warning('Registration reply deferred reply=%s code=%s', job['id'], exc.code)
        if exc.fatal:
            raise
        return True, max(1.1, delay) if exc.code == 'http_429' else 1.1
    store.finish_reply(job['id'], message_id=message_id)
    LOGGER.info('Registration reply sent reply=%s message_id=%s', job['id'], message_id)
    return True, 1.1

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    stopped = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())
    try:
        client = TelegramClient(read_credential())
        store = GuardianStore(DATABASE_PATH)
        with worker_lock(DATABASE_PATH.parent / '.telegram-registration.lock'):
            while not stopped.is_set():
                try:
                    client.verify_bot()
                    if client.request('getWebhookInfo').get('url'):
                        raise TelegramError('webhook_already_configured', fatal=True)
                    break
                except TelegramError as exc:
                    if not exc.retryable:
                        raise
                    LOGGER.warning('Registration startup retry code=%s', exc.code)
                    stopped.wait(max(5, exc.retry_after))
            if stopped.is_set():
                return 0
            store.recover_replies()
            LOGGER.info('Guardian registration ready bot=@carecall_research_alert_bot')
            while not stopped.is_set():
                sent, delay = send_reply(store, client)
                if stopped.wait(delay):
                    break
                try:
                    updates = client.request('getUpdates', {
                        'offset':store.cursor()+1, 'limit':50,
                        'timeout':0 if sent else 10, 'allowed_updates':['message'],
                    })
                    for update in updates:
                        if stopped.is_set():
                            break
                        outcome = store.apply_update(update)
                        # Only a constant outcome; never the raw message/deep link.
                        if outcome == 'pending':
                            LOGGER.info('Guardian request pending; use guardian_admin.py pending')
                except TelegramError as exc:
                    if exc.code == 'http_409':
                        raise TelegramError('another_getUpdates_consumer_or_webhook', fatal=True) from None
                    if not exc.retryable:
                        raise
                    LOGGER.warning('Registration polling retry code=%s', exc.code)
                    stopped.wait(max(5, exc.retry_after))
        return 0
    except TelegramError as exc:
        LOGGER.error('Registration stopped code=%s', exc.code)
        return 2 if exc.fatal else 1
    except Exception as exc:
        LOGGER.error('Registration stopped error_type=%s', type(exc).__name__)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
