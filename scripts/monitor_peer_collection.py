"""Read provisional guest progress without inspecting or changing active evidence."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from guest import command

REMOTE_READ = r'''
import json, sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
root=Path(sys.argv[1])

def complete_journal_records(path):
    # This is a provisional reader, not a chain verifier. The writer can be
    # appending a final line, so only newline-terminated records are considered.
    raw=path.read_bytes()
    complete=raw[:raw.rfind(b'\n')+1]
    records=[json.loads(line) for line in complete.splitlines() if line.strip()]
    if any(type(record) is not dict for record in records):
        raise ValueError('journal record must be an object')
    if any(record.get('kind') in ('run_opened','run_closed','admission_amendment_applied')
            and type(record.get('data')) is not dict for record in records):
        raise ValueError('journal status record data must be an object')
    return records

phases={}
for split, planned in [('smoke',9),('collection',216)]:
    directory=root/('live-'+split)
    index_path=directory/'phase-index.json'
    ledger_path=directory/'budget-ledger.json'
    journal_path=directory/'journal.jsonl'
    row={'planned':planned,'status':'unstarted','counts':{},'starts':0,'archived':0,
         'current':[],'known_reported_tokens':0,'unknown_usage_attempts':0,'halt_reason':None,
         'latest_evidence_utc':None,'execution_checks_passed':0,'execution_checks_failed':0,
         'failed_execution_checks':[],'mean_completed_trial_seconds':None,
         'run_status_source':None,'latest_run_record_kind':None,'latest_run_record_sequence':None,
         'historical_halt_reason':None,'ledger_stop_reason':None,'admission_amendment':None,
         'retained_reservations':[],'retained_reservations_provisional':True,
         'unretained_unknown_usage_attempts':0}
    try:
        if index_path.exists():
            index=json.loads(index_path.read_text())
            entries=list(index.get('entries',{}).values())
            row['counts']=dict(Counter(e.get('status','unknown') for e in entries))
            row['starts']=sum(e.get('attempt') is not None for e in entries)
            row['archived']=row['counts'].get('archived',0)
            completed=[e for e in entries if e.get('status')=='archived']
            row['execution_checks_passed']=sum((e.get('attempt') or {}).get('check_passed') is True for e in completed)
            failed=[e for e in completed if (e.get('attempt') or {}).get('check_passed') is False]
            row['execution_checks_failed']=len(failed)
            row['failed_execution_checks']=[{'model':e.get('model'),'attempt_id':e.get('attempt_id'),
                'reasons':(e.get('attempt') or {}).get('failure_reasons')} for e in failed]
            durations=[e['attempt']['elapsed_seconds'] for e in completed
                if isinstance((e.get('attempt') or {}).get('elapsed_seconds'),(int,float))]
            row['mean_completed_trial_seconds']=sum(durations)/len(durations) if durations else None
            row['current']=[{'model':e.get('model'),'attempt_id':e.get('attempt_id')}
                            for e in entries if e.get('status')=='started']
            halt=(index.get('last_run') or {}).get('halted')
            row['halt_reason']=halt.get('reason') if isinstance(halt,dict) else halt
            row['historical_halt_reason']=row['halt_reason']
            row['run_status_source']='index_fallback'
            row['status']='halted' if halt else ('archived' if row['archived']==planned else
                ('in_progress' if row['starts'] else 'unstarted'))
            timestamps=[index_path.stat().st_mtime]
            unresolved=[]
            if ledger_path.exists():
                ledger=json.loads(ledger_path.read_text())
                ledger_attempts=ledger.get('attempts',{})
                attempts=list(ledger_attempts.values())
                row['known_reported_tokens']=sum(a.get('actual',0) if a.get('status')=='settled'
                    else a.get('observed',0) for a in attempts)
                row['unknown_usage_attempts']=sum(a.get('status')=='unresolved' for a in attempts)
                row['reserved_tokens']=sum(a.get('reservation',0) for a in attempts if a.get('status')!='settled')
                row['ledger_stop_reason']=ledger.get('stop_reason')
                unresolved=[{'reservation_id':key,'reserved_tokens':value.get('reservation')}
                    for key,value in ledger_attempts.items() if value.get('status')=='unresolved']
                timestamps.append(ledger_path.stat().st_mtime)
            if journal_path.exists():
                records=complete_journal_records(journal_path)
                runs=[record for record in records if record.get('kind') in ('run_opened','run_closed')]
                amendments=[record for record in records if record.get('kind')=='admission_amendment_applied']
                if amendments:
                    data=amendments[-1]['data']
                    amendment=data['amendment']
                    row['admission_amendment']={'hash':data['amendment_hash'],
                        'policy':amendment.get('policy'),'max_unresolved_trials':amendment.get('max_unresolved_trials'),
                        'verified_by_monitor':False}
                    maximum=amendment.get('max_unresolved_trials')
                    if (amendment.get('policy')=='keep_unresolved_reservations'
                            and type(maximum) is int and len(unresolved)<=maximum):
                        row['retained_reservations']=unresolved
                if runs:
                    latest=runs[-1]
                    row['run_status_source']='complete_journal_record'
                    row['latest_run_record_kind']=latest['kind']
                    row['latest_run_record_sequence']=latest.get('sequence')
                    if latest['kind']=='run_opened':
                        row['status']='in_progress'
                        row['halt_reason']=None
                    else:
                        halt=latest['data'].get('halted')
                        row['halt_reason']=halt.get('reason') if isinstance(halt,dict) else halt
                        row['status']='halted' if halt else ('archived' if row['archived']==planned else 'closed')
                timestamps.append(journal_path.stat().st_mtime)
            row['unretained_unknown_usage_attempts']=len(unresolved)-len(row['retained_reservations'])
            row['latest_evidence_utc']=datetime.fromtimestamp(max(timestamps),timezone.utc).isoformat()
    except (OSError,ValueError,TypeError,KeyError):
        row['status']='snapshot_unavailable'
        row['snapshot_error']='Could not read this provisional snapshot; retry on next refresh.'
    phases[split]=row
print(json.dumps({'kind':'provisional_status_snapshot','verified_final_evidence':False,
 'updated_utc':datetime.now(timezone.utc).isoformat(),'phases':phases}))
'''


def snapshot(project: str) -> dict:
    result = subprocess.run(command(['.venv/bin/python', '-c', REMOTE_READ, 'runs/study'], project),
                            capture_output=True, text=True, timeout=40, check=True)
    return json.loads(result.stdout)


def atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix='.run-status-', suffix='.tmp', delete=False) as stream:
        temp = Path(stream.name)
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', default='/opt/swarm-auth-bench/phases/peer-reporting-p1-collection-v1')
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1] / 'docs/long-run-explanation/run-status.json')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--once', action='store_true')
    mode.add_argument('--watch', action='store_true')
    parser.add_argument('--interval', type=float, default=45)
    parser.add_argument('--deadline', default='2026-10-05T00:00:00Z')
    args = parser.parse_args()
    deadline = datetime.fromisoformat(args.deadline.replace('Z', '+00:00'))
    if deadline.tzinfo is None or not 1 <= args.interval <= 60:
        parser.error('deadline must include a timezone; interval must be between 1 and 60 seconds')
    while True:
        try:
            value = snapshot(args.project)
            atomic_write(args.output, value)
            print(json.dumps(value), flush=True)
        except (subprocess.SubprocessError, OSError, ValueError) as error:
            # Preserve the last successful snapshot and its halt reason. Its age exposes a stale reader.
            print(json.dumps({'monitor_error':type(error).__name__, 'last_success_preserved':True}), flush=True)
            if not args.watch:
                raise SystemExit(1)
        if not args.watch or datetime.now(timezone.utc) >= deadline:
            break
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        time.sleep(max(0, min(args.interval, remaining)))


if __name__ == '__main__':
    main()
