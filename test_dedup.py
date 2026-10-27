#!/usr/bin/env python3
from app.parsers.codex import parse_codex_session
import glob

files = sorted(glob.glob('data/codex_sessions/sessions/**/rollout-*.jsonl', recursive=True))[:5]
for f in files:
    r = parse_codex_session(f)
    msgs = r['messages']
    asst = [m for m in msgs if m['role'] == 'assistant']
    ag = sum(1 for m in asst if m['msg_type'] == 'agent_message')
    ot = sum(1 for m in asst if 'output_text' in m['msg_type'])
    fname = f.rsplit('/', 1)[-1][:40]
    print(f'{len(msgs)} msgs, {len(asst)} asst ({ag} agent, {ot} output_text) - {fname}')
