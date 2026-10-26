import copy,json
from dataclasses import replace
import httpx
import pytest
from jsonschema import validate,ValidationError
from math_agent.compact_output import output_schema
from math_agent.provider import DeepSeek,ModelError
from test_compact_tools import pack


def named(raw):
    raw=copy.deepcopy(raw)
    for item in raw.values():
        b=item['board'];box=b.pop('bounding_box',None)
        if box:b['view']={'x_min':box[0],'x_max':box[2],'y_min':box[3],'y_max':box[1]}
    return raw


def test_output_schema_binds_conditions_and_named_ranges(sample):
    source,candidate,_=sample;schema=output_schema(source);raw=named(pack(candidate))
    validate(raw,schema)
    bad=copy.deepcopy(raw);bad['Q1']['proof']['condition_ids']=['Q1:S1']
    with pytest.raises(ValidationError):validate(bad,schema)
    assert 'bounding_box' not in schema['$defs']['Board']['properties']
    assert 'view' in schema['$defs']['Board']['properties']


def test_solve_native_keeps_tool_reasoning_and_never_uses_chat(settings,sample):
    from math_agent.responses_transport import strict_schema
    source,candidate,_=sample;schema=output_schema(source);original=copy.deepcopy(schema);raw=named(pack(candidate));calls=[]
    def handler(req):
        calls.append(req.url.path);b=json.loads(req.content)
        assert req.url.path=='/responses' and b['text']['format']['strict'] is True
        assert b['text']['format']['schema']==strict_schema(original)
        assert schema==original
        if len(calls)==1:
            return httpx.Response(200,json={'status':'completed','output':[
                {'type':'reasoning','content':[{'type':'reasoning_text','text':'private retained thought'}]},
                {'type':'function_call','call_id':'m1','name':'calculate','arguments':'{}'}]})
        assert b['input'][-3]['content'][0]['text']=='private retained thought'
        assert json.loads(b['input'][-1]['output'])=={'result':2}
        return httpx.Response(200,json={'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(raw)}]}]})
    cfg=replace(settings,compact_tools=True,solve_responses=True)
    c=DeepSeek(cfg,lambda *args:None,httpx.MockTransport(handler))
    try:
        result=c.generate('solve',[{'role':'user','content':'json'}],
            tools=[{'type':'function','function':{'name':'calculate','parameters':{'type':'object'}}}],
            dispatch=lambda *args:{'result':2},output_schema=schema)
        assert result==raw and calls==['/responses','/responses']
    finally:c.close()


def test_native_rejects_appended_explanation_without_retry(settings,sample):
    schema=output_schema(sample[0]);calls=[]
    def handler(req):
        calls.append(1)
        return httpx.Response(200,json={'status':'completed','output':[{'type':'message','content':[
            {'type':'output_text','text':json.dumps(named(pack(sample[1])))+'\n注意：解释'}]}]})
    c=DeepSeek(replace(settings,compact_tools=True,solve_responses=True),lambda *args:None,httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelError):c.generate('solve',[],output_schema=schema)
        assert calls==[1]
    finally:c.close()


def test_native_rejects_schema_wrapped_answer_without_flattening(settings,sample):
    schema=output_schema(sample[0]);calls=[];audit=[]
    wrapped={'type':'object','additionalProperties':False,'required':schema['required'],
             'properties':named(pack(sample[1]))}
    def handler(req):
        calls.append(1)
        return httpx.Response(200,json={'status':'completed','output':[{'type':'message','content':[
            {'type':'output_text','text':json.dumps(wrapped)}]}]})
    c=DeepSeek(replace(settings,compact_tools=True,solve_responses=True),
               lambda kind,value:audit.append((kind,value)),httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelError):c.generate('solve',[],output_schema=schema)
        saved=next(value for kind,value in audit if kind=='model_output_error')
        assert json.loads(saved['content'])==wrapped and calls==[1]
    finally:c.close()
