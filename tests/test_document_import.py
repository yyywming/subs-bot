"""Node files become manageable personal subscriptions, with no real Telegram calls."""
from __future__ import annotations
import asyncio
import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

os.environ['BOT_TOKEN']='0:TEST'
os.environ['ALLOWED_USER_IDS']='1,2'
os.environ['ADMIN_USER_IDS']='1'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import bot as B
from db import Store, nodes_of

URI='vless://11111111-1111-1111-1111-111111111111@example.invalid:443#中文节点'
YAML='"proxies":\n - {name: yaml-node, type: trojan, server: example.invalid, port: 443, password: secret, sni: tls.invalid}\n'.replace('\\n','\n')

class Message:
    def __init__(self,filename,data):
        self.document=NS(file_name=filename,file_size=len(data),file_id='fixture')
        self.replies=[]
        self.markups=[]
    async def reply_text(self,text,**kwargs):
        self.replies.append(text);return self
    async def reply_html(self,text,**kwargs):
        self.replies.append(text);self.markups.append(kwargs.get('reply_markup'));return self
    async def edit_text(self,text,**kwargs):
        self.replies.append(text)

class UploadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.original=B.store
        B.store=Store(str(Path(self.temp.name)/'test.db'))
    async def asyncTearDown(self):
        await B.store.close();B.store=self.original;self.temp.cleanup()
    async def upload(self,name,raw,uid=1,cid=-100123):
        message=Message(name,raw)
        file=NS(download_as_bytearray=AsyncMock(return_value=bytearray(raw)))
        ctx=NS(bot=NS(get_file=AsyncMock(return_value=file)),user_data={},args=[])
        update=NS(effective_user=NS(id=uid),effective_chat=NS(id=cid,type='supergroup' if cid<0 else 'private'),effective_message=message,callback_query=None)
        await B.handle_document(update,ctx)
        return update,ctx
    async def test_txt_encodings_and_base64_become_subscriptions(self):
        variants={'utf8':URI.encode('utf-8-sig'),'utf16':URI.encode('utf-16'),'gb':URI.encode('gb18030'),'base64':base64.b64encode(URI.encode())}
        for name,raw in variants.items():
            with self.subTest(name=name):
                update,ctx=await self.upload(name+'.TXT',raw)
                rows=await B.store.list_subs(1)
                row=next(r for r in rows if r['name']==name)
                self.assertEqual([(n['name'],n['server']) for n in nodes_of(row)],[('中文节点','example.invalid')])
                self.assertTrue(row['url'].startswith('uploaded://'))
        self.assertEqual(await B.store.list_subs(-100123),[])
        self.assertEqual(await B.store.list_temp(1),[])
        await B.cmd_list(update,ctx)
        labels=[b.text for line in update.effective_message.markups[-1].inline_keyboard for b in line]
        self.assertTrue(any('base64' in label for label in labels))
    async def test_yaml_fields_and_same_name_replacement(self):
        await self.upload('config.yaml',YAML.encode())
        row=(await B.store.list_subs(1))[0]
        self.assertEqual(nodes_of(row)[0]['password'],'secret')
        self.assertEqual(nodes_of(row)[0]['sni'],'tls.invalid')
        await B.store.update_sub(1,row['id'],last_error='stale')
        await self.upload('config.yaml',YAML.replace('yaml-node','updated-node').encode(),cid=1)
        rows=await B.store.list_subs(1)
        self.assertEqual([(r['id'],r['token']) for r in rows],[(row['id'],row['token'])])
        self.assertEqual(nodes_of(rows[0])[0]['name'],'updated-node')
        self.assertIsNone(rows[0]['last_error'])
    async def test_different_users_same_filename_remain_independent(self):
        await self.upload('same.txt',URI.encode())
        await self.upload('same.txt',URI.replace('中文节点','第二人').encode(),uid=2)
        self.assertEqual(nodes_of((await B.store.list_subs(1))[0])[0]['name'],'中文节点')
        self.assertEqual(nodes_of((await B.store.list_subs(2))[0])[0]['name'],'第二人')
    async def test_static_refresh_does_not_mark_valid_file_failed(self):
        await self.upload('static.txt',URI.encode())
        row=(await B.store.list_subs(1))[0]
        await B.store.update_sub(1,row['id'],last_error='old unsupported URL')
        result=await B.refresh_sub(1,row,rename=True,meta_only=True)
        self.assertIsNone(result['last_error'])
        self.assertEqual(result['node_count'],1)
        self.assertEqual(nodes_of(await B.store.get_sub(1,row['id']))[0]['name'],'中文节点')
    async def test_node_limit_applies_to_complete_file_import(self):
        text='\n'.join(URI.replace('中文节点',name) for name in ('one','two','three'))
        with patch.object(B,'MAX_IMPORTED_NODES',2):
            await self.upload('limited.txt',text.encode())
        row=(await B.store.list_subs(1))[0]
        self.assertEqual([n['name'] for n in nodes_of(row)],['one','two'])
        self.assertEqual(row['node_count'],2)
    async def test_invalid_yaml_preserves_existing_subscription(self):
        await self.upload('safe.yaml',YAML.encode())
        before=await B.store.list_subs(1)
        await self.upload('safe.yaml',b'proxies: [unterminated')
        self.assertEqual(await B.store.list_subs(1),before)
    async def test_mixed_txt_preserves_url_subscription_import(self):
        async def fetched(url):
            return [{'name':'remote-node','type':'trojan','server':'remote.invalid','port':443,'password':'p'}],{},None
        with patch.object(B,'fetch_subscription',fetched):
            await self.upload('mixed.txt',(URI+'\nhttps://example.invalid/sub.txt').encode())
        rows=await B.store.list_subs(1)
        self.assertEqual({r['url'] for r in rows if not r['url'].startswith('uploaded://')},{'https://example.invalid/sub.txt'})
        self.assertEqual({n['name'] for r in rows for n in nodes_of(r)},{'中文节点','remote-node'})

if __name__=='__main__':unittest.main()
