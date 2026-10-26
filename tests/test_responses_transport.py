import pytest
from math_agent.responses_transport import body,message,input_items


def assert_strict_required(schema):
    if isinstance(schema,dict):
        if 'properties' in schema:
            assert schema.get('required')==list(schema['properties'])
        for value in schema.values():assert_strict_required(value)
    elif isinstance(schema,list):
        for value in schema:assert_strict_required(value)


def test_strict_schema_recursively_requires_every_property_without_mutating_input(settings):
    import copy
    schema={'type':'object','properties':{
        'items':{'type':'array','items':{'anyOf':[{'$ref':'#/$defs/Item'},{'type':'null'}]}},
        'mapping':{'type':'object','properties':{'Q1':{'$ref':'#/$defs/Item'}},'additionalProperties':False}},
        '$defs':{'Item':{'oneOf':[{'type':'object','properties':{'value':{'type':'string'},'optional':{'type':'integer','default':0}},
            'required':['value'],'additionalProperties':False}]}}}
    original=copy.deepcopy(schema)
    sent=body(settings,[],schema,strict=True)
    assert_strict_required(sent['text']['format']['schema'])
    assert sent['text']['format']['schema'] is not schema
    assert schema==original


@pytest.mark.parametrize('keyword',['default','const','examples','enum'])
def test_strict_schema_does_not_rewrite_instance_values(settings,keyword):
    instances=[{'properties':{'literal':1},'required':['instance']}]
    value=instances if keyword in {'examples','enum'} else instances[0]
    schema={'type':'object','properties':{'value':{'type':'object',keyword:value}}}
    sent=body(settings,[],schema,strict=True)['text']['format']['schema']
    assert sent['properties']['value'][keyword]==value
    assert sent['required']==['value']


def test_non_strict_schema_is_sent_unchanged(settings):
    schema={'type':'object','properties':{'optional':{'type':'string','default':'x'}}}
    assert body(settings,[],schema)['text']['format']['schema']==schema


def test_responses_roundtrip_keeps_thinking_tools_and_original_images(settings):
    initial=[{'role':'system','content':'json'}, {'role':'user','content':[{'type':'text','text':'inspect'},
        {'type':'image_url','image_url':{'url':'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAC0lEQVR4nGNgQAYAAA4AAamRc7EAAAAASUVORK5CYII='}}]}]
    result={'status':'completed','output':[{'type':'reasoning','content':[{'type':'reasoning_text','text':'private reasoning'}]},
        {'type':'function_call','call_id':'fc1','name':'probe','arguments':'{}'}]}
    assistant,finish=message(result);assert finish=='tool_calls'
    sent=body(settings,initial+[assistant,{'role':'tool','tool_call_id':'fc1','content':'28'}],{'type':'object'})
    assert sent['reasoning']['effort']=='high' and sent['max_output_tokens']==settings.max_tokens
    assert sent['text']['format']['type']=='json_schema'
    assert sent['input'][1]['content'][1]['detail']=='original'
    assert sent['input'][-3]['content'][0]['text']=='private reasoning'
    assert sent['input'][-2]['call_id']==sent['input'][-1]['call_id']=='fc1'
    assert 'previous_response_id' not in sent

@pytest.mark.parametrize('status',['incomplete','failed'])
def test_responses_partial_output_is_never_accepted(status):
    with pytest.raises(ValueError):message({'status':status,'output':[{'type':'message','content':[{'type':'output_text','text':'{}'}]}]})


def test_responses_duplicate_calls_rejected():
    call={'type':'function_call','call_id':'fc1','name':'probe','arguments':'{}'}
    with pytest.raises(ValueError):message({'status':'completed','output':[call,call]})


