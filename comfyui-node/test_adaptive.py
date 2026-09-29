# SPDX-License-Identifier: GPL-3.0-only
"""Offline tests: no credentials, no Jev calls, no GPU allocation."""
import importlib.util
import pathlib
import sys
import types
import unittest
from unittest.mock import patch as mock_patch
import torch

ROOT = pathlib.Path(__file__).parent
pkg = types.ModuleType("jev_test_package"); pkg.__path__ = [str(ROOT)]
sys.modules[pkg.__name__] = pkg
from jev_test_package.adaptive import AdaptiveController, sampler_wrapper
from jev_test_package.jev_client import ask

def answer(choice="15", confidence=.9):
    return {"choice": choice, "confidence": confidence, "probabilities": {choice: 1}, "usage": None}

def make(chooser=None, **kwargs):
    p=types.SimpleNamespace(topk_ratio=.05)
    c=AdaptiveController(p, chooser=chooser or (lambda s: answer()), **kwargs)
    c.begin([1,.8,.5,.2,0])
    return p,c

def observe(c):
    c.observe_gate(torch.arange(4096,dtype=torch.float32).reshape(1,64,1,64),0)

class Tests(unittest.TestCase):
    def test_next_step_budget_and_final_no_request(self):
        states=[]; choices=iter(['5','15','7.5'])
        def choose(s): states.append(s); return answer(next(choices))
        p,c=make(choose); used=[]
        for i in range(4):
            used.append(p.topk_ratio); observe(c); c.completed_step(i)
        self.assertEqual(used,[.1,.05,.15,.075]); self.assertEqual(c.requests,3)
        self.assertEqual([s['next_step'] for s in states],[2,3,4])
        self.assertEqual(c.events[-1]['reason'],'last_step_no_request')
    def test_failure_and_circuit_breaker(self):
        calls=[]
        def fail(s): calls.append(s); raise TimeoutError('secret must not be logged')
        p,c=make(fail)
        for i in range(3): observe(c); c.completed_step(i)
        self.assertEqual(len(calls),1); self.assertEqual(p.topk_ratio,.1)
        self.assertNotIn('secret',str(c.events)); self.assertEqual(c.events[-1]['reason'],'api_circuit_open')
    def test_low_confidence_and_bad_choice(self):
        for a in [answer('5',.49),answer('80'),answer('5',float('nan'))]:
            p,c=make(lambda s:a); observe(c); c.completed_step(0)
            self.assertEqual(p.topk_ratio,.1)
    def test_nonfinite_response_metadata_falls_back(self):
        a=answer('20'); a['probabilities']={'20':float('nan')}
        p,c=make(lambda s:a); observe(c); c.completed_step(0)
        self.assertEqual(p.topk_ratio,.1)
        self.assertTrue(c.disabled)
    def test_fixed_node_keeps_original_inputs(self):
        import ast
        tree=ast.parse((ROOT/'nodes.py').read_text(encoding='utf-8'))
        node=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='H3V2JevAdaptiveVSAPatch')
        calls=[]
        class Parent:
            def patch(self,*args,**kwargs): calls.append((args,kwargs)); return ('model',)
        def require(condition,message):
            if not condition: raise RuntimeError(message)
        namespace={'H3V2StreamingVSAPatch':Parent,'require':require}
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(ROOT/'nodes.py'),'exec'),namespace)
        cls=namespace['H3V2JevAdaptiveVSAPatch']
        self.assertEqual(cls().patch('m','cache',7.5,mode='fixed'),('model',))
        self.assertEqual(calls[0][0],('m','cache',7.5,0.0,1.0,12288,True))
        self.assertIsNone(calls[0][1]['_adaptive'])
        cls().patch('m','cache',5,mode='fixed',producer_chunk=8192)
        self.assertEqual(calls[-1][1]['producer_chunk'],8192)
    def test_duplicate_callbacks_no_extra_requests(self):
        p,c=make(); observe(c); c.completed_step(0); c.completed_step(0)
        self.assertEqual(c.requests,1)
    def test_max_budget_and_missing_stats(self):
        p,c=make(max_requests=0); observe(c); c.completed_step(0)
        self.assertEqual(c.requests,0); self.assertEqual(p.topk_ratio,.1)
        p,c=make(); c.completed_step(0); self.assertEqual(c.requests,0)
    def test_gate_sample_once_per_step_and_change(self):
        p,c=make(); observe(c); first=c.stats.copy()
        c.observe_gate(torch.ones(1,64,1,64),49)
        self.assertEqual(c.stats,first); self.assertLessEqual(c.stats['samples'],1024)
        c.completed_step(0); c.observe_gate(torch.ones(1,64,1,64),0)
        self.assertGreater(c.stats['relative_mean_change'],.9)
    def test_missing_key_never_starts_worker(self):
        with mock_patch.dict('os.environ',{},clear=True), mock_patch('subprocess.run') as run:
            with self.assertRaises(RuntimeError): ask({},'',1)
            run.assert_not_called()
    def test_callback_wrapper_fresh_run_and_cleanup(self):
        p=types.SimpleNamespace(topk_ratio=.05); seen=[]; callbacks=[]
        class Executor:
            class_obj=types.SimpleNamespace(sampler_function=types.SimpleNamespace(__name__='sample_res_multistep'))
            def __call__(self,model,sigmas,extra,callback,noise,*args):
                for i in range(4):
                    seen.append(p.topk_ratio); observe(p.adaptive_controller)
                    callback(i,None,None,4)
                return 'ok'
        wrapped=sampler_wrapper(p,{'chooser':lambda s:answer('20')})
        for _ in range(2):
            self.assertEqual(wrapped(Executor(),None,torch.tensor([1,.8,.5,.2,0]),{},lambda *a:callbacks.append(a),None),'ok')
            self.assertIsNone(p.adaptive_controller)
        self.assertEqual(seen,[.1,.2,.2,.2]*2); self.assertEqual(len(callbacks),8)
    def test_unsupported_sampler_no_network(self):
        p=types.SimpleNamespace(topk_ratio=.05)
        class Executor:
            class_obj=types.SimpleNamespace(sampler_function=types.SimpleNamespace(__name__='sample_heun'))
            def __call__(self,*args): return p.topk_ratio
        def forbidden(s): raise AssertionError('must not call')
        self.assertEqual(sampler_wrapper(p,{'chooser':forbidden})(Executor(),None,torch.tensor([1,0]),{},None,None),.1)

