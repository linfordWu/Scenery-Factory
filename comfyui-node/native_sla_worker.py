# SPDX-License-Identifier: GPL-3.0-only
import json, sys
from typesafe_sdk import Choice, RetryPolicy, TypeSafeClient
from jev_client import sla_questions
state = json.load(sys.stdin)
questions = {k: Choice(instructions=q['instructions'], criteria=q['criteria']) for k, q in sla_questions(state).items()}
try:
    with TypeSafeClient(model='jev-1.13.0', retry=RetryPolicy(max_retries=0), timeout=20, base_url='https://api.typesafe.ai') as client:
        r = client.system_one(state=state, questions=questions)
    print(json.dumps({'decisions': {k: {'choice': v.choice, 'confidence': v.confidence, 'probabilities': v.probabilities} for k, v in r.choices.items()}, 'model': r.model, 'usage': r.usage.model_dump(mode='json') if r.usage else None}))
except Exception as e:
    print(json.dumps({'error': type(e).__name__}))
    sys.exit(1)