@pytest.mark.parametrize('stage',['review','math_repair','deferred_review'])
def test_provider_native_schema_tool_loop_is_stateless_and_keeps_evidence(settings,stage):
    import json,httpx
    from dataclasses import replace
    from math_agent.provider import DeepSeek
    requests=[];audit=[]
    schema={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}
    def handler(req):
        sent=json.loads(req.content);requests.append(sent)
        assert req.url.path=='/responses'
        assert sent['text']['format']['schema']==schema
        if len(requests)==1:
            assert sent['input'][0]['content'][1]['detail']=='original'
            return httpx.Response(200,json={'status':'completed','output':[
                {'type':'reasoning','content':[{'type':'reasoning_text','text':'private test reasoning'}]},
                {'type':'function_call','call_id':'a','name':'inspect','arguments':'{}'}]})
        assert sent['input'][-3]['content'][0]['text']=='private test reasoning'
        assert sent['input'][-2]['call_id']==sent['input'][-1]['call_id']=='a'
        assert json.loads(sent['input'][-1]['output'])=={'ok':False,'holds':False}
        return httpx.Response(200,json={'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'{"ok":false}'}]}]})
    c=DeepSeek(replace(settings,review_responses=True,compact_tools=True),lambda *x:audit.append(x),httpx.MockTransport(handler))
    try:
        result=c.generate(stage,[{'role':'user','content':[{'type':'text','text':'original'},
            {'type':'image_url','image_url':{'url':'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAC0lEQVR4nGNgQAYAAA4AAamRc7EAAAAASUVORK5CYII='}}]}],
            tools=[{'type':'function','function':{'name':'inspect','parameters':{'type':'object'}}}],
            dispatch=lambda *args:{'ok':False,'holds':False},output_schema=schema)
        assert result=={'ok':False} and c.calls==2
        assert 'private test reasoning' not in str(audit)
    finally:c.close()


@pytest.mark.parametrize('case',['incomplete','schema','http','network'])
def test_provider_native_failure_never_retries_or_switches_to_chat(settings,case):
    import httpx
    from dataclasses import replace
    from math_agent.provider import DeepSeek,ModelError
    calls=[]
    def handler(req):
        calls.append(req.url.path)
        if case=='network':raise httpx.ReadTimeout('test',request=req)
        if case=='http':return httpx.Response(429)
        return httpx.Response(200,json={'status':'incomplete' if case=='incomplete' else 'completed',
            'output':[{'type':'message','content':[{'type':'output_text','text':'{"ok":"wrong"}'}]}]})
    c=DeepSeek(replace(settings,review_responses=True,compact_tools=True),lambda *x:None,httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelError):c.generate('review',[],output_schema={'type':'object','properties':{'ok':{'type':'boolean'}}})
        assert calls==['/responses']
    finally:c.close()


def test_cache_separates_native_review_protocol(settings):
    from dataclasses import replace
    from math_agent.cache import key_for
    req={'text':'question','images':[]}
    assert key_for(settings,req)!=key_for(replace(settings,review_responses=True,compact_tools=True),req)


@pytest.mark.parametrize('status',['completed','incomplete'])
def test_native_failed_output_audit_retains_final_only_and_no_retry(settings,status):
    import json,httpx
    from dataclasses import replace
    from math_agent.provider import DeepSeek,ModelError
    seen=[];audit=[]
    def handler(req):
        seen.append(req)
        return httpx.Response(200,json={'status':status,'incomplete_details':{'reason':'max_output_tokens'},'output':[
            {'type':'reasoning','content':[{'type':'reasoning_text','text':'PRIVATE REASONING NEVER LOG'}]},
            {'type':'message','content':[{'type':'output_text','text':json.dumps({'bad':settings.api_key+' data:image/png;base64,AAAA'})}]}]})
    c=DeepSeek(replace(settings,review_responses=True,compact_tools=True),lambda k,v:audit.append((k,v)),httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelError):c.generate('review',[],output_schema={'type':'object','properties':{'ok':{'type':'boolean'}},'additionalProperties':False})
        e=next(v for k,v in audit if k=='model_output_error')
        assert 'bad' in e['content'] and len(seen)==1 and e['api_status']==status
        assert e['incomplete_reason']=='max_output_tokens'
        encoded=str(audit);assert 'PRIVATE REASONING NEVER LOG' not in encoded
        assert settings.api_key not in encoded and 'base64,AAAA' not in encoded
    finally:c.close()


def test_native_nonobject_response_never_crashes_diagnostic_or_retries(settings):
    import httpx
    from dataclasses import replace
    from math_agent.provider import DeepSeek,ModelError
    requests=[];audit=[]
    def handler(r):requests.append(r);return httpx.Response(200,json=[])
    c=DeepSeek(replace(settings,review_responses=True,compact_tools=True),lambda k,v:audit.append((k,v)),httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelError):c.generate('review',[],output_schema={'type':'object'})
        assert len(requests)==1 and any(k=='model_output_error' for k,_ in audit)
    finally:c.close()


@pytest.mark.parametrize('case',['http','incomplete','invalid_json','final_array','response_array'])
def test_complete_candidate_recovery_cannot_replay_transport_or_partial_failures(settings,case):
    import json,httpx
    from dataclasses import replace
    from math_agent.provider import DeepSeek,ModelError,CompletedSchemaError
    from math_agent.submission import receive
    from math_agent.parallel_budget import ParallelBudget
    calls=[];audit=[]
    def handler(request):
        calls.append(request)
        if case=='http':return httpx.Response(503)
        if case=='response_array':return httpx.Response(200,json=[])
        text='{' if case=='invalid_json' else '[]' if case=='final_array' else '{}'
        return httpx.Response(200,json={'status':'incomplete' if case=='incomplete' else 'completed',
            'output':[{'type':'message','content':[{'type':'output_text','text':text}]}],
            'usage':{'input_tokens':13,'output_tokens':19}})
    c=DeepSeek(replace(settings,solve_responses=True,compact_tools=True),lambda k,v:audit.append((k,v)),httpx.MockTransport(handler))
    c.shared_budget=ParallelBudget(12,timeout_seconds=240)
    try:
        with pytest.raises(ModelError) as error:receive(c,'solve',[],lambda k,v:audit.append((k,v)),output_schema={'type':'object'})
        assert not isinstance(error.value,CompletedSchemaError)
        assert len(calls)==1 and not any(k=='schema_candidate' for k,v in audit)
        snapshot=c.shared_budget.snapshot()['usage']
        if case not in {'http','response_array'}:assert snapshot['known_completion_tokens']==19
    finally:c.close()


def test_complete_schema_error_has_only_final_untrusted_object_and_does_not_retry(settings):
    import json,httpx
    from dataclasses import replace
    from math_agent.provider import DeepSeek,CompletedSchemaError
    calls=[];audit=[];raw={'ok':'wrong-type'}
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={'status':'completed','output':[
            {'type':'reasoning','content':[{'type':'reasoning_text','text':'PRIVATE REASONING'}]},
            {'type':'message','content':[{'type':'output_text','text':json.dumps(raw)}]}],
            'usage':{'input_tokens':13,'output_tokens':19}})
    c=DeepSeek(replace(settings,solve_responses=True,compact_tools=True),lambda k,v:audit.append((k,v)),httpx.MockTransport(handler))
    try:
        with pytest.raises(CompletedSchemaError) as raised:
            c.generate('solve',[],output_schema={'type':'object','properties':{'ok':{'type':'boolean'}}})
        assert raised.value.value==raw and raised.value.path==['ok']
        assert len(calls)==1 and 'PRIVATE REASONING' not in str(audit)
    finally:c.close()


def test_current_whole_branch_and_repair_schemas_pass_gateway_node_preflight(sample):
    import copy
    from math_agent.responses_transport import check_schema_nodes
    from math_agent.compact_output import output_schema,native_schema
    from math_agent.submission import patch_schema
    from math_agent.diagram_patch import DiagramPatch
    from math_agent.compact_review import output_schema as review_schema
    from test_native_constructions import augment
    for raw in [sample[0],augment(sample)[0]]:
        check_schema_nodes(output_schema(raw,require_basis=True))
        branch=copy.deepcopy(raw);branch['questions']=branch['questions'][-1:]
        check_schema_nodes(output_schema(branch,require_basis=True,completed_steps=['Q1:S1']))
        check_schema_nodes(review_schema(raw))
        for issues in [[{'question_id':'Q1','path':'board'}],[{'question_id':'Q1','path':'proof.steps.0.basis_ids','code':'missing_basis'}]]:
            check_schema_nodes(patch_schema(issues,raw))
    check_schema_nodes(native_schema(DiagramPatch.model_json_schema()))


def test_invalid_gateway_node_stops_before_http_and_records_no_send(settings):
    import httpx
    from dataclasses import replace
    from math_agent.provider import DeepSeek,ModelError
    from math_agent.parallel_budget import ParallelBudget
    calls=[];audit=[]
    def handler(request):calls.append(request);raise AssertionError('preflight must prevent HTTP')
    c=DeepSeek(replace(settings,solve_responses=True,compact_tools=True),lambda k,v:audit.append((k,v)),httpx.MockTransport(handler))
    c.shared_budget=ParallelBudget(12,timeout_seconds=240)
    try:
        with pytest.raises(ModelError,match='发送前预检'):
            c.generate('solve',[],output_schema={'type':'object','properties':{'Q1':{'allOf':[{'type':'object'}]}}})
        assert not calls and any(k=='model' and v.get('not_sent') for k,v in audit)
        usage=c.shared_budget.snapshot()['usage']
        assert usage['complete'] and usage['not_sent_calls']==1
    finally:c.close()
