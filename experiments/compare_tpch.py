"""Selected TPC-H-derived queries executed on PostgreSQL, not an official TPC-H run.

DuckDB's tpch extension supplies data and query text only. PostgreSQL executes
all measured queries. Two hand-defined index layouts are compared to PK-only
baselines. Run: python -m experiments.compare_tpch --scale 0.1
"""
import argparse
import csv
import hashlib
import json
import platform
import random
import re
import statistics
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter_ns
from uuid import uuid4

import duckdb
import sqlglot
from psycopg2 import sql
from sqlglot import exp

from backend.db import PROJECT_ROOT, get_connection
from backend.metadata_analyzer import get_metadata
from backend.plan_analyzer import summarize_plan
from experiments.compare_indexes import index_layout
from experiments.compare_scales import remove_owned_schema
from experiments.compare_strategies import create_indexes

TRIALS, WARMUPS, REPEATS = 3, 2, 7
TABLES = ('customer', 'orders', 'lineitem')
KEYS = {'customer': ['c_custkey'], 'orders': ['o_orderkey'],
        'lineitem': ['l_orderkey', 'l_linenumber']}
LAYOUTS = {
    'shipdate_single': [('lineitem', ['l_shipdate'])],
    'query_targeted': [('lineitem', ['l_shipdate', 'l_discount']),
                       ('orders', ['o_custkey', 'o_orderdate']),
                       ('lineitem', ['l_orderkey', 'l_shipdate'])],
}


def postgres_type(kind):
    if kind in {'INTEGER', 'BIGINT', 'SMALLINT', 'DATE'}:
        return kind
    if kind == 'VARCHAR':
        return 'TEXT'
    if re.fullmatch(r'DECIMAL\(\d+,\d+\)', kind):
        return kind
    raise ValueError(f'Unsupported generated column type: {kind}')


def query_entries(db, schema):
    entries = []
    for number, original in db.execute(
            'SELECT query_nr, query FROM tpch_queries() WHERE query_nr IN (1,3,6) ORDER BY query_nr').fetchall():
        tree = sqlglot.parse_one(original, read='duckdb')
        if not isinstance(tree, exp.Select):
            raise ValueError('Expected a SELECT query.')
        for table in tree.find_all(exp.Table):
            if table.name not in TABLES or table.catalog or table.db:
                raise ValueError('Unexpected TPC-H table reference.')
            table.set('db', exp.to_identifier(schema, quoted=True))
        entries.append({'id': f'TPCH_Q{number}', 'frequency': 1,
                        'original_duckdb_sql': original,
                        'sql': tree.sql(dialect='postgres')})
    if [q['id'] for q in entries] != ['TPCH_Q1', 'TPCH_Q3', 'TPCH_Q6']:
        raise ValueError('Required queries were not supplied by the extension.')
    return entries


