"""No network or production DB access. Temporary databases and fake Telegram only."""
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from event_database import EventDatabase
from message_validator import validate_call_message
from notification_store import NotificationStore, utc_now
from confirmation_store import ConfirmationStore
from operator_store import OperatorStore
from telegram_worker import process_one, render_message
from telegram_api import TelegramError, TelegramClient

TOPIC = 'carecall/v1/devices/button01/call'

class Telegram:
    def __init__(self):
        self.sent=[]; self.edits=[]; self.failure=None
    def send(self, chat, text):
        self.sent.append((chat,text))
        return len(self.sent)+100
    def edit_markup(self, chat, mid, markup):
        self.edits.append((chat,mid,markup))
        if self.failure:
            raise self.failure

class Confirmations(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'events.db'
        self.db=EventDatabase(self.path)
        self.store=NotificationStore(self.path)
        self.confirm=ConfirmationStore(self.path)
        self.operator=OperatorStore(self.path); self.operator.migrate()
        self.store.register_first_recipient(111)
        self.client=Telegram(); self.update_id=1
    def sql(self, statement, args=()):
        with closing(self.store.connect()) as c:
            return [dict(r) for r in c.execute(statement,args).fetchall()]
    def call(self,n,boot='0000000000001234'):
        event=f'button01-{boot}-{n:08d}'
        self.db.save_call(validate_call_message(TOPIC,json.dumps(dict(schema_version=1,
            event_id=event,device_id='button01',event_type='care_call',sequence=n,uptime_ms=n*1000)).encode()))
        return event
    def send(self):
        process_one(self.store,self.client,now=100)
    def message(self,event,chat=111):
        return self.sql('''SELECT o.*,m.token FROM notification_outbox o
            JOIN confirmation_messages m USING(notification_id) WHERE event_id=? AND chat_id=?''',(event,chat))[0]
    def click(self,row,actor=None,mid=None):
        chat=row['chat_id'] if actor is None else actor
        update={'update_id':self.update_id,'callback_query':{'id':str(self.update_id),
            'from':{'id':chat,'is_bot':False},'data':'c:'+row['token'],
            'message':{'message_id':row['telegram_message_id'] if mid is None else mid,
                'chat':{'id':chat,'type':'private'}}}}
        self.update_id+=1
        return self.operator.apply_update(update,now=100)
    def test_notification_text_unchanged_and_button_only_after_known_send(self):
        event=self.call(1)
        self.assertIsNone(self.confirm.next_markup())
        self.send(); self.assertEqual(len(self.client.sent),1)
        self.assertEqual(self.client.edits,[])
        self.send(); self.assertEqual(len(self.client.edits),1)
        self.assertEqual(self.client.edits[0][2]['inline_keyboard'][0][0]['text'],'확인했습니다.')
        callback=self.client.edits[0][2]['inline_keyboard'][0][0]['callback_data']
        self.assertEqual(len(callback.encode()),34)
        self.assertEqual(render_message({'received_at':'2026-09-18T05:25:56+00:00'}),
            '[사용자가 보호자를 호출]\n호출 시간 : 2026/09/18 14:25:56\n호출 메시지 : 보호자의 도움을 요청하고 있습니다. 확인해주시길 바랍니다.')
    def test_new_call_removes_previous_keyboard_without_editing_text(self):
        old=self.call(1); self.send(); self.send()
        new=self.call(2)
        self.send(); self.assertEqual(self.client.edits[-1][2],{'inline_keyboard':[]})
        self.send(); self.send()
        self.assertEqual(len(self.client.sent),2)
        self.assertEqual(self.click(self.message(old)),'call_stale')
        self.assertEqual(self.click(self.message(new)),'call_confirmed')
    def test_generation_order_over_arrival_order_and_duplicates(self):
        new=self.call(5); old=self.call(4); self.call(5)
        self.assertEqual(self.sql('SELECT * FROM confirmation_latest')[0]['event_id'],new)
        for _ in range(4): self.send()
        self.assertEqual(len(self.client.sent),2)
        self.assertEqual(len(self.client.edits),1)
        self.assertEqual(self.click(self.message(old)),'call_stale')
        self.assertEqual(self.click(self.message(new)),'call_confirmed')
        self.assertEqual(self.sql('SELECT delivery_count FROM call_events WHERE event_id=?',(new,))[0]['delivery_count'],2)
    def test_authorized_any_guardian_and_one_confirmation(self):
        self.sql('INSERT INTO notification_recipients(device_id,chat_id,created_at) VALUES(?,?,?)',('button01',222,utc_now()))
        event=self.call(1)
        for _ in range(4): self.send()
        a,b=self.message(event),self.message(event,222)
        self.assertEqual(self.click(a,actor=999),'call_forbidden')
        self.assertEqual(self.click(a,mid=999),'call_forbidden')
        self.sql('UPDATE notification_recipients SET enabled=0 WHERE chat_id=111')
        self.assertEqual(self.click(a),'call_forbidden')
        self.assertEqual(self.click(b),'call_confirmed')
        self.assertEqual(self.click(b),'call_already_confirmed')
        for _ in range(2): self.send()
        self.assertEqual(self.sql("SELECT count(*) AS n FROM confirmation_messages WHERE applied='button'")[0]['n'],0)
    def test_durable_downlink_retries_until_device_report(self):
        event=self.call(1); self.send(); row=self.message(event)
        self.assertEqual(self.click(row),'call_confirmed')
        self.assertEqual(self.confirm.claim_command(100)['event_id'],event)
        self.assertIsNone(self.confirm.claim_command(109))
        reopened=ConfirmationStore(self.path)
        self.assertEqual(reopened.claim_command(110)['event_id'],event)
        report=dict(schema_version=1,event_type='care_confirmation_result',event_id=event,device_id='button01',status='applied')
        self.assertTrue(reopened.receive_device_result(TOPIC,json.dumps(report).encode()))
        self.assertIsNone(reopened.claim_command(200))
        self.assertEqual(self.db.count_events(),1)
    def test_new_call_stops_old_command_retries(self):
        event=self.call(1); self.send(); self.click(self.message(event))
        self.call(2); self.assertIsNone(self.confirm.claim_command(100))
    def test_report_schema_and_topic_guard(self):
        report=dict(schema_version=1,event_type='care_confirmation_result',event_id='abc',device_id='button01',status='applied')
        with self.assertRaises(ValueError): self.confirm.receive_device_result('wrong',json.dumps(report).encode())
        report['schema_version']=True
        with self.assertRaises(ValueError): self.confirm.receive_device_result(TOPIC,json.dumps(report).encode())
        self.assertFalse(self.confirm.receive_device_result(TOPIC,b'{bad'))
    def test_timeout_during_add_then_new_call_still_removes_old_keyboard(self):
        old=self.call(1); self.send()
        self.client.failure=TelegramError('network_or_response_ambiguous',retryable=True)
        with patch('telegram_worker.time.time',return_value=100): self.send()
        self.assertEqual(self.sql('SELECT applied FROM confirmation_messages')[0]['applied'],'unknown')
        self.call(2); self.client.failure=None
        process_one(self.store,self.client,now=106)
        self.assertEqual(self.client.edits[-1][2],{'inline_keyboard':[]})
        self.assertEqual(self.click(self.message(old)),'call_stale')
    def test_crash_before_markup_response_is_reconciled(self):
        self.call(1); self.send()
        job=self.confirm.next_markup(); self.confirm.begin_markup(job)
        self.call(2)
        new=ConfirmationStore(self.path).next_markup()
        self.assertEqual(new['desired'],'empty')
    def test_recipient_deletion_cascades_confirmation_tokens(self):
        event=self.call(1); self.send(); row=self.message(event)
        key=self.sql('SELECT management_key FROM notification_recipients')[0]['management_key']
        with self.operator.transaction() as c: self.assertTrue(self.operator._remove(c,key))
        self.assertEqual(self.sql('SELECT * FROM confirmation_messages'),[])
        self.assertEqual(self.click(row),'call_forbidden')
        self.assertEqual(self.db.count_events(),1)
    def test_callback_replay_is_not_applied_twice(self):
        event=self.call(1); self.send(); row=self.message(event)
        self.assertEqual(self.click(row),'call_confirmed')
        self.update_id-=1
        self.assertEqual(self.click(row),'replayed')
    def test_legacy_call_remains_deliverable_without_keyboard(self):
        event='button01-test-00000001'
        payload=dict(schema_version=1,event_id=event,device_id='button01',event_type='care_call',sequence=1,uptime_ms=1)
        self.db.save_call(validate_call_message(TOPIC,json.dumps(payload).encode()))
        self.send(); self.send()
        self.assertEqual(len(self.client.sent),1); self.assertEqual(self.client.edits,[])
    def test_existing_guardian_help_route(self):
        result=self.operator.apply_update({'update_id':1,'message':{'from':{'id':111,'is_bot':False},
            'chat':{'id':111,'type':'private'},'date':100,'text':'/help'}},now=100)
        self.assertEqual(result,'ignored')
        self.assertEqual(len(self.sql('SELECT * FROM guardian_replies')),1)
    def test_edit_not_modified_is_idempotent_success(self):
        self.call(1); self.send()
        self.client.failure=TelegramError('message_not_modified')
        self.send(); self.assertIsNone(self.confirm.next_markup())
        api=TelegramClient('123:offline_fake_token')
        with self.assertRaises(TelegramError) as caught:
            api._raise_api_error(400,{'description':'Bad Request: message is not modified'},'editMessageReplyMarkup')
        self.assertEqual(caught.exception.code,'message_not_modified')

if __name__=='__main__': unittest.main()
