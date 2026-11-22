from hashlib import sha256
import json
import sqlite3

from app.problem import DeskError
from app.register.committed import load_stages, pool_ledger, run_commitments, run_releases, settle
from app.vault.record import encode, volume


def _build_document(report):
    stages = report['stages']
    return {'document':'馆际复制签发凭证','manifest':report['manifest'],
            'accounting':stages['balance'],'requests':stages['pages']['copies'],
            'revocations':stages['pages']['reversals'],'isolated':stages['intake']['exceptions'],
            'suppressions':stages['intake']['duplicates'],'resolutions':stages['licenses']['verdicts']}


def stamp(vault, run):
    report = volume(vault, run)
    document = _build_document(report)
    body = encode(document)
    imprint = sha256(body.encode()).hexdigest()
    attempt = 0
    while True:
        try:
            with vault.writing() as conn:
                existing = conn.execute('SELECT digest FROM slips WHERE id=?', (run,)).fetchone()
                if existing:
                    payload = _response(conn, run, existing[0], report)
                    payload['reused'] = True
                    return payload
                stages = load_stages(conn, run)
                commitments, releases, shortages = settle(conn, run, stages)
                if shortages:
                    raise DeskError('POOL_OVERCOMMIT', 409, {'shortages':shortages,
                                                             'pools':pool_ledger(conn)})
                conn.execute('INSERT INTO slips VALUES (?,?,?)', (run, imprint, body))
                for item in commitments:
                    conn.execute('INSERT INTO commitments VALUES (?,?,?,?)',
                                 (run, item['pool'], item['gross'], item['net']))
                for record in releases:
                    conn.execute('INSERT INTO releases VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                                 (record['run'], record['entry'], record['line'],
                                  record['received_year'], record['target_run'], record['target'],
                                  record['org'], record['year'], record['pool'],
                                  record['pages'], record['reason']))
                return _response(conn, run, imprint, report)
        except sqlite3.OperationalError as exc:
            if 'locked' in str(exc) and attempt < 4:
                attempt += 1
                continue
            raise


def _response(conn, run, imprint, report):
    releases = run_releases(conn, run)
    return {'run_id':run, 'imprint':imprint, 'reused':False,
            'trial':report['manifest']['totals'],
            'commitments':run_commitments(conn, run),
            'releases':[item for item in releases if item['reason'] is None],
            'release_errors':[item for item in releases if item['reason']],
            'pools':pool_ledger(conn)}


def certificate(vault, run):
    volume(vault, run)
    with vault.session() as conn:
        record = conn.execute('SELECT digest,body FROM slips WHERE id=?',(run,)).fetchone()
    if record is None:
        raise DeskError('DRAFT_ONLY',409)
    fingerprint, body = record
    return {'imprint':fingerprint,'verified':sha256(body.encode()).hexdigest()==fingerprint,'document':json.loads(body)}
