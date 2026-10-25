"""V2 integration: real graph/provider, deterministic HTTP responses, no paid API."""
import copy
import json
from dataclasses import replace
import httpx
import pytest

from math_agent.provider import DeepSeek, ModelError


def reply_patch(body):
    # Chat and Responses preserve the same user content payload.
    messages = body.get('messages', body.get('input', []))
    contexts = []
    for message in messages:
        for part in message.get('content', []) if isinstance(message.get('content'), list) else []:
            if part.get('type') in {'text','input_text'}:
                try: contexts.append(json.loads(part['text']))
                except (ValueError,KeyError): pass
    context = next(item['context'] for item in contexts if 'context' in item)
    fields = context['field_correction']
    assert 'rejected_output' not in context
    return {'base_sha256':fields['base_sha256'], 'replacements':{
        item['path']:{name:'real' for name in item['symbols_to_preserve']} for item in fields['fields']}}


@pytest.mark.parametrize('profile,native', [('serial',False), ('parallel_reuse',False), ('serial',True)])
def test_correction_then_surplus_closer_still_reaches_independent_review(
        make_runner,source_v2,draft_v2,review_v2,profile,native):
    op=draft_v2['questions']['Q1']['computations'][0]
    op.update(op='evaluate',expression='x/2',variables=['x'],substitutions={'x':2},expected='1')
    original=copy.deepcopy(draft_v2)
    runner=make_runner([source_v2,draft_v2,reply_patch,review_v2],profile=profile,
                       solve_responses=native,review_responses=native)
    # Inject the observed single extra root closer into the third HTTP reply.
    # The fixture transport has no external network access.
    inner=runner.provider.client._transport
    def wrap(request):
        response=inner.handle_request(request)
        response.read(); data=response.json()
        if len(runner.sent)==3:
            if request.url.path.endswith('/responses'):
                data['output'][0]['content'][0]['text']+='}'
            else:
                data['choices'][0]['message']['content']+='}'
        return httpx.Response(response.status_code,json=data)
    runner.provider.client.close()
    runner.provider.client=httpx.Client(transport=httpx.MockTransport(wrap))
    runner.run()
    report=json.loads((runner.directory/'report.json').read_text())
    assert report['status']=='completed',report.get('error')
    assert report['model_calls_this_job']==4
    assert 'tools' not in runner.sent[2]
    for key in ('thinking','reasoning_effort','reasoning'):
        assert runner.sent[2].get(key)==runner.sent[1].get(key)
    assert runner.sent[2].get('max_tokens',runner.sent[2].get('max_output_tokens'))==runner.sent[1].get('max_tokens',runner.sent[1].get('max_output_tokens'))
    assert any(e['kind']=='json_surplus_root_closure' for e in runner.records)
    assert any(e['kind']=='field_correction' for e in runner.records)
    assert any(e['kind']=='model' and e['stage']=='review' for e in runner.records)
    assert json.loads((runner.directory/'model-02-v2-solve.json').read_text())==original
    merged=json.loads((runner.directory/'model-03-v2-solve-merged.json').read_text())
    expected=copy.deepcopy(original);expected['questions']['Q1']['computations'][0]['variables']={'x':'real'}
    assert merged==expected


def test_format_fix_does_not_hide_a_negative_mathematical_check(make_runner,source_v2,draft_v2,review_v2):
    draft_v2['questions']['Q1']['computations'][0].update(
        op='evaluate',expression='x/2',variables=['x'],substitutions={'x':2},expected='999')
    runner=make_runner([source_v2,draft_v2,reply_patch,review_v2],max_repairs=0)
    runner.run()
    report=json.loads((runner.directory/'report.json').read_text())
    assert report['status']=='failed'
    assert len(runner.sent)==4
    assert not runner.cache.data
    receipts=json.loads((runner.directory/'v2-evidence.json').read_text())['receipts']
    assert receipts['half_length']['verdict']=='fail'


@pytest.mark.parametrize('native',[False,True])
def test_truncated_reply_never_uses_surplus_recovery(settings,native):
    requests=[];events=[]
    def handler(request):
        requests.append(request)
        if native:
            return httpx.Response(200,json={'status':'incomplete','output':[{
                'type':'message','content':[{'type':'output_text','text':'{"result":1}}'}]}]})
        return httpx.Response(200,json={'choices':[{'finish_reason':'length',
            'message':{'role':'assistant','content':'{"result":1}}'}}]})
    client=DeepSeek(replace(settings,compact_tools=True,solve_responses=native),
                    lambda k,v:events.append((k,v)),httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelError):
            client.generate('solve',[],**({'output_schema':{'type':'object'}} if native else {}))
        assert len(requests)==1 and not any(k=='json_surplus_root_closure' for k,_ in events)
    finally:client.close()


@pytest.mark.parametrize('content', ['{"x":1,"x":2}}', '{"x":1}{}', '{"x":NaN}}', '{"x":"broken}}'])
def test_untrusted_content_still_fails_without_retry(settings,content):
    requests=[];events=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop',
            'message':{'role':'assistant','content':content}}]})
    client=DeepSeek(settings,lambda k,v:events.append((k,v)),httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelError):client.generate('solve',[])
        assert len(requests)==1 and not any(k=='json_surplus_root_closure' for k,_ in events)
    finally:client.close()
