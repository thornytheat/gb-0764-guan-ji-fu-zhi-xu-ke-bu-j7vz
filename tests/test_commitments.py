"""Trial vs. committed pages, shared yearly-pool settlement and cross-volume releases."""
from concurrent.futures import ThreadPoolExecutor

HEADER = 'entry,action,received,institution,issn,pages,target\n'


def deposit(client, text):
    return '/api/volumes/' + client.post('/api/volumes', json={'csv': text}).json['run_id']


def north(ledger):
    return next(pool for pool in ledger['pools'] if pool['pool'] == '2026/NORTH')


def test_deposit_is_trial_only_and_books_no_pool(client):
    address = deposit(client, HEADER + 'A1,copy,2026-11-06,00123,2049-3630,3-10,\n')
    assert client.get('/api/ledger').json == {'pools': []}
    assert client.get(address + '/export').status_code == 409


def test_stamp_converts_trial_into_committed_pages(client):
    address = deposit(client, HEADER + 'A1,copy,2026-11-06,00123,2049-3630,3-10,\n')
    result = client.post(address + '/stamp').json
    assert result['trial']['effective'] == 8
    assert result['commitments'] == [{'pool': '2026/NORTH', 'gross': 8, 'net': 8}]
    pool = north(client.get('/api/ledger').json)
    assert (pool['limit'], pool['committed'], pool['available']) == (10, 8, 2)


def test_second_volume_over_pool_is_rejected_and_rolls_back(client):
    first = deposit(client, HEADER + 'A1,copy,2026-11-06,00123,2049-3630,3-10,\n')
    client.post(first + '/stamp')
    second = deposit(client, HEADER + 'B1,copy,2026-11-06,00123,2049-3630,3-6,\n')
    reply = client.post(second + '/stamp')
    assert reply.status_code == 409 and reply.json['error'] == 'POOL_OVERCOMMIT'
    shortage = reply.json['details']['shortages'][0]
    assert (shortage['pool'], shortage['available'], shortage['requested']) == ('2026/NORTH', 2, 4)
    # nothing booked for the rejected run, first commitment untouched
    pool = north(client.get('/api/ledger').json)
    assert (pool['committed'], pool['available']) == (8, 2)
    assert client.get(second + '/export').status_code == 409


def test_rejected_stamp_can_retry_without_double_booking(client):
    first = deposit(client, HEADER + 'A1,copy,2026-11-06,00123,2049-3630,3-10,\n')
    client.post(first + '/stamp')
    blocked = deposit(client, HEADER + 'B1,copy,2026-11-06,00123,2049-3630,3-6,\n')
    for _ in range(2):
        reply = client.post(blocked + '/stamp')
        assert reply.status_code == 409
    # a smaller order that fits the remainder commits cleanly
    fitting = deposit(client, HEADER + 'D1,copy,2026-11-06,00123,2049-3630,3-4,\n')
    assert client.post(fitting + '/stamp').status_code == 200
    assert north(client.get('/api/ledger').json)['committed'] == 10


def test_signed_stamp_is_idempotent_and_keeps_imprint(client):
    address = deposit(client, HEADER + 'A1,copy,2026-11-06,00123,2049-3630,3-10,\n')
    first = client.post(address + '/stamp').json
    repeat = client.post(address + '/stamp').json
    assert repeat == dict(first, reused=True)
    assert north(client.get('/api/ledger').json)['committed'] == 8


def test_concurrent_stamps_never_oversubscribe_one_year_pool(client):
    runs = [deposit(client, HEADER + f'C{i},copy,2026-11-06,00123,2049-3630,3-8,\n')
            for i in range(4)]
    with ThreadPoolExecutor(8) as pool:
        statuses = list(pool.map(lambda run: client.post(run + '/stamp').status_code, runs))
    assert sorted(statuses) == [200, 409, 409, 409]
    pool_row = north(client.get('/api/ledger').json)
    assert (pool_row['committed'], pool_row['available']) == (6, 4)


def test_cross_volume_cancel_returns_original_signed_pages(client):
    first = deposit(client, HEADER + 'A1,copy,2026-11-06,00123,2049-3630,3-12,\n')
    client.post(first + '/stamp')  # commits 10, pool full
    second = deposit(client, HEADER + 'B1,copy,2026-11-06,00123,2049-3630,3-6,\n'
                                    'X1,cancel,2026-11-07,00123,,,A1\n')
    result = client.post(second + '/stamp').json
    release = result['releases'][0]
    assert (release['target'], release['pages'], release['pool']) == ('A1', 10, '2026/NORTH')
    pool = north(client.get('/api/ledger').json)
    assert (pool['committed_gross'], pool['released'], pool['committed'], pool['available']) == (14, 10, 4, 6)


