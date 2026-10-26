"""Experimental DeepSeek Responses wire adapter, not selected by production flow.
Reference: https://api-docs.deepseek.com/guides/responses_api/
"""
import copy


def check_schema_nodes(schema):
    """Preflight the node form required by the actual DeepSeek gateway.

    Additional JSON Schema rules are retained for local validation; this checks
    core nodes ($defs/properties/items/unions) without inventing missing types.
    Pydantic's discriminated oneOf unions were accepted in prior real requests.
    """
    def visit(node,path):
        if not isinstance(node,dict) or not any(k in node for k in ('type','anyOf','oneOf','$ref')):
            raise ValueError('DeepSeek输出Schema节点须有type/anyOf/oneOf/$ref：'+path)
        for key in ('$defs','$def','properties'):
            for name,child in node.get(key,{}).items():visit(child,path+'/'+key+'/'+name)
        if 'items' in node:visit(node['items'],path+'/items')
        for key in ('anyOf','oneOf'):
            for index,child in enumerate(node.get(key,[])):visit(child,path+'/'+key+'/'+str(index))
    visit(schema,'#')


def strict_schema(schema):
    result=copy.deepcopy(schema)
    def visit(node):
        if 'properties' in node:node['required']=list(node['properties'])
        for key in ('$defs','$def','properties'):
            for child in node.get(key,{}).values():visit(child)
        if 'items' in node:visit(node['items'])
        for key in ('anyOf','oneOf'):
            for child in node.get(key,[]):visit(child)
    visit(result)
    return result


def content_parts(content):
    if isinstance(content,str):return content
    result=[]
    for part in content:
        if part['type']=='text':result.append({'type':'input_text','text':part['text']})
        elif part['type']=='image_url':
            result.append({'type':'input_image','image_url':part['image_url']['url'],
                'detail':part['image_url'].get('detail','original')})
        else:raise ValueError('unsupported input part')
    return result


def output_items(items):
    result=[]
    for item in items:
        kind=item['type']
        if kind=='reasoning':result.append({'type':kind,'content':copy.deepcopy(item.get('content',[]))})
        elif kind=='function_call':
            result.append({k:item[k] for k in ('type','call_id','name','arguments')})
        elif kind=='message':
            result.append({'type':kind,'role':'assistant','content':[
                {'type':'output_text','text':p['text']} for p in item.get('content',[]) if p['type']=='output_text']})
        else:raise ValueError('unsupported response output item')
    return result


def input_items(messages):
    result=[]
    for message in messages:
        role=message['role']
        if role=='assistant' and '_response_items' in message:
            result.extend(output_items(message['_response_items']))
        elif role=='tool':
            result.append({'type':'function_call_output','call_id':message['tool_call_id'],'output':message['content']})
        else:result.append({'type':'message','role':role,'content':content_parts(message['content'])})
    return result


def body(settings,messages,schema,tools=None,tool_choice=None,thinking=True,strict=False):
    check_schema_nodes(schema)
    result={'model':settings.model,'input':input_items(messages),
        'reasoning':{'effort':settings.effort if thinking else 'none'},'max_output_tokens':settings.max_tokens,
        'text':{'format':{'type':'json_schema','name':'math_agent_output',
                          'schema':strict_schema(schema) if strict else schema,'strict':strict}}}
    if tools:result['tools']=[{'type':'function',**copy.deepcopy(t['function'])} for t in tools]
    if tool_choice is not None:result['tool_choice']=tool_choice
    return result


def message(result):
    if result.get('status')!='completed':raise ValueError('Responses incomplete/failed; no output accepted')
    items=result.get('output',[]);calls=[];text=[];seen=set()
    for item in items:
        if item['type']=='function_call':
            call_id=item['call_id']
            if not call_id or call_id in seen:raise ValueError('duplicate/empty call_id')
            seen.add(call_id)
            calls.append({'id':call_id,'type':'function','function':{'name':item['name'],'arguments':item['arguments']}})
        elif item['type']=='message':text.extend(p['text'] for p in item.get('content',[]) if p['type']=='output_text')
        elif item['type']!='reasoning':raise ValueError('unsupported response output')
    if not calls and not text:raise ValueError('no final output')
    value={'role':'assistant','content':''.join(text) or None,'_response_items':output_items(items)}
    if calls:value['tool_calls']=calls
    return value,'tool_calls' if calls else 'stop'