def load_data(conn, db, schema):
    counts = {}
    with tempfile.TemporaryDirectory(prefix='indexmaxxer_tpch_') as temporary:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '300s'")
            cur.execute("SET LOCAL lock_timeout = '5s'")
            for table in TABLES:
                columns = db.execute(f'DESCRIBE "{table}"').fetchall()
                definitions = [sql.SQL('{} {}').format(sql.Identifier(row[0]),
                               sql.SQL(postgres_type(row[1]))) for row in columns]
                definitions.append(sql.SQL('PRIMARY KEY ({})').format(
                    sql.SQL(', ').join(sql.Identifier(k) for k in KEYS[table])))
                cur.execute(sql.SQL('CREATE TABLE {} ({}) WITH (fillfactor=100, autovacuum_enabled=false)').format(
                    sql.Identifier(schema, table), sql.SQL(', ').join(definitions)))
                path = Path(temporary) / f'{table}.csv'
                digest = hashlib.sha256()
                rows_written = 0
                # Tables and sorting keys come from constants above.
                order = ', '.join('"' + k + '"' for k in KEYS[table])
                reader = db.execute(f'SELECT * FROM "{table}" ORDER BY {order}')
                with path.open('w', encoding='utf-8', newline='') as handle:
                    writer = csv.writer(handle, lineterminator='\n')
                    while True:
                        batch = reader.fetchmany(10000)
                        if not batch:
                            break
                        if any(value is None for row in batch for value in row):
                            raise ValueError('Unexpected NULL in generated data.')
                        writer.writerows(batch)
                        rows_written += len(batch)
                with path.open('rb') as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b''):
                        digest.update(block)
                with path.open('r', encoding='utf-8', newline='') as handle:
                    cur.copy_expert(sql.SQL('COPY {} FROM STDIN WITH (FORMAT CSV)').format(
                        sql.Identifier(schema, table)).as_string(conn), handle)
                cur.execute(sql.SQL('SELECT count(*) FROM {}').format(sql.Identifier(schema, table)))
                loaded = cur.fetchone()[0]
                if loaded != rows_written or loaded == 0:
                    raise ValueError('Generated and imported row counts differ or are empty.')
                counts[table] = {'rows': loaded, 'csv_sha256': digest.hexdigest(),
                                 'columns': [{'name': r[0], 'type': r[1]} for r in columns]}
                print(f'Loaded {table}: {loaded:,} rows', flush=True)
            cur.execute(sql.SQL('ALTER TABLE {} ADD FOREIGN KEY (o_custkey) REFERENCES {} (c_custkey)').format(
                sql.Identifier(schema, 'orders'), sql.Identifier(schema, 'customer')))
            cur.execute(sql.SQL('ALTER TABLE {} ADD FOREIGN KEY (l_orderkey) REFERENCES {} (o_orderkey)').format(
                sql.Identifier(schema, 'lineitem'), sql.Identifier(schema, 'orders')))
            for table in TABLES:
                cur.execute(sql.SQL('ANALYZE {}').format(sql.Identifier(schema, table)))
    conn.commit()
    return counts


def fingerprint(rows):
    # Ignore output order but preserve all values and duplicate multiplicities.
    encoded = sorted(json.dumps(list(row), default=str, separators=(',', ':')) for row in rows)
    return hashlib.sha256('\n'.join(encoded).encode('utf-8')).hexdigest()


def measure(conn, queries, reference, seed):
    samples = {q['id']: [] for q in queries}
    rng = random.Random(seed)
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '120s'")
        for iteration in range(WARMUPS + REPEATS):
            order = list(queries)
            rng.shuffle(order)
            for query in order:
                started = perf_counter_ns()
                cur.execute(query['sql'])
                rows = cur.fetchall()
                elapsed = (perf_counter_ns() - started) / 1_000_000
                signature = (len(rows), fingerprint(rows))
                if query['id'] in reference and reference[query['id']] != signature:
                    raise RuntimeError(f"Query results changed: {query['id']}")
                reference.setdefault(query['id'], signature)
                if iteration >= WARMUPS:
                    samples[query['id']].append(elapsed)
        output = []
        for query in queries:
            cur.execute('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' + query['sql'])
            explain = cur.fetchone()[0][0]
            values = samples[query['id']]
            output.append({'id': query['id'], 'sql': query['sql'], 'frequency': 1,
                           'samples_ms': values, 'median_ms': statistics.median(values),
                           'min_ms': min(values), 'max_ms': max(values),
                           'row_count': reference[query['id']][0],
                           'result_sha256': reference[query['id']][1],
                           'explain': explain, 'plan_summary': summarize_plan(explain)})
    return {'queries': output,
            'weighted_mean_of_medians_ms': statistics.mean(q['median_ms'] for q in output)}


def configuration_trial(conn, queries, candidates, baseline_first, reference, seed):
    if baseline_first:
        baseline = measure(conn, queries, reference, seed)
    with conn.cursor() as cur:
        cur.execute('SAVEPOINT imx_tpch_indexes')
    try:
        created = create_indexes(conn, candidates)
        indexed = measure(conn, queries, reference, seed)
    finally:
        with conn.cursor() as cur:
            cur.execute('ROLLBACK TO SAVEPOINT imx_tpch_indexes')
            cur.execute('RELEASE SAVEPOINT imx_tpch_indexes')
    if not baseline_first:
        baseline = measure(conn, queries, reference, seed)
    before, after = (p['weighted_mean_of_medians_ms'] for p in (baseline, indexed))
    return {'phase_order': ['baseline', 'indexed'] if baseline_first else ['indexed', 'baseline'],
            'baseline': baseline, 'indexed': indexed,
            'improvement_pct': 100 * (before - after) / before,
            'added_indexes': created,
            'total_added_index_bytes': sum(c['index_size_bytes'] for c in created),
            'row_counts_match': True, 'result_values_match': True}


