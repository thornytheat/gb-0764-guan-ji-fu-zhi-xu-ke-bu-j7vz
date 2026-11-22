import json

from app.problem import DeskError


def load_stages(conn, run):
    item = conn.execute('SELECT evidence FROM volumes WHERE id=?', (run,)).fetchone()
    if item is None:
        raise DeskError('RUN_UNKNOWN', 404)
    return json.loads(item[0])


def _signed_copies_elsewhere(conn, run):
    """Map entry -> copy row for every order signed inside other imprinted volumes."""
    remote = {}
    rows = conn.execute('SELECT v.id, v.evidence FROM volumes v JOIN slips s ON s.id=v.id '
                        'WHERE v.id != ? ORDER BY v.rowid', (run,)).fetchall()
    for run_id, evidence in rows:
        stages = json.loads(evidence)
        for copy in stages['pages']['copies']:
            remote.setdefault(copy['entry'], {'run': run_id, 'copy': copy})
    return remote


def _cancel_catalog(stages):
    """Map cancel entry -> its own org code and received year from the verdicts."""
    catalog = {}
    for verdict in stages['licenses']['verdicts']:
        if verdict['action'] == 'cancel':
            catalog[verdict['entry']] = {'org': verdict['org']['code'] if verdict.get('org') else None,
                                         'year': verdict['received'][:4]}
    return catalog


def _external_reversals(conn, run, stages):
    """Adjudicate cancellations whose target lives in another already-imprinted volume.

    Reversals resolved inside this run are already netted into the snapshot
    accounts; only cross-volume targets reach here. A valid release hands back
    exactly the pages the original certificate signed, never the trial figure.
    """
    # The trial marks every target absent from this volume as UNKNOWN_TARGET
    # and credits nothing. At imprint we re-open those reversals and look for
    # the original copy inside already-signed sibling volumes.
    pending = [rev for rev in stages['pages']['reversals'] if rev['reason'] == 'UNKNOWN_TARGET']
    remote = _signed_copies_elsewhere(conn, run) if pending else {}
    catalog = _cancel_catalog(stages)
    records = []
    for rev in pending:
        target = rev['target']
        origin = remote.get(target)
        info = catalog.get(rev['entry'], {})
        reason = None
        pool = org = year = None
        pages = 0
        target_run = origin['run'] if origin else None
        if origin is None:
            reason = 'UNKNOWN_TARGET'
        if not reason:
            reference = origin['copy']
            pool = reference['pool']
            org = reference['org']
            year = pool.split('/', 1)[0]
            if info.get('org') != org:
                reason = 'WRONG_ORG'
            elif info.get('year') != year:
                reason = 'BOUNDARY'
            elif conn.execute('SELECT 1 FROM releases WHERE target=? AND reason IS NULL',
                              (target,)).fetchone():
                reason = 'REPEATED_CANCEL'
        if not reason:
            pages = origin['copy']['licensed']
        records.append({'run': run, 'entry': rev['entry'], 'line': rev['line'],
                        'received_year': info.get('year'), 'target_run': target_run,
                        'target': target, 'org': org, 'year': year, 'pool': pool,
                        'pages': pages, 'reason': reason})
    return records


def settle(conn, run, stages):
    """Compute net commitments per pool and validate against the shared yearly pools."""
    releases = _external_reversals(conn, run, stages)
    restored = {}
    for record in releases:
        if record['reason'] is None:
            restored[record['pool']] = restored.get(record['pool'], 0) + record['pages']
    trial = stages['pages']['accounts']
    commitments, shortages = [], []
    for pool, account in trial.items():
        gross, net = account['gross'], account['used']
        committed = conn.execute('SELECT COALESCE(SUM(net),0) FROM commitments WHERE pool=?',
                                 (pool,)).fetchone()[0]
        released = conn.execute('SELECT COALESCE(SUM(pages),0) FROM releases '
                                'WHERE pool=? AND reason IS NULL', (pool,)).fetchone()[0]
        credit = restored.get(pool, 0)
        occupied = committed - released - credit
        available = account['limit'] - occupied
        if net > available:
            shortages.append({'pool': pool, 'limit': account['limit'], 'occupied': occupied,
                              'available': available, 'requested': net})
        commitments.append({'pool': pool, 'limit': account['limit'], 'gross': gross, 'net': net,
                            'restored': credit})
    return commitments, releases, shortages


def pool_ledger(conn):
    """Global view of every yearly pool that has a commitment or a release.

    Pure trial volumes touch no pool and therefore never appear here; their
    limits are read from the evidence of the volumes that actually booked.
    """
    active_pools = {row[0] for row in conn.execute('SELECT DISTINCT pool FROM commitments')}
    active_pools.update(row[0] for row in conn.execute(
        'SELECT DISTINCT pool FROM releases WHERE reason IS NULL'))
    limits = {}
    for row in conn.execute('SELECT evidence FROM volumes'):
        for pool, account in json.loads(row[0])['pages']['accounts'].items():
            if pool in active_pools:
                limits[pool] = account['limit']
    committed = dict(conn.execute('SELECT pool, SUM(net) FROM commitments GROUP BY pool').fetchall())
    gross = dict(conn.execute('SELECT pool, SUM(gross) FROM commitments GROUP BY pool').fetchall())
    released = dict(conn.execute('SELECT pool, SUM(pages) FROM releases WHERE reason IS NULL '
                                 'GROUP BY pool').fetchall())
    ledger = []
    for pool in sorted(active_pools):
        used = committed.get(pool, 0) - released.get(pool, 0)
        ledger.append({'pool': pool, 'limit': limits[pool], 'committed_gross': gross.get(pool, 0),
                       'released': released.get(pool, 0), 'committed': used,
                       'available': limits[pool] - used})
    return ledger


def run_commitments(conn, run):
    return [dict(zip(('pool', 'gross', 'net'), row)) for row in conn.execute(
        'SELECT pool, gross, net FROM commitments WHERE run=? ORDER BY pool', (run,)).fetchall()]


def run_releases(conn, run):
    cols = ('entry', 'line', 'target_run', 'target', 'pool', 'pages', 'reason')
    return [dict(zip(cols, row)) for row in conn.execute(
        'SELECT entry, line, target_run, target, pool, pages, reason FROM releases '
        'WHERE run=? ORDER BY line', (run,)).fetchall()]
