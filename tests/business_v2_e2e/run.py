"""Actual local Conversation → AI → typed ChotaCRM provider acceptance runner.

Launch from candidate AI root. Only synthetic disposable PostgreSQL containers,
model responses and /me identity are fixtures; service HTTP and business are real.
"""
import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import httpx
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
ROOTS = {'ai': ROOT,
         'cs': Path(os.environ.get('BUSINESS_E2E_CONVERSATION_ROOT', ROOT.parent / 'conversation')).resolve(),
         'crm': ROOT.parent / 'crm'}
AI_IMAGE = os.environ.get('BUSINESS_E2E_AI_IMAGE', 'business-v2-current-ai-runtime:local')
IMAGES = {'ai': AI_IMAGE, 'model': AI_IMAGE,
          'cs': os.environ.get('BUSINESS_E2E_CONVERSATION_IMAGE', 'conversation-service:goal4a-author'),
          'crm': os.environ.get('BUSINESS_E2E_CRM_IMAGE', 'business-v2-current-crm-runtime:latest')}
PORTS = {'ai':8001,'cs':8002,'crm':8003,'model':8004}



def private(path, value):
    path.write_text(json.dumps(value, default=str, ensure_ascii=False))
    path.chmod(0o600)


def clean_env():
    return {'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED':'1', 'PYTHONPATH':'/work/ai'}


def tool(name, arguments=None):
    return {'tool':name, 'arguments':arguments or {}}


def ref(name, *path):
    return {'$tool': name, 'path': list(path)}


