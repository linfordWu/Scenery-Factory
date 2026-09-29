# SPDX-License-Identifier: GPL-3.0-only
"""Jev chooses execution/identity for blocks 1..49; never reuses activations."""
import json,math,time
import torch
from .adaptive import AdaptiveController

class BlockController(AdaptiveController):
    def __init__(self,*args,initial_keep=5.0,max_skip_blocks=20,max_consecutive_skips=1,**kwargs):
        super().__init__(*args,**kwargs)
        self.initial_keep=initial_keep
        self.max_skip_blocks=max_skip_blocks;self.max_consecutive_skips=max_consecutive_skips;self.streaks={b:0 for b in range(50)}
        self.skip_set=set();self.actual_skips=[];self.measured={};self.step_index=0
    def begin(self,sigmas):
        self.sigmas=[float(x) for x in sigmas];self.total=len(sigmas)-1
        self.patch.topk_ratio=self.initial_keep/100
        self.emit({'event':'begin','policy':'block_v3','total_steps':self.total,'keep_percent':self.initial_keep,'max_requests':self.max_requests,'max_skip_per_step':self.max_skip_blocks,'max_consecutive_skips':self.max_consecutive_skips,'mandatory_blocks':[0]})
    def observe_gate(self,*args,**kwargs):pass
    def should_skip(self,index):
        if index!=0 and index in self.skip_set:
            self.actual_skips.append(index);return True
        return False
    def sample(self,x,layout):
        channels=x[:,::max(1,x.shape[-1]//16)][:,:16]
        out={}
        for start,end,kind in layout.segments:
            if kind in ('audio','video') and end>start:
                idx=torch.linspace(start,end-1,min(64,end-start),device=x.device).long()
                out[kind]=channels.index_select(0,idx).detach().float()
        return out
    def observe_block(self,index,before,after):
        values={}
        for kind in ('audio','video'):
            if kind not in before or kind not in after:return
            a,b=before[kind],after[kind]
            value=float((b-a).norm()/a.norm().clamp_min(1e-8))
            if not math.isfinite(value):return
            values[kind]=round(value,6)
        self.measured[index]={'relative_residual_l2':values,'measured_step':self.step_index+1}
    def completed_step(self,step):
        if step<=self.last_step:return
        self.last_step=step
        event={'event':'step','policy':'block_v3','step':step+1,'total_steps':self.total,'sigma':self.sigmas[step],
            'applied_keep_percent':self.initial_keep,'applied_skip_blocks':sorted(set(self.actual_skips)),
            'computed_block_count':50-len(set(self.actual_skips)),'block_measurements':dict(self.measured)}
        if step==self.total-1:
            event.update(reason='last_step_no_request',next_keep_percent=None);self.emit(event);return
        for b in range(50):self.streaks[b]=self.streaks[b]+1 if b in self.actual_skips else 0
        ranked=sorted(range(1,50),key=lambda b:max(self.measured.get(b,{}).get('relative_residual_l2',{'unknown':float('inf')}).values()))
        state={'policy':'block_v3','current_step':step+1,'next_step':step+2,'total_steps':self.total,'current_sigma':self.sigmas[step],'next_sigma':self.sigmas[step+1],
            'keep_percent':self.initial_keep,'max_skip_blocks':self.max_skip_blocks,'max_consecutive_skips':self.max_consecutive_skips,'mandatory_execute':[0],
            'proxy_note':'residual activation change, NOT measured perceptual quality; no cached activation is reused',
            'blocks':{str(b):dict(self.measured.get(b,{}),relative_rank=(ranked.index(b)+1)/49,was_skipped=b in self.actual_skips,consecutive_skips=self.streaks[b]) for b in range(1,50)}}
        selected=set();reason='fallback_execute_all';started=time.perf_counter()
        if self.disabled:reason='api_circuit_open'
        elif self.requests>=self.max_requests:reason='request_budget_exhausted'
        elif len(self.measured)!=50:reason='incomplete_block_measurements'
        else:
            self.requests+=1
            try:
                answer=self.chooser(state)
                json.dumps(answer,allow_nan=False)
                decisions=answer['decisions'];assert set(decisions)=={f'b{b:02d}' for b in range(1,50)}
                eligible=[]
                for b in range(1,50):
                    d=decisions[f'b{b:02d}'];confidence=float(d['confidence'])
                    assert d['choice'] in ('execute','skip') and math.isfinite(confidence) and 0<=confidence<=1
                    if d['choice']=='skip' and confidence>=self.min_confidence and self.streaks[b]<self.max_consecutive_skips:eligible.append(b)
                selected=set(sorted(eligible,key=lambda b:ranked.index(b))[:self.max_skip_blocks]);reason='jev'
                event['answer']=answer
            except Exception as exc:
                selected=set();reason='api_error:'+type(exc).__name__;self.disabled=True
        event.update(reason=reason,next_keep_percent=self.initial_keep,next_skip_blocks=sorted(selected),request_count=self.requests,decision_seconds=round(time.perf_counter()-started,6),state=state)
        self.skip_set=selected;self.actual_skips=[];self.step_index=step+1;self.emit(event)