class AVTests(unittest.TestCase):
    def test_modal_mapping_depths_and_low_keep(self):
        from jev_test_package.av_adaptive import AVController
        p=types.SimpleNamespace(topk_ratio=.1)
        c=AVController(p,chooser=lambda state:answer('1'))
        c.begin([1,.8,.5,.2,0])
        self.assertEqual(p.topk_ratio,.05)
        original=torch.full((80,16),999.)
        original[10:30]=2; original[30:70]=3
        permutation=torch.arange(79,-1,-1)
        plan={'inv':torch.argsort(permutation)}
        layout=types.SimpleNamespace(segments=[(0,10,'ref_img'),(10,30,'audio'),(30,70,'video')])
        gate=original[permutation].reshape(1,80,1,16)
        for b in (0,24): c.observe_gate(gate,b,plan,layout)
        self.assertIsNone(c.stats)
        c.observe_gate(gate,49,plan,layout)
        for data in c.stats['blocks'].values():
            self.assertEqual(data['audio']['mean_abs'],2)
            self.assertEqual(data['video']['mean_abs'],3)
        c.completed_step(0)
        self.assertEqual(p.topk_ratio,.01); self.assertEqual(c.requests,1)
        c.observe_gate(gate*2,0,plan,layout)
        c.completed_step(0)
        self.assertIn(0,c.observations)
        for b in (24,49): c.observe_gate(gate*2,b,plan,layout)
        self.assertEqual(c.stats['blocks']['49']['video']['relative_l2_change'],1)
        c.completed_step(1); self.assertEqual(c.requests,2)