def summaries(results, configs):
    output = []
    for name, candidates in configs.items():
        trials = [r for r in results if r['configuration'] == name]
        before = [r['baseline']['weighted_mean_of_medians_ms'] for r in trials]
        after = [r['indexed']['weighted_mean_of_medians_ms'] for r in trials]
        gains = [r['improvement_pct'] for r in trials]
        sizes = [r['total_added_index_bytes'] for r in trials]
        queries = []
        names = {c['index_name'] for c in candidates}
        for qid in ('TPCH_Q1', 'TPCH_Q3', 'TPCH_Q6'):
            b = [next(q for q in r['baseline']['queries'] if q['id'] == qid) for r in trials]
            a = [next(q for q in r['indexed']['queries'] if q['id'] == qid) for r in trials]
            paired = [100 * (x['median_ms'] - y['median_ms']) / x['median_ms'] for x, y in zip(b, a)]
            queries.append({'id': qid, 'median_baseline_ms': statistics.median(q['median_ms'] for q in b),
                            'median_indexed_ms': statistics.median(q['median_ms'] for q in a),
                            'median_paired_improvement_pct': statistics.median(paired),
                            'baseline_ms_by_trial': [q['median_ms'] for q in b],
                            'indexed_ms_by_trial': [q['median_ms'] for q in a],
                            'paired_gains_pct': paired,
                            'diagnostic_trials_using_added_index': sum(any(n.get('index') in names
                                for n in q['plan_summary']['nodes']) for q in a)})
        output.append({'configuration': name, 'median_baseline_ms': statistics.median(before),
                       'median_indexed_ms': statistics.median(after),
                       'median_paired_improvement_pct': statistics.median(gains),
                       'median_added_index_bytes': statistics.median(sizes),
                       'baseline_ms_by_trial': before, 'indexed_ms_by_trial': after,
                       'improvement_pct_by_trial': gains, 'added_index_bytes_by_trial': sizes,
                       'queries': queries})
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scale', type=float, choices=(0.01, 0.1, 1.0), default=0.1)
    args = parser.parse_args()
    folder = PROJECT_ROOT / 'results'
    folder.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    output = folder / f"tpch_comparison_{now.strftime('%Y%m%dT%H%M%S%fZ')}.json"
    schema = 'imx_tpch_' + uuid4().hex
    metadata = get_metadata()
    report = {'experiment': 'selected_tpch_queries_postgresql', 'status': 'incomplete',
              'cleanup_verified': False, 'recorded_at_utc': now.isoformat(),
              'scratch_schema': schema, 'scale_factor': args.scale,
              'client': {'python': platform.python_version(), 'os': platform.platform(),
                         'duckdb': duckdb.__version__, 'sqlglot': sqlglot.__version__},
              'source_metadata_before': metadata,
              'protocol': {'trials': TRIALS, 'warmups': WARMUPS, 'repeats': REPEATS,
                 'query_weights': 'Equal weights for Q1, Q3, Q6',
                 'baseline': 'Primary-key indexes; no extra indexes',
                 'index_selection': 'Hand-defined experimental layouts; not advisor-selected',
                 'measurement': 'Client execute + fetchall; diagnostics are separate EXPLAIN runs',
                 'phase_order': 'Baseline first trials 1 and 3; indexed first trial 2',
                 'cache': 'Warmups, no forced cache clearing',
                 'data_source': 'DuckDB tpch extension dbgen; imported into PostgreSQL',
                 'query_source': 'DuckDB tpch_queries() Q1, Q3, Q6; SQLGlot PostgreSQL translation',
                 'reference': 'https://duckdb.org/docs/current/core_extensions/tpch',
                 'limitations': ['Selected TPC-H-derived queries, not an official compliant TPC-H benchmark.',
                    'DuckDB documents small differences from official TPC-H data and fixed query parameters.',
                    'Only the three required tables are loaded; supplier/part/nation foreign keys omitted.',
                    'No QphH, refresh streams, concurrency benchmark, or automatic advisor claim.',
                    'Small scale, one machine, and three trials; no significance claim.']},
              'results': []}

    def save():
        temporary = output.with_suffix('.tmp')
        temporary.write_text(json.dumps(report, indent=2), encoding='utf-8')
        temporary.replace(output)

    save()
    print(f'Results file: {output}', flush=True)
    db = duckdb.connect(':memory:')
    try:
        db.execute("SET memory_limit='1GB'")
        db.execute('SET threads=2')
        try:
            db.execute('LOAD tpch')
        except duckdb.Error:
            db.execute('INSTALL tpch')
            db.execute('LOAD tpch')
        print(f'Generating TPC-H-derived data at scale {args.scale}...', flush=True)
        db.execute(f'CALL dbgen(sf={args.scale})')
        queries = query_entries(db, schema)
        report['queries'] = queries
        configs = {name: [{'schema': schema, 'table': table, 'columns': columns,
                          'index_name': 'imx_t_' + hashlib.sha256(
                              json.dumps([table, columns]).encode()).hexdigest()[:16]}
                         for table, columns in layout] for name, layout in LAYOUTS.items()}
        report['configurations'] = configs
        with get_connection() as conn:
            created = False
            try:
                with conn.cursor() as cur:
                    cur.execute('SELECT pg_try_advisory_lock(510, 1)')
                    if not cur.fetchone()[0]:
                        raise RuntimeError('Another IndexMaxxer experiment is running.')
                    cur.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
                    created = True
                report['data'] = load_data(conn, db, schema)
                db.close()
                db = None
                with conn.cursor() as cur:
                    cur.execute("SELECT version(), current_setting('shared_buffers'), current_setting('work_mem')")
                    report['postgres_settings'] = dict(zip(('version','shared_buffers','work_mem'),cur.fetchone()))
                conn.commit()
                save()
                reference = {}
                rng = random.Random(42)
                for trial in range(1, TRIALS + 1):
                    order = list(configs)
                    rng.shuffle(order)
                    for name in order:
                        print(f'\nTrial {trial}/{TRIALS}: {name}', flush=True)
                        try:
                            with conn.cursor() as cur:
                                cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ WRITE')
                            result = configuration_trial(conn, queries, configs[name], trial % 2 == 1, reference, 42+trial)
                        finally:
                            conn.rollback()
                        result.update({'trial': trial, 'configuration': name})
                        report['results'].append(result)
                        save()
                        print(f"Baseline={result['baseline']['weighted_mean_of_medians_ms']:.3f} ms | "
                              f"Indexed={result['indexed']['weighted_mean_of_medians_ms']:.3f} ms | "
                              f"Gain={result['improvement_pct']:+.1f}%", flush=True)
                report['summary'] = summaries(report['results'], configs)
            finally:
                if created:
                    remove_owned_schema(conn, schema)
                    report['experiment_schema_removed'] = True
                save()
        report['source_metadata_after'] = get_metadata()
        report['source_index_layout_unchanged'] = index_layout(metadata) == index_layout(report['source_metadata_after'])
        report['cleanup_verified'] = report.get('experiment_schema_removed', False) and report['source_index_layout_unchanged']
        if not report['cleanup_verified']:
            raise RuntimeError('Cleanup verification failed.')
        report['status'] = 'complete'
        save()
    except BaseException as exc:
        report['status'] = 'failed'
        report['error'] = f'{type(exc).__name__}: {exc}'
        save()
        raise
    finally:
        if db is not None:
            db.close()
    print('\nSelected TPC-H query summary:')
    for row in report['summary']:
        print(f"{row['configuration']}: gain={row['median_paired_improvement_pct']:+.1f}% | "
              f"added size={row['median_added_index_bytes']/1048576:.2f} MiB")
        for q in row['queries']:
            print(f"  {q['id']}: baseline={q['median_baseline_ms']:.3f} ms | "
                  f"indexed={q['median_indexed_ms']:.3f} ms | "
                  f"paired gain={q['median_paired_improvement_pct']:+.1f}% | "
                  f"added index used={q['diagnostic_trials_using_added_index']}/{TRIALS}")
    print('Completed. Experiment schema removed; original index layout unchanged.')
    print(f'Saved to: {output}')


if __name__ == '__main__':
    main()
