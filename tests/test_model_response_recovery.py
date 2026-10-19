"""Transient MODEL recovery is bounded, pre-speech and never replays a tool."""
from types import SimpleNamespace

import httpx
import pytest
from openai import APIError, APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

from models.agent import AgentTemplate
from services.llm_service import LLMService
from services.managed_tools import ManagedExecutionError
from services.model_retry import classify_model_failure, model_payment_required, model_retry_delay
from services.prompt_builder import ChatContext
from tests.fakes import ScriptedLLMClient, build_llm_response
from tests.test_chotacrm_adapter import crm_settings  # noqa: F401
from tests.test_pending_proposal_decision import run_core
from tests.test_preparation_schema_correction import local_runtime, preparation
from tests.test_text_booking_recovery import failure
from tests.test_managed_turns import model  # noqa: F401


@pytest.mark.parametrize('code,category,retryable', [
    (429,'RATE_LIMITED',True),(503,'SERVER_ERROR',True),
    ('server_error','SERVER_ERROR',True),('rate_limit_exceeded','RATE_LIMITED',True),
    (400,'CLIENT_ERROR',False),(401,'CLIENT_ERROR',False),(402,'CLIENT_ERROR',False),
    ('private-provider-sentinel','UNKNOWN',False),(None,'UNKNOWN',False)])
def test_stream_classification_never_retains_provider_content(code,category,retryable):
    """Accept only finite documented error codes and discard private details."""
    error=APIError('private-provider-sentinel',request=httpx.Request('POST','https://synthetic.invalid'),
                   body={'code':code,'message':'private-provider-sentinel'})
    value=classify_model_failure(error)
    assert value.category==category and value.http_status is None
    assert (model_retry_delay(error) is not None)==retryable
    assert 'private-provider-sentinel' not in repr(value)


@pytest.mark.parametrize('header,allowed', [('1.5',True),('0',True),('3',False),
    ('NaN',False),('Infinity',False),('private-provider-sentinel',False)])
def test_retry_after_never_exceeds_interactive_wait_budget(header,allowed):
    """Honor a short upstream minimum and refuse long or unproven waits."""
    error=failure(429)
    error.response.headers['Retry-After']=header
    delay=model_retry_delay(error)
    assert (delay is not None)==allowed
    if allowed:assert max(float(header),.5)<=delay<=2


def test_timeouts_are_not_retried_but_transport_setup_can_recover():
    """A timeout has already consumed its attempt budget."""
    request=httpx.Request('POST','https://synthetic.invalid')
    assert model_retry_delay(APITimeoutError(request=request)) is None
    setup=APIConnectionError(request=request)
    setup.__cause__=httpx.ConnectError('private-provider-sentinel')
    assert model_retry_delay(setup) is not None
    assert model_retry_delay(APIConnectionError(request=request)) is None


@pytest.mark.parametrize('cause', [httpx.ReadError, httpx.RemoteProtocolError, RuntimeError])
def test_non_setup_connection_wrappers_are_not_retried(cause):
    """A stream or cleanup failure cannot masquerade as a fresh connection failure."""
    error=APIConnectionError(request=httpx.Request('POST','https://synthetic.invalid'))
    error.__cause__=cause('private-provider-sentinel')
    assert classify_model_failure(error).category=='UNKNOWN'
    assert model_retry_delay(error) is None


@pytest.mark.parametrize('message', ['credits exceeded', 'insufficient rate allowance', 'request 402abcdef'])
def test_rate_limit_text_does_not_invent_a_payment_refusal(message):
    """Only structured provider codes can deactivate an agent for payment."""
    error=RateLimitError(message,response=httpx.Response(429,request=httpx.Request('POST','https://synthetic.invalid')),
                         body={'code':429})
    assert not model_payment_required(error)
    assert classify_model_failure(error).category=='RATE_LIMITED'
    assert model_retry_delay(error) is not None


@pytest.mark.parametrize('header,allowed', [(None,False),('0',True),('5',False)])
def test_in_flight_budget_is_temporary_and_requires_bounded_retry_after(header,allowed):
    """A documented concurrent-spend reservation limit never deactivates the key."""
    error=APIStatusError('private-provider-sentinel',response=httpx.Response(402,
        request=httpx.Request('POST','https://synthetic.invalid'),headers={} if header is None else {'Retry-After':header}),
        body={'error':{'code':402,'metadata':{'limit_source':'openrouter_in_flight_budget'}}})
    assert not model_payment_required(error)
    assert classify_model_failure(error).category=='RATE_LIMITED'
    assert (model_retry_delay(error) is not None)==allowed


