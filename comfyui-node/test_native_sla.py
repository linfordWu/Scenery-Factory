# SPDX-License-Identifier: GPL-3.0-only
"""Offline policy, transport and portable-workflow tests. No API calls or generation."""
import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

parser = argparse.ArgumentParser()
parser.add_argument('--comfy-root', required=True)
args, rest = parser.parse_known_args()
sys.argv = [sys.argv[0], *rest]
sys.path.insert(0, args.comfy_root)
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('native_sla_under_test', HERE / 'native_sla.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
import torch


def answer(first=False, choice='3', confidence=.9):
    return {'decisions': {str(i): {'choice': '10' if first and i == 0 else choice,
                                 'confidence': confidence, 'probabilities': {}}
                          for i in range(0 if first else 1, 50)}}


def completed_response(data):
    return types.SimpleNamespace(returncode=0, stdout=json.dumps(data))


class NativePolicyTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.logger = patch.object(module, 'emit', self.events.append)
        self.logger.start()
        self.addCleanup(self.logger.stop)
        # These cases exercise the Jev worker transport; Laya-mode coverage is in test_laya_client.py.
        self.engine = patch.dict(os.environ, {'H3_DECISION_ENGINE': 'jev'})
        self.engine.start()
        self.addCleanup(self.engine.stop)

    def controller(self, mode='jev_first'):
        return module.Controller(types.SimpleNamespace(), 'test-python', mode, '{"prompt":"test"}')

    def measurements(self, c):
        c.pending = {i: torch.tensor([.1 + i / 100, .2, .3 + i / 100, .4]) for i in range(50)}

    def test_fixed_first_modes_do_not_call_api(self):
        with patch.object(module.subprocess, 'run') as run:
            for mode, expected in [('fixed5', 5), ('fixed10', 10)]:
                c = self.controller(mode)
                c.initialize()
                self.assertEqual(c.keeps, [expected] * 50)
            run.assert_not_called()

    def test_first_step_decides_all_fifty_and_uses_worker(self):
        c = self.controller()
        with patch.object(module.subprocess, 'run', return_value=completed_response(answer(True))) as run:
            c.initialize()
        self.assertEqual(c.keeps, [10] + [3] * 49)
        self.assertEqual(c.requests, 1)
        self.assertTrue(run.call_args.args[0][4].endswith('native_sla_worker.py'))
        state = json.loads(run.call_args.kwargs['input'])
        self.assertTrue(state['initialization'])
        self.assertEqual(state['next_step'], 1)

    def test_first_low_confidence_uses_ten(self):
        c = self.controller()
        with patch.object(module.subprocess, 'run', return_value=completed_response(answer(True, confidence=.2))):
            c.initialize()
        self.assertEqual(c.keeps, [10] * 50)

    def test_invalid_initial_answer_is_atomic_and_circuit_breaks(self):
        for broken in [answer(True, choice='100'), answer(True, confidence=float('nan')), {'decisions': {}}]:
            c = self.controller()
            with patch.object(module.subprocess, 'run', return_value=completed_response(broken)):
                c.initialize()
            self.assertTrue(c.disabled)
            self.assertEqual(c.keeps, [10] * 50)
            with patch.object(module.subprocess, 'run') as run:
                c.done(0)
                run.assert_not_called()

    def test_later_block_zero_and_low_confidence(self):
        c = self.controller('fixed10')
        self.measurements(c)
        data = answer(choice='1')
        data['decisions']['2']['confidence'] = .1
        with patch.object(module.subprocess, 'run', return_value=completed_response(data)):
            c.done(0)
        self.assertEqual(c.keeps[0], 5)
        self.assertEqual(c.keeps[1], 1)
        self.assertEqual(c.keeps[2], 5)
        self.assertEqual(self.events[0]['applied_layer_keep_percent'], [10] * 50)

    def test_four_requests_total_no_final_request(self):
        c = self.controller()
        with patch.object(module.subprocess, 'run', return_value=completed_response(answer(True))):
            c.initialize()
        with patch.object(module.subprocess, 'run', return_value=completed_response(answer())) as run:
            for step in range(4):
                self.measurements(c)
                c.done(step)
            self.assertEqual(run.call_count, 3)
        self.assertEqual(c.requests, 4)

    def test_later_transport_failure_resets_and_does_not_log_exception(self):
        c = self.controller('fixed10')
        self.measurements(c)
        with patch.object(module.subprocess, 'run', side_effect=RuntimeError('do-not-log-sensitive-transport-body')):
            c.done(0)
        self.assertTrue(c.disabled)
        self.assertEqual(c.keeps, [5] * 50)
        self.assertNotIn('do-not-log-sensitive', json.dumps(self.events))

    def test_laya_mode_controller_uses_local_engine(self):
        with patch.dict(os.environ, {'H3_DECISION_ENGINE': 'laya'}):
            c = self.controller()
            with patch.object(module, '_request', return_value=answer(True)) as request:
                c.initialize()
            self.assertEqual(c.keeps, [10] + [3] * 49)
            self.assertEqual(request.call_count, 1)

    def test_sampling_excludes_reference_rows_and_copies_before_mutation(self):
        x = torch.arange(120 * 32, dtype=torch.float32).reshape(120, 32)
        layout = types.SimpleNamespace(segments=[(0, 20, 'ref_img'), (20, 40, 'audio'), (40, 120, 'video')])
        sampled = module.sample(x, layout)
        self.assertEqual(set(sampled), {'audio', 'video'})
        self.assertEqual(sampled['video'].shape, (64, 16))
        before = sampled['audio'].clone()
        x.zero_()
        self.assertTrue(torch.equal(sampled['audio'], before))

    def test_node_defaults_and_actionable_preflight(self):
        fields = module.H3JevNativeSLAPatch.INPUT_TYPES()
        self.assertEqual(fields['optional']['initial_policy'][0][0], 'jev_first')
        with patch.dict(os.environ, {'H3_DECISION_ENGINE': 'jev'}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'TYPESAFE_API_KEY'):
                module.H3JevNativeSLAPatch().patch(None)
        with patch.dict(os.environ, {'H3_DECISION_ENGINE': 'jev', 'TYPESAFE_API_KEY': 'unit-test-placeholder'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'prompt_context'):
                module.H3JevNativeSLAPatch().patch(None)

    def test_laya_engine_needs_no_api_key_and_warms_up(self):
        calls = []
        import sys as _sys
        fake = types.ModuleType('laya_client')
        fake.warmup = lambda: calls.append('warmup')
        with patch.dict(os.environ, {'H3_DECISION_ENGINE': 'laya'}, clear=True), \
                patch.dict(_sys.modules, {'laya_client': fake}):
            with self.assertRaisesRegex(ValueError, 'prompt_context'):
                module.H3JevNativeSLAPatch().patch(None)
        self.assertEqual(calls, ['warmup'])

    def test_portable_pair_only_inserts_native_node(self):
        a = json.loads((HERE / 'examples/matlow_fused_4step.api.json').read_text(encoding='utf-8'))
        b = json.loads((HERE / 'examples/009jev_4step.api.json').read_text(encoding='utf-8'))
        node = b.pop('900')
        self.assertEqual(node['class_type'], 'H3JevNativeSLAPatch')
        self.assertEqual(node['inputs']['sdk_python'], '')
        self.assertEqual(node['inputs']['prompt_context'], b['131']['inputs']['prompt'])
        b['142']['inputs']['model'] = a['142']['inputs']['model']
        b['92'] = a['92']
        self.assertEqual(a, b)
        for graph in [a, b]:
            self.assertEqual(graph['127']['class_type'], 'UNETLoader')
            self.assertEqual(graph['124']['inputs']['steps'], 4)
            self.assertEqual(graph['129']['inputs']['noise_seed'], 2026)


if __name__ == '__main__':
    unittest.main()
