"""Account costs without changing selected text, executing accessors, or leaking metadata."""
from types import SimpleNamespace

import pytest
from openai.types.completion_usage import CompletionUsage

from services.completion_accounting import accounting_metrics
from services.managed_completion import select_managed_completion
from tests.test_streaming_observation import assemble, frame


@pytest.mark.parametrize('shape', ['dict', 'namespace', 'sdk'])
async def test_usage_survives_stream_and_omits_private_metadata(shape):
    usage={'prompt_tokens':20,'completion_tokens':5,'total_tokens':25,
        'prompt_tokens_details':{'cached_tokens':12,'cache_write_tokens':3,'private':'PRIVATE_USAGE'},
        'cost':0.000001239,'private':'PRIVATE_USAGE','cost_details':{'private':'PRIVATE_USAGE'}}
    if shape=='namespace':
        usage=SimpleNamespace(**usage)
    elif shape=='sdk':
        usage=CompletionUsage.model_validate(usage)
    result,partials=await assemble([frame('Готово'),frame(finish='stop',usage=usage)])
    assert select_managed_completion(result,str.strip).answer=='Готово'
    assert partials==['Готово']
    assert accounting_metrics(result.usage)==dict(prompt_tokens=20,completion_tokens=5,total_tokens=25,
        cached_tokens=12,cache_write_tokens=3,cost_nano_credits=1239)
    assert 'PRIVATE_USAGE' not in repr(result)


@pytest.mark.parametrize('bad', [True,-1,10**10,float('nan'),float('inf'),-float('inf'),'PRIVATE_USAGE',None])
async def test_bad_cost_and_cache_counts_never_change_text(bad):
    result,partials=await assemble([frame('Готово'),frame(finish='stop',usage={
        'completion_tokens':5,'cost':bad,
        'prompt_tokens_details':{'cached_tokens':bad,'cache_write_tokens':bad}})])
    assert select_managed_completion(result,str.strip).answer=='Готово' and partials==['Готово']
    assert accounting_metrics(result.usage)=={'completion_tokens':5}


def test_absent_cost_is_distinct_from_known_zero_and_rounding_is_explicit():
    assert accounting_metrics({})=={}
    assert accounting_metrics({'cost':0})=={'cost_nano_credits':0}
    assert accounting_metrics({'cost':0.0000000005})=={'cost_nano_credits':1}
    assert accounting_metrics({'cost':10**9})=={'cost_nano_credits':10**18}


def test_hostile_values_and_sdk_extra_metadata_do_not_execute_accessors():
    class Hostile:
        def __getattribute__(self,name):
            raise AssertionError('untrusted accessor')
        def __str__(self):
            raise AssertionError('untrusted conversion')
    assert accounting_metrics(Hostile())=={}
    assert accounting_metrics({'cost':Hostile(),'prompt_tokens_details':Hostile()})=={}
    usage=CompletionUsage.model_construct(prompt_tokens=1,completion_tokens=2,total_tokens=3)
    object.__delattr__(usage,'__pydantic_extra__')
    assert accounting_metrics(usage)==dict(prompt_tokens=1,completion_tokens=2,total_tokens=3)
