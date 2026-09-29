# SPDX-License-Identifier: GPL-3.0-only
"""Real SDK with in-memory HTTP 503/timeout transports: zero remote API calls."""
import contextlib, importlib.util, io, json, os, pathlib, sys, types
from unittest.mock import patch
import httpx2
import typesafe_sdk

root=pathlib.Path(__file__).parent
pkg=types.ModuleType('jev_sdk_test');pkg.__path__=[str(root)];sys.modules[pkg.__name__]=pkg
from jev_sdk_test import jev_client
from jev_sdk_test.adaptive import AdaptiveController
real_client=typesafe_sdk.TypeSafeClient
results=[]
for kind in ['http_503','read_timeout']:
    calls=[]
    def handler(request):
        calls.append(request.url.path)
        if kind=='read_timeout':raise httpx2.ReadTimeout('simulated',request=request)
        return httpx2.Response(503,json={'error':'simulated service unavailable'})
    def client(**kwargs):
        return real_client(**kwargs,transport=httpx2.MockTransport(handler))
    output=io.StringIO()
    with patch.dict(os.environ,{'TYPESAFE_API_KEY':'offline-placeholder'}), patch.object(typesafe_sdk,'TypeSafeClient',client), patch.object(sys,'stdin',io.StringIO(json.dumps({'state':{},'timeout':1}))), contextlib.redirect_stdout(output):
        jev_client.worker()
    answer=json.loads(output.getvalue());assert 'error' in answer
    assert len(calls)==1, 'SDK must not retry'
    attempts=[]
    def chooser(state):attempts.append(state);raise RuntimeError(answer['error'])
    p=types.SimpleNamespace(topk_ratio=.05);c=AdaptiveController(p,chooser=chooser)
    c.begin([1,.7,.3,0])
    for i in range(2):c.stats={'mean_abs':1};c.completed_step(i)
    assert p.topk_ratio==.1 and len(attempts)==1 and c.disabled
    results.append({'case':kind,'transport_attempts':len(calls),'remote_api_calls':0,
                    'worker_error_type':answer['error'],'fallback_keep_percent':p.topk_ratio*100,
                    'circuit_open':c.disabled,'passed':True})
print(json.dumps({'tests':results,'remote_api_calls':0},indent=2))
