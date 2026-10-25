"""Prepare an isolated Studio diagnostic task, without creating a model client."""
import argparse
import json
from types import SimpleNamespace
from .classic_studio import debug_root
from math_agent.image_input import save_image
from math_agent.storage import Store,write_json


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--text',default='')
    parser.add_argument('--question',action='append',default=[])
    parser.add_argument('--answer',action='append',default=[])
    args=parser.parse_args()
    if not args.text.strip() and not args.question:parser.error('question text or image is required')
    paths=[('question',p) for p in args.question]+[('answer',p) for p in args.answer]
    if len(paths)>6:parser.error('at most six images')
    store=Store(debug_root());job,directory=store.prepare();images=[]
    for i,(role,path) in enumerate(paths,1):
        file=f'I{i}.png'
        with open(path,'rb') as stream:sha=save_image(SimpleNamespace(stream=stream),directory/file)
        images.append({'id':f'I{i}','role':role,'file':file,'sha256':sha})
    write_json(directory/'request.json',{'text':args.text,'images':images,'force':True})
    store.enqueue(job)
    print(json.dumps({'debug_job_id':job,'paid_requests':0}))


if __name__=='__main__':main()