def test_cross_volume_cancel_retry_does_not_release_twice(client):
    first = deposit(client, HEADER + 'A1,copy,2026-11-06,00123,2049-3630,3-12,\n')
    client.post(first + '/stamp')
    second = deposit(client, HEADER + 'X1,cancel,2026-11-07,00123,,,A1\n')
    client.post(second + '/stamp')
    again = client.post(second + '/stamp').json
    assert again['reused'] and len(again['releases']) == 1
    assert north(client.get('/api/ledger').json)['released'] == 10


def test_cross_volume_cancel_releases_reused_by_later_volume(client):
    first = deposit(client, HEADER + 'A1,copy,2026-11-06,00123,2049-3630,3-12,\n')
    client.post(first + '/stamp')  # 10 committed, full
    cancel_run = deposit(client, HEADER + 'X1,cancel,2026-11-07,00123,,,A1\n')
    client.post(cancel_run + '/stamp')  # 10 released
    later = deposit(client, HEADER + 'M1,copy,2026-11-08,00123,2049-3630,3-9,\n')  # 7 pages
    assert client.post(later + '/stamp').status_code == 200
    pool = north(client.get('/api/ledger').json)
    assert (pool['committed'], pool['available']) == (7, 3)


def test_cross_volume_cancel_guards_org_year_and_unknown_target(client):
    first = deposit(client, HEADER + 'A1,copy,2026-11-06,00123,2049-3630,3-12,\n')
    client.post(first + '/stamp')
    cases = {
        'Y1,cancel,2026-11-07,00123,,,GHOST\n': 'UNKNOWN_TARGET',
        'Y2,cancel,2026-11-07,00007,,,A1\n': 'WRONG_ORG',
        'Y3,cancel,2027-01-02,00123,,,A1\n': 'BOUNDARY',
    }
    for body, reason in cases.items():
        run = deposit(client, HEADER + body)
        result = client.post(run + '/stamp').json
        assert result['release_errors'][0]['reason'] == reason
        assert result['releases'] == []
    assert north(client.get('/api/ledger').json)['released'] == 0


def test_target_can_only_be_released_once_across_volumes(client):
    first = deposit(client, HEADER + 'A1,copy,2026-11-06,00123,2049-3630,3-12,\n')
    client.post(first + '/stamp')
    client.post(deposit(client, HEADER + 'Z1,cancel,2026-11-07,00123,,,A1\n') + '/stamp')
    duplicate = client.post(deposit(client, HEADER + 'Z2,cancel,2026-11-08,00123,,,A1\n') + '/stamp').json
    assert duplicate['release_errors'][0]['reason'] == 'REPEATED_CANCEL'
    assert north(client.get('/api/ledger').json)['released'] == 10


def test_original_certificate_remains_verifiable_after_release(client):
    from hashlib import sha256
    from app.vault.record import encode
    first = deposit(client, HEADER + 'A1,copy,2026-11-06,00123,2049-3630,3-12,\n')
    signature = client.post(first + '/stamp').json['imprint']
    client.post(deposit(client, HEADER + 'X1,cancel,2026-11-07,00123,,,A1\n') + '/stamp')
    doc = client.get(first + '/export').json
    assert doc['imprint'] == signature and doc['verified']
    assert sha256(encode(doc['document']).encode()).hexdigest() == signature
    assert doc['document']['requests'][0]['licensed'] == 10


def test_commitment_endpoint_reports_run_ledger(client):
    address = deposit(client, HEADER + 'A1,copy,2026-11-06,00123,2049-3630,3-10,\n')
    client.post(address + '/stamp')
    report = client.get(address + '/commitments').json
    assert report['commitments'] == [{'pool': '2026/NORTH', 'gross': 8, 'net': 8}]


def test_revised_resubmission_cannot_double_book_signed_pool(client):
    from app.source import bundle
    source = bundle('clean').decode()
    original = deposit(client, source)
    assert client.post(original + '/stamp').status_code == 200
    amended = deposit(client, source + 'X1,cancel,2026-12-02,00007,,,C05\n')
    # The revision restates the same orders; letting it commit would occupy the
    # pools twice, so the shared yearly pool rejects it while the old slip stands.
    reply = client.post(amended + '/stamp')
    assert reply.status_code == 409 and reply.json['error'] == 'POOL_OVERCOMMIT'
    assert client.get(original + '/export').json['verified']


def test_stamp_folio_distinguishes_trial_and_committed(client):
    signed = deposit(client, HEADER + 'A1,copy,2026-11-06,00123,2049-3630,3-10,\n')
    client.post(signed + '/stamp')
    deposit(client, HEADER + 'Q1,copy,2026-11-06,00123,2049-3630,3-4,\n')
    page = client.get('/folio/stamp').get_data(as_text=True)
    assert '试算与已承诺页数' in page and '年度池台账' in page
    assert '已承诺·可查验' in page and '仅试算·不占池' in page
