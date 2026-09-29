# SPDX-License-Identifier: GPL-3.0-only
"""Per-layer attention budgets; all blocks execute, first step and block0 protected."""
import json, math, time
import torch
from .block_adaptive import BlockController

class LayerController(BlockController):
    policy="layer_v4"
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.keeps=[5.0]*50;self.previous={};self.pending={};self.gates={}
    @staticmethod
    def levels(b):
        # Preserve entry/exit coverage; interior has the largest savings opportunity.
        return {'reduce':2.0 if b<10 else (3.0 if b>=45 else 1.0),
                'baseline':5.0,'protect':10.0 if b>=45 else 7.5}
    def begin(self,sigmas):
        self.sigmas=[float(x) for x in sigmas];self.total=len(sigmas)-1
        assert self.total==4, 'layer policies require four steps'
        self.patch.topk_ratio=.05
        self.emit({'event':'begin','total_steps':self.total,'max_requests':self.max_requests,'policy':self.policy,'first_step_keep':5,'block0_keep':5,'identity_skips':False})
    def should_skip(self,index):
        self.patch.topk_ratio=self.keeps[index]/100
        return False
    def observe_gate(self,gate,block_index,plan=None,layout=None):
        if self.step_index==3:return
        matrix=gate.detach().reshape(gate.shape[1],-1)
        channels=matrix[:,::max(1,matrix.shape[1]//16)][:,:16]
        out={}
        for start,end,kind in layout.segments:
            if kind in ('audio','video') and end>start:
                idx=torch.linspace(start,end-1,min(64,end-start),device=gate.device).long()
                out[kind]=channels.index_select(0,plan['inv'].index_select(0,idx)).float()
        self.gates[block_index]=out
    def sample(self,x,layout):
        return {} if self.step_index==3 else super().sample(x,layout)
    def observe_block(self,index,before,after):
        if self.step_index==3:return
        fields=[]
        for kind in ('audio','video'):
            a,b=before[kind],after[kind];d=b-a
            prev=self.previous.get((index,kind))
            drift=torch.tensor(-1.,device=a.device) if prev is None else (d-prev).norm()/prev.norm().clamp_min(1e-8)
            g=self.gates[index][kind].abs().flatten()
            fields.extend([(d.norm()/a.norm().clamp_min(1e-8)),drift,g.mean(),g.topk(max(1,g.numel()//10)).values.sum()/g.sum().clamp_min(1e-8)])
            self.previous[index,kind]=d.detach()
        self.pending[index]=torch.stack(fields)
    def completed_step(self,step):
        if step<=self.last_step:return
        self.last_step=step
        event={'event':'step','policy':self.policy,'step':step+1,'total_steps':4,'sigma':self.sigmas[step],
            'applied_keep_percent':sum(self.keeps)/50,'applied_layer_keep_percent':list(self.keeps),'computed_block_count':50,'applied_skip_blocks':[]}
        if step==3:
            event.update(reason='last_step_no_request',next_keep_percent=None);self.emit(event);return
        started=time.perf_counter();selected=[5.]*50;reason='fallback_5'
        if self.disabled:reason='api_circuit_open'
        elif self.requests>=self.max_requests:reason='request_budget_exhausted'
        elif set(self.pending)!=set(range(50)):reason='incomplete_measurements'
        else:
            rows=torch.stack([self.pending[b] for b in range(50)]).cpu().tolist()
            if not all(math.isfinite(v) for row in rows for v in row):reason='nonfinite_measurements'
            else:
                blocks={}
                ranks={k:sorted(range(50),key=lambda b:rows[b][offset]) for k,offset in [('audio',0),('video',4)]}
                for b,row in enumerate(rows):
                    blocks[str(b)]={'current_keep':self.keeps[b],'candidate_keep':self.levels(b),'depth':b/49}
                    for kind,offset in [('audio',0),('video',4)]:
                        vals=row[offset:offset+4]
                        blocks[str(b)][kind]={'residual_relative_l2':round(vals[0],6),'residual_rank':round((ranks[kind].index(b)+1)/50,3),
                            'residual_cross_step_change':None if vals[1]<0 else round(vals[1],6),'gate_mean_abs':round(vals[2],6),'gate_top10_mass':round(vals[3],6)}
                state={'policy':self.policy,'measured_step':step+1,'next_step':step+2,'total_steps':4,'sigma':self.sigmas[step],'next_sigma':self.sigmas[step+1],
                    'constraints':'All 50 blocks execute every step. Entire first step and block0 every step are fixed5. No cache or identity bypass. Resolution1024x1792,124frames.',
                    'human_feedback':'Fixed5 4step preferred over previous block-skip trials. 3step speech sounded metallic. Preserve reference black bun hairstyle and speech.',
                    'measurement_limits':'Residual = sampled block output minus input, separately actual audio/video tokens, excluding references/padding. Rank within each modality. Cross-step change includes normal denoising and previous keep changes; not causal sparse error. Gate activation is NOT attention mass. None is a perceptual quality score.',
                    'objective':'Reduce average keep below5 by spending less on relatively low-impact layers and more on the important layers. Do not protect all layers merely because no calibrated quality labels. This is a measured exploratory test; preserve worst modality.',
                    'budget_mean_keep_choices':[2.5,3.0,3.5], 'budget_rule':'For layer_v5 ONLY: classify relative importance; host uses probabilities to rank allocation within selected mean budget. Start from layer reduce levels, keep blocks0,1 >=5, increase highest protect layers (at most5), then fill baseline on remaining highest-risk layers. Thus baseline/protect labels are priorities, not independent binding percentages.', 'blocks':blocks}
                self.requests+=1
                try:
                    answer=self.chooser(state);json.dumps(answer,allow_nan=False)
                    assert set(answer['decisions'])=={f'b{b:02d}' for b in range(1,50)}
                    for b in range(1,50):
                        d=answer['decisions'][f'b{b:02d}'];conf=float(d['confidence']);assert math.isfinite(conf) and 0<=conf<=1
                        assert d['choice'] in self.levels(b)
                        if conf>=self.min_confidence:selected[b]=self.levels(b)[d['choice']]
                    if self.policy=='layer_v5':
                        selected=self.allocate(answer)
                        event['allocation_budget']=3.5 if float(answer['budget']['confidence'])<self.min_confidence else float(answer['budget']['choice'])
                        event['allocation_reason']='low_confidence_cautious_3.5' if float(answer['budget']['confidence'])<self.min_confidence else 'jev_budget'
                    event['answer']=answer;reason='jev'
                except Exception as exc:reason='api_error:'+type(exc).__name__;self.disabled=True;selected=[5.]*50
                event['state']=state
        event.update(reason=reason,next_keep_percent=sum(selected)/50,next_layer_keep_percent=selected,request_count=self.requests,decision_seconds=round(time.perf_counter()-started,6))
        self.keeps=selected;self.step_index=step+1;self.pending={};self.gates={};self.emit(event)

class BudgetLayerController(LayerController):
    policy='layer_v5'
    def allocate(self,answer):
        budget=answer['budget'];target=float(budget['choice']);assert target in (2.5,3.,3.5)
        confidence=float(budget['confidence']);assert math.isfinite(confidence) and 0<=confidence<=1
        if confidence<self.min_confidence:target=3.5
        ds=answer['decisions']
        def score(b):
            probs=ds[f'b{b:02d}']['probabilities']
            assert set(probs)=={'reduce','baseline','protect'}
            assert all(isinstance(v,(int,float)) and math.isfinite(v) and 0<=v<=1 for v in probs.values())
            assert abs(sum(probs.values())-1)<.02
            return probs['baseline']+2*probs['protect']
        ranked=sorted(range(2,50),key=score,reverse=True)
        result=[self.levels(b)['reduce'] for b in range(50)];result[0]=result[1]=5.
        # Classifications express risk priorities. Host converts them to percentages under a global budget.
        protected=[b for b in ranked if ds[f'b{b:02d}']['choice']=='protect' and ds[f'b{b:02d}']['confidence']>=self.min_confidence][:5]
        for b in protected:
            wanted=self.levels(b)['protect']
            if sum(result)+wanted-result[b]<=target*50:result[b]=wanted
        for b in ranked:
            if b in protected:continue
            if sum(result)+5-result[b]<=target*50:result[b]=5.
        assert max(result)<=10 and sum(result)/50<=target
        return result