class BlockTests(unittest.TestCase):
    def build(self,chooser=None):
        from jev_test_package.block_adaptive import BlockController
        p=types.SimpleNamespace(topk_ratio=.05)
        response={'decisions':{f'b{i:02d}':{'choice':'skip','confidence':.9,'probabilities':{'skip':.9,'execute':.1}} for i in range(1,50)}}
        c=BlockController(p,chooser=chooser or (lambda state:response),min_confidence=.5)
        c.begin([1,.97,.92,.8,0])
        for i in range(50):c.measured[i]={'relative_residual_l2':{'audio':i/100,'video':i/100},'measured_step':1}
        return p,c
    def test_block_zero_budget_and_refresh(self):
        p,c=self.build();c.completed_step(0)
        self.assertEqual(c.skip_set,set(range(1,21)));self.assertFalse(c.should_skip(0))
        for b in range(1,50):c.should_skip(b)
        old=c.skip_set.copy();c.completed_step(1)
        self.assertFalse(old&c.skip_set);self.assertLessEqual(len(c.skip_set),20)
        c.completed_step(2);c.completed_step(3);self.assertEqual(c.requests,3)
    def test_two_consecutive_skip_limit(self):
        p,c=self.build();c.max_skip_blocks=30;c.max_consecutive_skips=2;c.max_requests=4
        c.begin([1,.98,.95,.9,.8,0]);streak={b:0 for b in range(50)}
        for step in range(5):
            for b in range(50):
                skipped=c.should_skip(b);streak[b]=streak[b]+1 if skipped else 0
                self.assertLessEqual(streak[b],2)
            self.assertEqual(streak[0],0)
            c.completed_step(step)
        self.assertEqual(c.requests,4)
    def test_block_error_executes_all(self):
        def fail(state):raise TimeoutError('secret')
        p,c=self.build(fail);c.completed_step(0);self.assertEqual(c.skip_set,set());self.assertTrue(c.disabled)
        c.completed_step(1);self.assertEqual(c.requests,1);self.assertNotIn('secret',str(c.events))
    def test_identity_hook_does_not_call_block(self):
        import ast
        p,c=self.build();c.skip_set={1};p.adaptive_controller=c
        tree=ast.parse((ROOT/'streaming.py').read_text());fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='make_block_patch')
        ns={};exec(compile(ast.Module(body=[fn],type_ignores=[]),'streaming.py','exec'),ns)
        def forbidden(args):raise AssertionError('skipped block must not execute')
        x=torch.ones(10,16);out=ns['make_block_patch'](None,1,p,None)({'img':x},{'original_block':forbidden})
        self.assertIs(out['img'],x)
    def test_modal_measurement_excludes_refs(self):
        p,c=self.build();layout=types.SimpleNamespace(segments=[(0,5,'ref_img'),(5,10,'audio'),(10,20,'video')])
        x=torch.ones(20,32);x[:5]=999
        before=c.sample(x,layout);after=c.sample(x*1.1,layout);c.observe_block(9,before,after)
        self.assertAlmostEqual(c.measured[9]['relative_residual_l2']['audio'],.1,places=5)

