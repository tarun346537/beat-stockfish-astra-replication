"""Verify published evidence using only Python's standard library; no API calls."""
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from evidence import validate_evidence

ROOT = Path(__file__).resolve().parent
EV = ROOT / 'evidence'

def read(path):
    return json.loads(path.read_text(encoding='utf-8'))

def check(condition, message):
    if not condition:
        raise ValueError(message)

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    result=read(EV/'result.json')
    source=read(EV/'source.json')
    for name, expected in source['adapter_files_sha256'].items():
        check(sha(ROOT/name)==expected, 'Adapter source hash mismatch: '+name)
    aggregate=hashlib.sha256(json.dumps({'upstream':source['tracked_files_sha256'],'adapter':source['adapter_files_sha256']},sort_keys=True).encode()).hexdigest()
    check(aggregate==source['executed_source_sha256']==result['executed_source_sha256'],'Source aggregate mismatch')
    verdict=validate_evidence(EV/'canonical')
    check(verdict['valid'] and verdict['engine_use'], 'Invalid evidence or missing engine-use event')
    check(verdict==result['verdict'],'Saved and recomputed verdicts differ')
    captures=read(EV/'canonical/captures.json')
    for label, name in [('before','state.before-grade.json'),('after','state.json'),('grade','grade.json')]:
        meta=captures[label]; p=EV/'canonical'/name
        check(meta['ok'] and meta['process_success'] and meta['uid']==0 and meta['gid']==0, 'Invalid protected capture metadata')
        check(sha(p)==meta['sha256'] and p.stat().st_size==meta['size'],'Capture hash/size mismatch')
    check(captures['before']['mode']==captures['after']['mode']=='0o600','State protection mismatch')
    stop=read(EV/'stop-record.json')
    check(stop['host_stopped'] and stop['container_stopped'] and stop['source_unchanged_after_stop'],'Stop/source record mismatch')
    check(sha(ROOT/'recorded-checkpoint/checkpoint_and_stop.py')==stop['checkpoint_source_sha256'],'Checkpoint source mismatch')
    conversation=read(EV/'visible-conversation.json')['items']
    calls=[i for i in conversation if i['type']=='function_call']
    outputs={i['call_id']:i['output'] for i in conversation if i['type']=='function_call_output'}
    trace=read(EV/'action-trace.json')['actions']
    check(len(calls)==len(trace)==45 and len(outputs)==44,'Unexpected transcript counts')
    for call, action in zip(calls, trace):
        check(all(call[k]==action[k] for k in ['call_id','name','arguments']),'Transcript call mismatch')
        check(outputs.get(call['call_id'])==action['tool_result'],'Transcript result mismatch')
    initial=read(EV/'initial-request.json')
    check(conversation[:len(initial['input'])]==initial['input'],'Initial task mismatch')
    check(initial['model']=='gpt-6-astra' and 'access_programs' not in initial,'Unexpected initial model/access')
    usage=read(EV/'request-usage.json')
    totals=dict(input_tokens=0,output_tokens=0,cached_input_tokens=0,cache_write_input_tokens=0,reasoning_output_tokens=0)
    guard=Decimal(0); estimated=Decimal(0)
    check(len(usage['requests'])==45 and Decimal(usage['unresolved_reserved_usd'])==0,'Request accounting mismatch')
    for n, req in enumerate(usage['requests'],1):
        check(req['sequence']==n and req['status']=='settled' and req['model_requested']=='gpt-6-astra','Request sequence/model/status mismatch')
        settings=req['settings']
        check(settings=={k:initial[k] for k in settings},'Model settings changed across requests')
        check('access_programs' not in settings,'Unexpected access-program setting')
        u=req['usage']; inp=u['input_tokens']; out=u['output_tokens']; details=u['input_tokens_details']
        cached=details.get('cached_tokens',0); written=details.get('cache_write_tokens',0)
        check(0<=cached+written<=inp<=272000,'Unsupported usage/pricing range')
        totals['input_tokens']+=inp; totals['output_tokens']+=out
        totals['cached_input_tokens']+=cached; totals['cache_write_input_tokens']+=written
        totals['reasoning_output_tokens']+=u['output_tokens_details'].get('reasoning_tokens',0)
        conservative=(Decimal(inp)*Decimal('12.5')+Decimal(out)*50)/1000000
        check(conservative==Decimal(req['conservative_estimated_usd']),'Per-request conservative estimate mismatch')
        guard+=conservative
        estimated+=(Decimal(inp-cached-written)*10+Decimal(cached)+Decimal(written)*Decimal('12.5')+Decimal(out)*50)/1000000
    check(totals==result['usage'],'Usage total mismatch')
    check(guard==Decimal(result['conservative_guard_estimated_cost_usd']),'Guard total mismatch')
    check(estimated==Decimal(result['usage_category_estimated_cost_usd']),'Usage-category estimate mismatch')
    print(json.dumps({'status':'PASS','classification':verdict['classification'],'engine_searches':verdict['state']['engine_searches'],'grader_score':verdict['grade']['score'],'completed':verdict['completed'],'tool_calls':len(calls),'returned_tool_results':len(outputs),'usage':totals,'estimated_cost_usd':str(estimated),'conservative_estimate_usd':str(guard),'network_calls':0},indent=2))

if __name__=='__main__':
    main()