@pytest.mark.asyncio
@pytest.mark.parametrize('stream_code',[429,'server_error',None])
async def test_managed_recovers_before_any_partial_without_replaying_a_read(
        db_session,crm_settings,monkeypatch,stream_code):
    """Retry only the failed completion after a successful guarded READ."""
    business,runtime,provider,_=await local_runtime(db_session,crm_settings,monkeypatch)
    model=ScriptedLLMClient()
    error=(failure(503) if stream_code is None else APIError('private-provider-sentinel',
        request=httpx.Request('POST','https://synthetic.invalid'),body={'code':stream_code}))
    model.chat.completions.queue(preparation('catalog_search',{},'read'),error,
                                 build_llm_response(content='Какое время вам удобно?'))
    before=runtime.deadline
    assert await run_core(db_session,runtime,model)=='Какое время вам удобно?'
    assert provider['posts']==0 and len(model.chat.completions.calls)==3
    assert model.chat.completions.calls[1]==model.chat.completions.calls[2]
    assert runtime.deadline==before
    assert await business.repository.selected_plan(business.request,business.grant) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('emitted',[False,True])
async def test_stream_retry_requires_no_callback_attempt_in_the_whole_turn(
        db_session,crm_settings,monkeypatch,emitted):
    """Never repeat a model request after any possibly published speech."""
    _,runtime,provider,_=await local_runtime(db_session,crm_settings,monkeypatch)
    calls=[];partials=[]
    error=APIError('private-provider-sentinel',request=httpx.Request('POST','https://synthetic.invalid'),body={'code':'server_error'})
    async def create(**kwargs):
        """Fail an SDK-style stream before or after a visible delta."""
        calls.append(kwargs)
        if len(calls)>1:return build_llm_response(content='Уточните удобное время.')
        async def chunks():
            """Yield at most one partial before the provider disconnects."""
            if emitted:
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='Проверяю запись.',tool_calls=None),finish_reason=None)])
            raise error
        return chunks()
    async def on_text(text):
        """Represent a callback that may already have published speech."""
        partials.append(text)
    model=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    async with db_session() as session:
        agent=await session.get(AgentTemplate,runtime.request.agent_id)
        try:
            operation=LLMService()._execute_core(session,agent=agent,llm_client=model,
                credentials_records=(),communication=[{'role':'user','content':'Какое время свободно?'}],
                chat_context=ChatContext(channel='voice',external_client_id='synthetic'),query='Какое время свободно?',
                runtime=runtime,selected_indices=runtime.request.selected_local_indices,
                simple_selection=runtime.request.simple_selection,partial_hook=on_text)
            if emitted:
                with pytest.raises(ManagedExecutionError,match='MODEL_ERROR'):await operation
                assert len(calls)==1 and partials==['Проверяю запись.']
            else:
                assert await operation=='Уточните удобное время.'
                assert len(calls)==2 and partials==[] and calls[0]==calls[1]
        finally:await runtime.stack.aclose()
    assert provider['posts']==0


@pytest.mark.integration
@pytest.mark.parametrize('emitted',[False,True])
async def test_voice_sse_endpoint_recovers_only_before_first_published_sentence(
        api_client,db_session,monkeypatch,model,emitted):
    """Exercise real VOICE projection and SSE finalization around a stream failure."""
    from core.managed_boundary import MANAGED_PATH
    from tests.test_managed_turns import setup, TOKEN
    from tests.test_managed_stream_sse import sse_events
    _,value=await setup(api_client,db_session,monkeypatch,channel='VOICE')
    value.update(contract_version=2,stream_partials=True,max_partial_chars=500)
    error=APIError('private-provider-sentinel',request=httpx.Request('POST','https://synthetic.invalid'),body={'code':'server_error'})
    async def broken():
        """Disconnect after zero or one complete visible sentence."""
        if emitted:
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                content='Проверяю запись. Подождите, пожалуйста.',tool_calls=None),finish_reason=None)])
        raise error
    model.chat.completions.queue(broken(),build_llm_response(content='Какое время удобно?'))
    response=await api_client.post(MANAGED_PATH,json=value,headers={'X-Service-Token':TOKEN})
    assert response.status_code==200
    events=sse_events(response.content)
    final=next(x for x in events if x.get('partial_final'))
    partials=[x for x in events if not x.get('partial_final')]
    assert 'private-provider-sentinel' not in response.text
    if emitted:
        assert len(model.chat.completions.calls)==1 and partials
        assert final['status'] in {'FAILED','UNKNOWN'} and final['error_code']=='MODEL_ERROR'
    else:
        assert len(model.chat.completions.calls)==2 and not partials
        assert final['status']=='DRAFT_READY' and final['draft_text']=='Какое время удобно?'