class Harness:
    def __init__(self, state, urls, client, anchor, internal_urls):
        self.state, self.urls = state, urls
        self.docker, self.anchor, self.internal_urls = client, anchor, internal_urls
        self.ids = {k:str(uuid4()) for k in ['owner','agent','integration']}
        self.tokens = {k:secrets.token_urlsafe(36) for k in ['cs','ai','effect']}
        self.volumes = {str(root): {'bind':'/work/'+name,'mode':'ro'} for name,root in ROOTS.items()}
        self.volumes[str(state)] = {'bind':'/evidence','mode':'rw'}
        self.processes, self.logs = {}, []
        self.client = httpx.AsyncClient(timeout=50, trust_env=False)
        self.owner_headers = {'X-User-Id': self.ids['owner'], 'Cookie':'access_token='+self.ids['owner'], 'Origin':'https://chota.by'}
        self.grant = {'owner_id': 'synthetic-owner', 'integration_id': self.ids['integration'], 'channel':'TELEGRAM', 'provider_namespace':'e2e.synthetic'}
        self.results = []

    def url(self, name):
        return 'http://127.0.0.1:' + self.anchor.attrs['NetworkSettings']['Ports'][str(PORTS[name])+'/tcp'][0]['HostPort']

    def internal_url(self, name):
        return 'http://127.0.0.1:' + str(PORTS[name])

    async def setup(self):
        key = rsa.generate_private_key(public_exponent=65537,key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()
        pub = key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        allowlist = [('127.0.0.1',port) for port in [5432,5433,*PORTS.values()]]
        config = dict(self.ids, roots={k:'/work/'+k for k in ROOTS}, state='/evidence', ports=PORTS, allowlist=allowlist)
        self.config = config
        private(self.state/'config.json',config)
        ai_url = make_url(self.internal_urls['ai'])
        ai_env = dict(POSTGRES_HOST=ai_url.host,POSTGRES_PORT=str(ai_url.port),POSTGRES_DB=ai_url.database,
            POSTGRES_USER=ai_url.username,POSTGRES_PASSWORD=ai_url.password,POSTGRES_ECHO='false',
            ENCRYPTION_KEY=Fernet.generate_key().decode(), LLM_BASE_URL=self.internal_url('model')+'/v1',
            KNOWLEDGE_BASE_ENABLED='false',PRIVACY_FILTER_ENABLED='false',SUBSCRIPTION_CHECK_ENABLED='false',
            BUSINESS_CAPABILITIES_ENABLED='true',BUSINESS_CRM_URL=self.internal_url('crm'),
            BUSINESS_CRM_PRIVATE_KEY=pem,BUSINESS_CRM_KEY_ID='synthetic', BUSINESS_CONFIG_ORIGINS='["https://chota.by"]',
            BUSINESS_CONVERSATION_URL=self.internal_url('cs'),
            BUSINESS_CONVERSATION_TOKEN=self.tokens['effect'],
            MANAGED_AI_AUTH_PRINCIPALS=json.dumps([{'principal_id':'synthetic-cs','token':self.tokens['ai'],
                'caller':'conversation-service','execution_namespace':'main-chota.conversation.v1',
                'grants':[dict(owner_id=self.grant['owner_id'],integration_id=self.ids['integration'],channel='TELEGRAM',
                    agent_id=self.ids['agent'],agent_owner_id=self.ids['owner'])]}]))
        effect_grant = dict(self.grant, provider_namespace='main-chota.conversation.v1')
        effect_principal = {'principal_id':'synthetic-business-effect','role':'BUSINESS_EFFECT',
            'token':self.tokens['effect'],'grants':[effect_grant]}
        cs_env = dict(DATABASE_URL=self.internal_urls['cs'],LOG_LEVEL='ERROR',
            CONVERSATION_AUTH_PRINCIPALS=json.dumps([
                {'principal_id':'synthetic-worker','token':self.tokens['cs'],'grants':[self.grant]},
                effect_principal]),
            MANAGED_AI_BASE_URL=self.internal_url('ai'),MANAGED_AI_BINDINGS=json.dumps([{
                'owner_id':self.grant['owner_id'],'integration_id':self.ids['integration'],'channel':'TELEGRAM',
                'agent_id':self.ids['agent'],'service_token':self.tokens['ai']}]))
        crm_env = dict(DATABASE_URL=self.internal_urls['crm'],USER_SERVICE_URL=self.internal_url('model'),ALLOWED_ORIGINS='["https://chota.by"]',
                       CRM_AGENT_PUBLIC_KEYS_JSON=json.dumps({'synthetic':pub}))
        self.envs = {name:dict(clean_env(), BUSINESS_E2E_CONFIG='/evidence/config.json', **extra)
                     for name,extra in [('ai',ai_env),('cs',cs_env),('crm',crm_env),('model',{})]}
        for name in ['ai','cs','crm']:
            path=self.state/(name+'-migrate.log')
            container = await asyncio.to_thread(self.docker.containers.run, IMAGES[name],
                ['python','-m','alembic','upgrade','head'], working_dir='/work/'+name,
                environment=self.envs[name], network_mode='container:'+self.anchor.id,
                volumes=self.volumes, user='0', detach=True)
            try:
                outcome = await asyncio.to_thread(container.wait, timeout=90)
                path.write_bytes(await asyncio.to_thread(container.logs)); path.chmod(0o600)
                assert outcome['StatusCode']==0, f'{name} migration failed; private log {path}'
            finally:
                await asyncio.to_thread(container.remove,force=True)
        for name in ['model','crm','ai','cs']:
            await self.start(name)
        await self.seed()

    async def start(self,name):
        process=await asyncio.to_thread(self.docker.containers.run,IMAGES[name],
            ['python','/work/ai/tests/business_v2_e2e/bootstrap.py',name],
            working_dir='/work/'+('ai' if name=='model' else name), environment=self.envs[name],
            volumes=self.volumes, network_mode='container:'+self.anchor.id, user='0',detach=True)
        self.processes[name]=process
        deadline=asyncio.get_running_loop().time()+60
        while asyncio.get_running_loop().time()<deadline:
            await asyncio.to_thread(process.reload)
            if process.status!='running':
                path=self.state/(name+'.log'); path.write_bytes(await asyncio.to_thread(process.logs)); path.chmod(0o600)
                raise AssertionError(f'{name} child exited; private log {path}')
            try:
                if (await self.client.get(self.url(name)+'/health',timeout=.3)).status_code==200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(.06)
        raise AssertionError(name+' readiness timeout')

    async def request(self,service,method,path,body=None,headers=None,expected=200):
        response=await self.client.request(method,self.url(service)+path,json=body,headers=headers)
        assert response.status_code==expected, f'{service} {path}: {response.status_code} {response.text[:300]}'
        return response.json() if response.content else None

    async def seed(self):
        base='/api/crm/v1/workspaces'
        workspace=await self.request('crm','POST',base,{'name':'Synthetic Beauty','timezone':'Europe/Minsk'},self.owner_headers,201)
        self.workspace=workspace['id']; w=base+'/'+self.workspace
        self.offering=await self.request('crm','POST',w+'/catalog/offerings',{
            'type':'SERVICE','name':'Окрашивание','price':'80.00','currency':'BYN','duration_minutes':60},self.owner_headers,201)
        self.resource=await self.request('crm','POST',w+'/resources',{'name':'Анна'},self.owner_headers,201)
        self.config_path='/v1/agents/'+self.ids['agent']+'/business-tools'
        self.connection=await self.request('ai','PUT',self.config_path,{'workspace_id':self.workspace},self.owner_headers)
        print('SETUP actual owner connection and CRM grant ready',flush=True)

    async def sql(self,service,query,*args):
        db=await asyncpg.connect(self.urls[service].replace('postgresql+asyncpg','postgresql'))
        try:
            return [dict(v) for v in await db.fetch(query,*args)]
        finally:
            await db.close()

    async def count(self,table):
        assert table in {'bookings','customers','deals','inquiries','agent_receipts','notes','follow_up_tasks'}
        return (await self.sql('crm','SELECT count(*) AS n FROM '+table))[0]['n']

    def provider_attempt_count(self, operation_id):
        """Count real CRM HTTP execute dispatches for one opaque operation ID."""
        path = self.state/'crm-http.jsonl'
        if not path.exists():
            return 0
        return sum(
            1 for line in path.read_text().splitlines()
            if (event := json.loads(line)).get('method') == 'POST'
            and event.get('operation_id') == operation_id
            and event.get('path', '').startswith('/internal/agent/v1/execute/')
        )

    async def recover(self, turn_id):
        container = await asyncio.to_thread(self.docker.containers.run, IMAGES['ai'],
            ['python','/work/ai/tests/business_v2_e2e/bootstrap.py','recover',turn_id],
            working_dir='/work/ai', environment=self.envs['ai'], volumes=self.volumes,
            network_mode='container:'+self.anchor.id, user='0',detach=True)
        try:
            result=await asyncio.to_thread(container.wait,timeout=40)
            path=self.state/'recovery.log'; path.write_bytes(await asyncio.to_thread(container.logs)); path.chmod(0o600)
            assert result['StatusCode']==0, 'Recovery failed; private log '+str(path)
        finally:
            await asyncio.to_thread(container.remove,force=True)
        return json.loads((self.state/'recovery.jsonl').read_text().splitlines()[-1])

    async def turn(self,message,steps,thread=None):
        await self.request('model','POST','/script',steps)
        headers={'X-Service-Token':self.tokens['cs']}; prefix='/internal/v1'
        accepted=await self.request('cs','POST',prefix+'/inbound-events',dict(self.grant,
            provider_thread_id=thread or str(uuid4()),provider_event_id=str(uuid4()),text=message),headers,201)
        conversation=accepted['conversation']
        created=await self.request('cs','POST',prefix+'/conversations/'+conversation['id']+'/turns',{
            'selection_key':str(uuid4()),'input_first_sequence':conversation['last_message_sequence'],
            'input_last_sequence':conversation['last_message_sequence'],'expected_input_revision':conversation['input_revision'],
            'expected_authority_revision':conversation['authority_revision']},headers,201)
        claim=await self.request('cs','POST',prefix+'/turns/'+created['turn']['id']+'/claim',{'worker_ref':'e2e','lease_seconds':300},headers)
        result=await self.request('cs','POST',prefix+'/turns/'+claim['turn']['id']+'/execute',{
            'claim_epoch':claim['turn']['claim_epoch'],'claim_token':claim['claim_token']},headers)
        assert len(result['turn']['operation_refs']) <= 32
        assert all(set(v)=={'origin','operation_id','outcome'} and v['origin']=='AI_AGENT_SERVICE'
                   for v in result['turn']['operation_refs'])
        private(self.state/('turn-'+claim['turn']['id']+'.json'),result)
        return result['turn']

    def proposal_steps(self,start,phone,name='Влад'):
        return [tool('catalog_search'),tool('resource_search'),
            tool('booking_check_availability',{'offering_ref':ref('catalog_search','data','items',0,'ref'),
                'resource_ref':ref('resource_search','data','items',0,'ref'),'start_at':start}),
            tool('booking_create',{'customer':{'display_name':name,'identity':{'kind':'PHONE','value':phone}},
                'offering_ref':ref('catalog_search','data','items',0,'ref'),'resource_ref':ref('resource_search','data','items',0,'ref'),
                'start_at':start,'inquiry_summary':'Хочу окрашивание'})]

    async def confirm(self,thread,message='+',intent='CONFIRM',after=None):
        steps=[tool('business_decide_proposal',{'proposal_id':{'$pending':True},'intent':intent})]
        steps += after or [{'answer':'Запись подтверждена.' if intent=='CONFIRM' else 'Хорошо.'}]
        return await self.turn(message,steps,thread)

    def check(self,label,condition):
        assert condition,label
        self.results.append({'case':label,'status':'PASS'})
        print('PASS '+label,flush=True)

    def last_tool(self, name):
        for raw in reversed((self.state/'model-requests.jsonl').read_text().splitlines()):
            value=json.loads(raw); calls={}
            for message in value['messages']:
                for call in message.get('tool_calls',[]) or []:
                    calls[call['id']]=call['function']['name']
            for message in reversed(value['messages']):
                if message['role']=='tool' and calls.get(message['tool_call_id'])==name:
                    return json.loads(message['content'])
        raise AssertionError('No actual model-visible result for '+name)

    def tool_results(self, name):
        requests = (self.state/'model-requests.jsonl').read_text().splitlines()
        if not requests:
            raise AssertionError('No scripted model request was recorded')
        value = json.loads(requests[-1])
        calls = {}
        for message in value['messages']:
            for call in message.get('tool_calls', []) or []:
                calls[call['id']] = call['function']['name']
        return [json.loads(message['content']) for message in value['messages']
                if message['role'] == 'tool' and calls.get(message['tool_call_id']) == name]

    def last_provider_result(self, capability):
        for line in reversed((self.state/'provider-results.jsonl').read_text().splitlines()):
            value = json.loads(line)
            if value.get('capability') == capability and value.get('ref'):
                return value
        raise AssertionError('No actual provider result for '+capability)

    async def cases(self):
        # A: factual answer must be derived from the actual typed tool payload.
        catalog=await self.turn('Какие у вас услуги?',[tool('catalog_search'),
            tool('catalog_get',{'offering_ref':ref('catalog_search','data','items',0,'ref')}),
            {'answer_from':ref('catalog_get','data','name')}])
        self.check('A_catalog',catalog.get('draft_text')=='Окрашивание')
        for i,word in enumerate(['+','ага','ок','👍','давайте']):
            thread=str(uuid4()); start=(datetime.now(timezone.utc)+timedelta(days=i+3)).replace(hour=15,minute=0,second=0,microsecond=0).isoformat()
            before=await self.count('bookings')
            proposed=await self.turn('Хочу записаться на окрашивание',self.proposal_steps(start,'+3752998700'+str(i).zfill(2)),thread)
            self.check('B_proposal_'+str(i),proposed.get('draft_text') is not None and await self.count('bookings')==before)
            accepted=await self.confirm(thread,word)
            self.check('C_confirm_'+word,await self.count('bookings')==before+1
                and accepted.get('draft_text','').startswith('Запись была подтверждена:')
                and len(accepted['operation_refs'])==1 and accepted['operation_refs'][0]['outcome']=='SUCCEEDED')
            if i==0:
                first_booking=(thread,start,self.last_provider_result('booking.create'))
        # L: a case-only existing-customer match gets an explicit reference
        # instruction; the model must issue a separate booking call with it.
        reuse_thread = str(uuid4())
        reuse_start = (datetime.now(timezone.utc)+timedelta(days=20)).replace(
            hour=16, minute=0, second=0, microsecond=0).isoformat()
        reuse_phone = '+375299870000'
        bookings_before, customers_before = await self.count('bookings'), await self.count('customers')
        reuse_steps = self.proposal_steps(reuse_start, reuse_phone, name='вЛаД')
        reuse_steps.append(tool('booking_create', {
            'customer_ref': ref('booking_create', 'customer_ref'),
            'offering_ref': ref('catalog_search', 'data', 'items', 0, 'ref'),
            'resource_ref': ref('resource_search', 'data', 'items', 0, 'ref'),
            'start_at': reuse_start, 'inquiry_summary': 'Хочу окрашивание',
        }))
        reuse_proposal = await self.turn('Запишите меня ещё раз на окрашивание', reuse_steps, reuse_thread)
        guidance = next((value for value in self.tool_results('booking_create')
                         if value.get('status') == 'CUSTOMER_REUSE_REQUIRED'), None)
        self.check('L_existing_customer_explicit_ref_no_preconsent_effect',
            guidance is not None and bool(guidance.get('customer_ref'))
            and 'customer' not in guidance and 'вЛаД' not in json.dumps(guidance)
            and reuse_proposal.get('draft_text') is not None
            and await self.count('bookings') == bookings_before
            and await self.count('customers') == customers_before)
        reused = await self.confirm(reuse_thread)
        reuse_booking = self.last_provider_result('booking.create')
        self.check('L_existing_customer_one_confirmed_booking_no_duplicate',
            await self.count('bookings') == bookings_before + 1
            and await self.count('customers') == customers_before
            and reuse_booking['customer_ref'] == guidance['customer_ref']
            and len(reused['operation_refs']) == 1
            and reused['operation_refs'][0]['outcome'] == 'SUCCEEDED')
        # M: a supplied incompatible name is clarified without exposing the
        # candidate name/ref or reserving an operation. Corrected canonical
        # details can then follow the same explicit-reference path.
        conflict_thread = str(uuid4())
        conflict_start = (datetime.now(timezone.utc)+timedelta(days=21)).replace(
            hour=16, minute=0, second=0, microsecond=0).isoformat()
        bookings_before, customers_before = await self.count('bookings'), await self.count('customers')
        conflict = await self.turn('Хочу записаться',
            self.proposal_steps(conflict_start, reuse_phone, name='Different Synthetic'), conflict_thread)
        self.check('M_name_conflict_safe_clarification_no_effect',
            conflict.get('draft_text') == 'Этот контакт уже связан с другим именем. Уточните, пожалуйста, какое имя указать для записи.'
            and 'Different Synthetic' not in conflict.get('draft_text', '')
            and guidance['customer_ref'] not in conflict.get('draft_text', '')
            and await self.count('bookings') == bookings_before
            and await self.count('customers') == customers_before)
        correction_thread = conflict_thread
        corrected_start = (datetime.now(timezone.utc)+timedelta(days=22)).replace(
            hour=16, minute=0, second=0, microsecond=0).isoformat()
        corrected_steps = self.proposal_steps(corrected_start, reuse_phone, name='Влад')
        corrected_steps.append(tool('booking_create', {
            'customer_ref': ref('booking_create', 'customer_ref'),
            'offering_ref': ref('catalog_search', 'data', 'items', 0, 'ref'),
            'resource_ref': ref('resource_search', 'data', 'items', 0, 'ref'),
            'start_at': corrected_start, 'inquiry_summary': 'Хочу окрашивание',
        }))
        corrected = await self.turn('Меня зовут Влад, запишите на окрашивание', corrected_steps, correction_thread)
        corrected_guidance = next((value for value in self.tool_results('booking_create')
                                   if value.get('status') == 'CUSTOMER_REUSE_REQUIRED'), None)
        self.check('M_corrected_name_prepares_without_duplicate',
            corrected.get('draft_text') is not None and corrected_guidance is not None
            and await self.count('bookings') == bookings_before
            and await self.count('customers') == customers_before)
        corrected_result = await self.confirm(correction_thread)
        corrected_booking = self.last_provider_result('booking.create')
        self.check('M_corrected_name_confirms_existing_customer_once',
            await self.count('bookings') == bookings_before + 1
            and await self.count('customers') == customers_before
            and corrected_booking['customer_ref'] == corrected_guidance['customer_ref']
            and len(corrected_result['operation_refs']) == 1
            and corrected_result['operation_refs'][0]['outcome'] == 'SUCCEEDED')
        # D: change the pending appointment; never execute original 18:00 facts.
        thread=str(uuid4()); start=(datetime.now(timezone.utc)+timedelta(days=10)).replace(hour=15,minute=0,second=0,microsecond=0)
        before=await self.count('bookings')
        await self.turn('Запиши в 18',self.proposal_steps(start.isoformat(),'+375299870010'),thread)
        changed=await self.confirm(thread,'да, только лучше в 19','CHANGE',self.proposal_steps((start+timedelta(hours=1)).isoformat(),'+375299870010'))
        self.check('D_changed_no_old_effect',await self.count('bookings')==before and '19:00' in changed.get('draft_text',''))
        await self.confirm(thread)
        self.check('D_new_frozen_effect',await self.count('bookings')==before+1)
        for intent,message in [('REJECT','нет'),('UNRELATED','Как вас найти?')]:
            thread=str(uuid4()); before=await self.count('bookings')
            date=(start+timedelta(days=2 if intent=='REJECT' else 3)).isoformat()
            await self.turn('Запиши',self.proposal_steps(date,'+375299870020'),thread)
            result=await self.confirm(thread,message,intent)
            self.check('E_reject' if intent=='REJECT' else 'F_unrelated',await self.count('bookings')==before and result['operation_refs']==[])
        # G: lookup uses self-asserted exact identity; only the safe booking projection.
        w='/api/crm/v1/workspaces/'+self.workspace
        customer=(await self.sql('crm',"SELECT customer_id FROM customer_identities WHERE normalized_value=$1",'+375299870000'))[0]['customer_id']
        await self.request('crm','POST',w+'/customers/'+str(customer)+'/notes',{'body':'PRIVATE_STAFF_NOTE_SENTINEL'},self.owner_headers,201)
        returning=await self.turn('Я Влад, номер +375299870000. Во сколько запись?',[
            tool('customer_find',{'identity':{'kind':'PHONE','value':'+375299870000'}}),
            tool('customer_get_context',{'customer_ref':ref('customer_find','data','candidates',0,'ref'),'sections':['BOOKINGS','NOTES','DEALS']}),
            {'booking_answer':True}])
        self.check('G_self_asserted_booking',returning.get('draft_text') is not None and '18:00' in returning['draft_text'])
        projection=self.last_tool('customer_get_context')
        self.check('G_private_projection',projection['data']['sections']['NOTES']['status']=='NOT_AUTHORIZED'
            and projection['data']['sections']['DEALS']['status']=='NOT_AUTHORIZED'
            and 'PRIVATE_STAFF_NOTE_SENTINEL' not in json.dumps(projection) and 'audit' not in json.dumps(projection).lower())
        # Same legitimate model-visible booking handle supports get/search and
        # two separately confirmed customer intents (reschedule, then cancel).
        thread,booking_start,_=first_booking
        moved_start=(datetime.fromisoformat(booking_start.replace('Z','+00:00'))+timedelta(hours=1)).isoformat()
        await self.turn('Перенеси на час позже',[
            tool('customer_find',{'identity':{'kind':'PHONE','value':'+375299870000'}}),
            tool('booking_search',{'customer_ref':ref('customer_find','data','candidates',0,'ref'),
                'from':(datetime.now(timezone.utc)-timedelta(days=1)).isoformat(),'to':(datetime.now(timezone.utc)+timedelta(days=20)).isoformat()}),
            tool('booking_get',{'booking_ref':ref('booking_search','data','items',0,'ref')}),
            tool('booking_reschedule',{'booking_ref':ref('booking_get','data','ref'),
                'expected_revision':ref('booking_get','data','revision'),'resource_ref':ref('booking_get','data','resource_ref'),
                'start_at':moved_start})],thread)
        moved=await self.confirm(thread)
        self.check('BOOKING_reschedule',moved['operation_refs'][0]['outcome']=='SUCCEEDED')
        booking=self.last_provider_result('booking.reschedule')
        await self.turn('Отмени запись',[tool('booking_get',{'booking_ref':booking['ref']}),
            tool('booking_cancel',{'booking_ref':ref('booking_get','data','ref'),'expected_revision':ref('booking_get','data','revision')})],thread)
        cancelled=await self.confirm(thread)
        self.check('BOOKING_cancel',cancelled['operation_refs'][0]['outcome']=='SUCCEEDED')
        # H: two CRM records share asserted identity; no customer handle leaks.
        w='/api/crm/v1/workspaces/'+self.workspace
        for name in ['AmbiguousOne','AmbiguousTwo']:
            person=await self.request('crm','POST',w+'/customers',{'name':name},self.owner_headers,201)
            await self.request('crm','POST',w+'/customers/'+person['id']+'/identities',
                {'type':'PHONE','value':'+375299870088'},self.owner_headers,201)
        ambiguous=await self.turn('Мой номер +375299870088. Когда запись?',[
            tool('customer_find',{'identity':{'kind':'PHONE','value':'+375299870088'}}),
            {'answer':'Подскажите, пожалуйста, ваше имя?'}])
        last_model=json.loads((self.state/'model-requests.jsonl').read_text().splitlines()[-1])
        payload=json.loads(next(m['content'] for m in reversed(last_model['messages']) if m['role']=='tool'))
        self.check('H_ambiguous_identity',payload['status']=='AMBIGUOUS' and payload['data']['candidates']==[]
            and ambiguous.get('draft_text')=='Подскажите, пожалуйста, ваше имя?')
        # WRITE_SAFE customer creation also traverses actual managed ledger/provider.
        before=await self.count('customers')
        safe_thread=str(uuid4())
        safe=await self.turn('Меня зовут Мария, телефон +375299870099',[
            tool('customer_create',{'display_name':'Мария','identities':[{'kind':'PHONE','value':'+375299870099'}]}),{'answer':'Данные сохранены.'}])
        self.check('WRITE_SAFE_customer',await self.count('customers')==before+1 and safe['operation_refs'][0]['outcome']=='SUCCEEDED')
        for name,extra in [('customer_add_note',{'body':'Прошу учесть пожелание'}),
                           ('inquiry_capture',{'summary':'Интересует окрашивание'}),
                           ('followup_create',{'title':'Уточнить пожелания'})]:
            result=await self.turn('Запишите мою просьбу',[
                tool('customer_find',{'identity':{'kind':'PHONE','value':'+375299870099'}}),
                tool(name,dict(extra,customer_ref=ref('customer_find','data','candidates',0,'ref'))),{'answer':'Просьба сохранена.'}],safe_thread)
            self.check('WRITE_SAFE_'+name,result['operation_refs'][0]['outcome']=='SUCCEEDED')
        # I/J default-off tools must be denied even on model invocation.
        before=await self.count('deals')
        denied=await self.turn('Создать заказ',[tool('deal_create',{})])
        self.check('J_deal_default_off',await self.count('deals')==before and denied.get('draft_text') is None)
        enabled=[c['id'] for c in self.connection['capabilities'] if c['enabled']]
        await self.request('ai','PATCH',self.config_path,{'enabled_capabilities':[c for c in enabled if c!='customer.create']},self.owner_headers)
        customers=await self.count('customers')
        disabled=await self.turn('Сохрани мои данные',[tool('customer_create',{
            'display_name':'Denied','identities':[{'kind':'PHONE','value':'+375299870077'}]})])
        self.check('I_disabled_capability',await self.count('customers')==customers and disabled.get('draft_text') is None)
        await self.request('ai','PATCH',self.config_path,{'enabled_capabilities':enabled+['deal.create','deal.move']},self.owner_headers)
        thread=str(uuid4())
        await self.turn('Хочу заказать окрашивание',[
            tool('catalog_search'),tool('customer_find',{'identity':{'kind':'PHONE','value':'+375299870000'}}),
            tool('deal_create',{'customer_ref':ref('customer_find','data','candidates',0,'ref'),
                'title':'Окрашивание','currency':'BYN','items':[{'offering_ref':ref('catalog_search','data','items',0,'ref'),'quantity':'1'}]})],thread)
        enabled_deal=await self.confirm(thread)
        self.check('J_enabled_deal',await self.count('deals')==before+1 and enabled_deal['operation_refs'][0]['outcome']=='SUCCEEDED')
        deal=self.last_provider_result('deal.create')
        await self.turn('Начинайте выполнять заказ',[tool('deal_get',{'deal_ref':deal['ref']}),
            tool('deal_move',{'deal_ref':ref('deal_get','data','ref'),'expected_revision':ref('deal_get','data','revision'),'stage':'IN_PROGRESS'})],thread)
        moved_deal=await self.confirm(thread)
        self.check('J_enabled_deal_move',moved_deal['operation_refs'][0]['outcome']=='SUCCEEDED')
        # Supported bounded booking/order bundle uses one customer agreement.
        thread=str(uuid4()); bookings=await self.count('bookings'); deals=await self.count('deals')
        group_start=(start+timedelta(days=10)).isoformat()
        await self.turn('Запиши и оформи заказ',[
            tool('customer_find',{'identity':{'kind':'PHONE','value':'+375299870000'}}),tool('catalog_search'),tool('resource_search'),
            tool('business_propose',{'actions':[
                {'capability':'booking.create','arguments':{'customer_ref':ref('customer_find','data','candidates',0,'ref'),
                    'offering_ref':ref('catalog_search','data','items',0,'ref'),'resource_ref':ref('resource_search','data','items',0,'ref'),'start_at':group_start}},
                {'capability':'deal.create','arguments':{'customer_ref':ref('customer_find','data','candidates',0,'ref'),
                    'title':'Окрашивание вместе с записью','currency':'BYN','items':[{'offering_ref':ref('catalog_search','data','items',0,'ref'),'quantity':'1'}]}}
            ]})],thread)
        self.check('GROUP_no_effect_before_consent',await self.count('bookings')==bookings and await self.count('deals')==deals)
        grouped=await self.confirm(thread)
        self.check('GROUP_one_confirmation_two_effects',await self.count('bookings')==bookings+1 and await self.count('deals')==deals+1
            and len(grouped['operation_refs'])==2 and all(v['outcome']=='SUCCEEDED' for v in grouped['operation_refs']))
        group_booking=self.last_provider_result('booking.create')
        unsupported=await self.turn('Измени и отмени одновременно',[
            tool('booking_get',{'booking_ref':group_booking['ref']}),
            tool('business_propose',{'actions':[
                {'capability':'booking.reschedule','arguments':{'booking_ref':ref('booking_get','data','ref'),
                    'expected_revision':ref('booking_get','data','revision'),'resource_ref':ref('booking_get','data','resource_ref'),
                    'start_at':(datetime.fromisoformat(group_start)+timedelta(hours=1)).isoformat()}},
                {'capability':'booking.cancel','arguments':{'booking_ref':ref('booking_get','data','ref'),'expected_revision':ref('booking_get','data','revision')}}
            ]})],thread)
        self.check('GROUP_unsupported_denied',unsupported['operation_refs']==[] and unsupported.get('draft_text') is None
            and await self.count('bookings')==bookings+1 and await self.count('deals')==deals+1)
        # K: real CRM commits, then test transport wrapper discards its HTTP result.
        thread=str(uuid4()); before=await self.count('bookings')
        await self.turn('Запиши',self.proposal_steps((start+timedelta(days=5)).isoformat(),'+375299870030'),thread)
        (self.state/'lose-response').touch()
        lost=await self.confirm(thread)
        lost_operation_id=lost['operation_refs'][0]['operation_id']
        lost_attempts=self.provider_attempt_count(lost_operation_id)
        self.check('K_lost_response_unknown',await self.count('bookings')==before+1 and lost.get('draft_text') is None
            and lost['operation_refs'][0]['outcome']=='UNKNOWN' and lost_attempts==1)
        blocked=await self.turn('Попробуй ещё раз',[{'answer':'Готово'}],thread)
        self.check('K_no_blind_retry',await self.count('bookings')==before+1
            and len(blocked['operation_refs'])==1
            and blocked['operation_refs'][0]['operation_id']==lost_operation_id
            and blocked['operation_refs'][0]['outcome']=='SUCCEEDED'
            and self.provider_attempt_count(lost_operation_id)==lost_attempts)
        recovered=await self.recover(lost['id'])
        self.check('K_authoritative_receipt',await self.count('bookings')==before+1 and recovered[0]['outcome']=='SUCCEEDED'
            and recovered[0]['operation_id']==lost_operation_id
            and self.provider_attempt_count(lost_operation_id)==lost_attempts)
        # K2: authoritative no-effect outcome after facts change at provider
        # acceptance, following positive AI dispatch permission.
        thread=str(uuid4()); before=await self.count('bookings'); people=await self.count('customers')
        await self.turn('Запиши',self.proposal_steps((start+timedelta(days=6)).isoformat(),'+375299870031'),thread)
        private(self.state/'change-facts-before-execute.json',{'path':w+'/catalog/offerings/'+self.offering['id']})
        rejected=await self.confirm(thread)
        self.check('K2_definitive_no_effect',await self.count('bookings')==before and await self.count('customers')==people
            and rejected['operation_refs'][0]['outcome']=='FAILED' and rejected.get('draft_text')!='Запись подтверждена.')
        sessions=await self.sql('ai','SELECT count(*) AS n FROM conversation_sessions')
        self.check('NO_legacy_sessions',sessions[0]['n']==0 and not (self.state/'forbidden.jsonl').exists())

    async def close(self):
        await self.client.aclose()
        for name,process in self.processes.items():
            try:
                await asyncio.to_thread(process.stop,timeout=5)
                path=self.state/(name+'.log'); path.write_bytes(await asyncio.to_thread(process.logs)); path.chmod(0o600)
            finally:
                await asyncio.to_thread(process.remove,force=True)



async def main():
    import docker

    state=Path(tempfile.mkdtemp(prefix='business-v2-e2e-'))
    state.chmod(0o700)
    print('Private evidence: '+str(state),flush=True)
    client=docker.from_env()
    password=secrets.token_urlsafe(24)
    containers=[]; harness=None
    env={'POSTGRES_USER':'e2e','POSTGRES_PASSWORD':password}
    ports={str(p)+'/tcp':('127.0.0.1',0) for p in [5432,5433,*PORTS.values()]}
    try:
        anchor=await asyncio.to_thread(client.containers.run,'postgres:16-alpine',
            environment=dict(env,POSTGRES_DB='business_e2e_ai'),ports=ports,
            tmpfs={'/var/lib/postgresql/data':'rw'},detach=True)
        containers.append(anchor); await asyncio.to_thread(anchor.reload)
        crm_container=await asyncio.to_thread(client.containers.run,'postgres:18-alpine',
            ['postgres','-p','5433','-c','listen_addresses=*'],
            environment=dict(env,POSTGRES_DB='business_e2e_crm'),network_mode='container:'+anchor.id,
            tmpfs={'/var/lib/postgresql':'rw'},detach=True)
        containers.append(crm_container)
        hostports=anchor.attrs['NetworkSettings']['Ports']
        def url(port,name):
            return f'postgresql+asyncpg://e2e:{password}@127.0.0.1:{port}/{name}'
        urls={'ai':url(hostports['5432/tcp'][0]['HostPort'],'business_e2e_ai'),
              'cs':url(hostports['5432/tcp'][0]['HostPort'],'business_e2e_cs'),
              'crm':url(hostports['5433/tcp'][0]['HostPort'],'business_e2e_crm')}
        for key in ['ai','crm']:
            deadline=asyncio.get_running_loop().time()+40
            while True:
                try:
                    db=await asyncpg.connect(urls[key].replace('postgresql+asyncpg','postgresql'),timeout=1)
                    if key=='ai': await db.execute('CREATE DATABASE business_e2e_cs')
                    await db.close(); break
                except (OSError,asyncpg.PostgresError):
                    if asyncio.get_running_loop().time()>deadline: raise
                    await asyncio.sleep(.2)
        internal_urls={'ai':url(5432,'business_e2e_ai'),'cs':url(5432,'business_e2e_cs'),'crm':url(5433,'business_e2e_crm')}
        harness=Harness(state,urls,client,anchor,internal_urls)
        await harness.setup(); await harness.cases()
        private(state/'summary.json',{'status':'PASS','results':harness.results,'real_provider':True,'live_model':False,
            'images':IMAGES,'source_roots':{name:str(path) for name,path in ROOTS.items()}})
        print('REAL_CROSS_SERVICE_E2E_PASS',flush=True)
    finally:
        if harness: await harness.close()
        for container in reversed(containers):
            await asyncio.to_thread(container.remove,force=True,v=True)
        client.close()



if __name__=='__main__':
    asyncio.run(main())