class LayerTests(unittest.TestCase):
    def make_layer(self,chooser):
        from jev_test_package.layer_adaptive import LayerController
        p=types.SimpleNamespace(topk_ratio=.05)
        c=LayerController(p,chooser=chooser,min_confidence=.4,max_requests=3)
        c.begin([1,.8,.5,.2,0]);return p,c
    def test_protection_and_layer_budgets(self):
        states=[]
        def choose(s):
            states.append(s)
            return {'decisions':{f'b{b:02d}':{'choice':'protect' if b==49 else 'reduce','confidence':.9} for b in range(1,50)}}
        p,c=self.make_layer(choose)
        for b in range(50):
            self.assertFalse(c.should_skip(b));self.assertEqual(p.topk_ratio,.05)
        for step in range(4):
            c.pending={b:torch.tensor([b+1.,-1.,1.,.3,b+2.,-1.,1.,.3]) for b in range(50)}
            c.completed_step(step)
        self.assertEqual(c.requests,3);self.assertEqual(len(states),3)
        self.assertEqual(c.keeps[0],5);self.assertEqual(c.keeps[1],2);self.assertEqual(c.keeps[20],1);self.assertEqual(c.keeps[49],10)
        self.assertEqual(c.events[-1]['reason'],'last_step_no_request')
    def test_invalid_and_low_confidence_fall_back5(self):
        for bad in [False,True]:
            def choose(s):
                return {'decisions':{f'b{b:02d}':{'choice':'invalid' if bad else 'reduce','confidence':.1} for b in range(1,50)}}
            p,c=self.make_layer(choose);c.pending={b:torch.ones(8) for b in range(50)};c.completed_step(0)
            self.assertEqual(c.keeps,[5.]*50)
    def test_modal_measurements_and_temporal_difference(self):
        p,c=self.make_layer(lambda s:None)
        layout=types.SimpleNamespace(segments=[(0,2,'reference'),(2,6,'audio'),(6,10,'video')])
        x=torch.ones(10,16);a=c.sample(x,layout);b=c.sample(x*2,layout)
        c.gates[0]={'audio':torch.ones(4,16),'video':torch.ones(4,16)}
        c.observe_block(0,a,b)
        self.assertAlmostEqual(float(c.pending[0][0]),1.);self.assertEqual(float(c.pending[0][1]),-1.)
        c.observe_block(0,a,b);self.assertEqual(float(c.pending[0][1]),0.)

class BudgetTests(unittest.TestCase):
    def test_allocation_budget_and_entrance(self):
        from jev_test_package.layer_adaptive import BudgetLayerController
        c=BudgetLayerController(types.SimpleNamespace(topk_ratio=.05),chooser=lambda s:None,min_confidence=.4)
        ds={f'b{b:02d}':{'choice':'protect' if b>40 else 'baseline','confidence':.9,'probabilities':{'reduce':0.,'baseline':.1 if b>40 else 1.,'protect':.9 if b>40 else 0.}} for b in range(1,50)}
        for target in [2.5,3.,3.5]:
            a=c.allocate({'decisions':ds,'budget':{'choice':str(target),'confidence':.9}})
            self.assertLessEqual(sum(a)/50,target);self.assertEqual(a[:2],[5,5]);self.assertTrue(any(v>5 for v in a));self.assertTrue(any(v<5 for v in a))
        a=c.allocate({'decisions':ds,'budget':{'choice':'3','confidence':.1}});self.assertLessEqual(sum(a)/50,3.5);self.assertEqual(a[:2],[5,5])

class DistributionTests(unittest.TestCase):
    def test_installer_contains_adaptive_modules_and_examples(self):
        spec=importlib.util.spec_from_file_location('jev_setup_test',ROOT/'setup_env.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        files=module.distribution_files()
        self.assertTrue(all((ROOT/p).is_file() for p in files))
        self.assertEqual(len(files),len(set(files)))
        for name in ['adaptive.py','av_adaptive.py','block_adaptive.py','layer_adaptive.py','jev_client.py','examples/jev_layer_v5_4step.api.json','examples/fixed5_4step.api.json']:
            self.assertIn(pathlib.Path(name),files)
    def test_example_pair_changes_only_mode_and_output(self):
        import json
        fixed=json.loads((ROOT/'examples/fixed5_4step.api.json').read_text(encoding='utf-8'))
        adaptive=json.loads((ROOT/'examples/jev_layer_v5_4step.api.json').read_text(encoding='utf-8'))
        self.assertEqual(fixed['900']['inputs']['mode'],'fixed')
        self.assertEqual(adaptive['900']['inputs']['mode'],'jev_adaptive')
        self.assertEqual(adaptive['124']['inputs']['steps'],4)
        self.assertEqual(adaptive['900']['inputs']['sdk_python'],'')
        fixed['900']['inputs']['mode']='jev_adaptive'
        fixed['92']['inputs']['filename_prefix']=adaptive['92']['inputs']['filename_prefix']
        self.assertEqual(fixed,adaptive)

if __name__=='__main__': unittest.main(verbosity=2)
