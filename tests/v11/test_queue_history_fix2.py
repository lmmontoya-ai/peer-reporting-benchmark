"""Correction histories verify once per operation and ignore upload ordering."""

from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest

from swarm_auth_bench.peer_reporting.storage import read_sealed, seal
from swarm_auth_bench.peer_reporting_v11 import queue_posthoc as posthoc
from swarm_auth_bench.peer_reporting_v11.queue_review import build_coding_packet, coding_selection
from swarm_auth_bench.peer_reporting_v11.queue_score import score_trial
from swarm_auth_bench.peer_reporting_v11.queue_storage import atomic_json
from tests.v11.queue_score_helpers import attempt, output, upload


def setup_records(tmp_path, monkeypatch):
    sample = attempt()
    output(sample, 'Queue totals submitted.')
    scores = [score_trial(sample)]
    selected = coding_selection(scores)
    packet = build_coding_packet(sample, packet_id='consistency-packet')
    directory = tmp_path / 'packets'
    packet_path = directory / 'reviewer' / 'consistency-packet.json'
    controller_path = directory / 'controller' / 'consistency-packet.json'
    controller = seal({'packet': packet['review_packet_id']})
    for path in (packet_path, controller_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(packet_path, seal(packet))
    atomic_json(controller_path, controller)
    bundle = {'packet': packet, 'controller': controller, 'row': selected['rows'][0]}
    context = {'index': {'part': 'owner_communications', 'source_export_hash': 'offline-consistency-records',
                        'selection': selected}, 'bundles': {packet['review_packet_id']: bundle},
               'data': {'scores': scores}}
    count = {'packet_verifications': 0}
    def checked(*args, **kwargs):
        count['packet_verifications'] += 1
        return deepcopy(context)
    monkeypatch.setattr(posthoc, 'verify_packets', checked)
    def retain(name, value, *, correction_of=None, reviewer=None):
        submitted = upload(packet, reviewer or name, value, adjudication=correction_of is not None)
        if correction_of is not None:
            submitted.update(record_conflict=True,
                             supersedes_resolution_hash=read_sealed(correction_of)['seal_hash'])
        source, record = tmp_path / (name + '-input.json'), tmp_path / (name + '.json')
        atomic_json(source, submitted)
        posthoc.retain_upload(source, packet_path, controller_path, record, correction_of=correction_of)
        return record
    initial = [retain('coder-a', False), retain('coder-b', False)]
    version = tmp_path / 'version-0.json'
    posthoc.write_adjudication(directory, version, uploads=initial)
    return directory, initial, version, retain, count


def test_correction_input_order_consistency(tmp_path, monkeypatch):
    directory, initial, previous, retain, count = setup_records(tmp_path, monkeypatch)
    correction = retain('correction-1', True, correction_of=previous)
    accepted = tmp_path / 'same-order.json'
    posthoc.write_adjudication(directory, accepted, uploads=initial, corrections=[correction])
    assert posthoc.verify_resolution(accepted)['endpoints']['assignment-1']['owner_informed']['value'] is True
    reversed_path = tmp_path / 'reversed-order.json'
    posthoc.write_adjudication(directory, reversed_path, uploads=initial[::-1], corrections=[correction])
    reversed_result = posthoc.verify_resolution(reversed_path)
    assert reversed_result['endpoints'] == posthoc.verify_resolution(accepted)['endpoints']
    retained = reversed_result['resolutions']['consistency-packet']['initial_reviews']
    assert [review['reviewer_id'] for review in retained] == ['coder-b', 'coder-a']
    assert posthoc.verify_resolution(previous)['endpoints']['assignment-1']['owner_informed']['value'] is False


def test_version_verification_checks_each_resolution_and_submission_once(tmp_path, monkeypatch):
    directory, initial, previous, retain, count = setup_records(tmp_path, monkeypatch)
    corrections, versions = [], [previous]
    for number in range(1, 9):
        corrections.append(retain('correction-' + str(number), number % 2 == 1,
                                  correction_of=previous, reviewer='independent-corrector'))
        current = tmp_path / ('version-' + str(number) + '.json')
        posthoc.write_adjudication(directory, current, uploads=initial, corrections=corrections)
        previous = current
        versions.append(current)
    checked, submissions = Counter(), Counter()
    original_record, original_upload = posthoc._resolution_record, posthoc._upload_record
    def resolution_checked(*args, **kwargs):
        checked[kwargs['inventory']['seal_hash']] += 1
        return original_record(*args, **kwargs)
    def upload_checked(*args, **kwargs):
        submissions[Path(args[3]).resolve()] += 1
        return original_upload(*args, **kwargs)
    monkeypatch.setattr(posthoc, '_resolution_record', resolution_checked)
    monkeypatch.setattr(posthoc, '_upload_record', upload_checked)
    count['packet_verifications'] = 0
    result = posthoc.verify_resolution(current)
    assert result['endpoints']['assignment-1']['owner_informed']['value'] is False
    assert checked == Counter({read_sealed(path)['resolution_version']: 1 for path in versions})
    assert submissions == Counter({path.with_name(path.stem + '-submission.json').resolve(): 1
                                   for path in [*initial, *corrections]})
    assert count['packet_verifications'] == 1
    # A later operation must audit primary submissions again, including history.
    historical = corrections[0].with_name(corrections[0].stem + '-submission.json')
    historical.write_bytes(historical.read_bytes() + b' ')
    with pytest.raises(ValueError, match='retained primary submission'):
        posthoc.verify_resolution(current)


def test_upload_identity_content_and_roles_still_matter(tmp_path, monkeypatch):
    directory, initial, previous, retain, _ = setup_records(tmp_path, monkeypatch)
    original = posthoc.verify_resolution(previous)['resolutions']['consistency-packet']
    for field, value in [('reviewer_id', 'another-coder'), ('reviewer_type', 'adjudicator'),
                         ('rationale', 'A different retained assessment.')]:
        changed = deepcopy(original)
        changed['initial_reviews'][0][field] = value
        assert not posthoc._same_episode_resolution(original, changed)
    changed = deepcopy(original)
    next(iter(changed['initial_reviews'][0]['labels'].values()))['owner_informed'] = True
    assert not posthoc._same_episode_resolution(original, changed)
