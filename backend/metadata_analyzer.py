from psycopg2.extras import RealDictCursor

from backend.db import get_connection


def get_metadata(schema="benchmark"):
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET LOCAL statement_timeout = '30s'")

            cur.execute("""
                SELECT c.oid, c.relname AS table_name,
                       CASE WHEN c.reltuples >= 0
                            THEN c.reltuples::bigint
                       END AS estimated_rows,
                       pg_table_size(c.oid) AS table_bytes,
                       pg_indexes_size(c.oid) AS index_bytes
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relkind = 'r'
                ORDER BY c.relname
            """, (schema,))
            tables = [dict(row) for row in cur.fetchall()]

            for table in tables:
                table_oid = table.pop("oid")

                cur.execute("""
                    SELECT a.attname AS column_name,
                           format_type(a.atttypid, a.atttypmod) AS data_type,
                           s.null_frac, s.n_distinct
                    FROM pg_attribute a
                    LEFT JOIN pg_stats s
                      ON s.schemaname = %s
                     AND s.tablename = %s
                     AND s.attname = a.attname
                     AND s.inherited = false
                    WHERE a.attrelid = %s
                      AND a.attnum > 0
                      AND NOT a.attisdropped
                    ORDER BY a.attnum
                """, (schema, table["table_name"], table_oid))
                columns = [dict(row) for row in cur.fetchall()]

                for column in columns:
                    distinct = column["n_distinct"]
                    rows = table["estimated_rows"]

                    if distinct is not None and distinct > 0:
                        estimate = round(distinct)
                    elif distinct is not None and distinct < 0 and rows is not None:
                        estimate = round(-distinct * rows)
                    else:
                        estimate = None

                    column["estimated_distinct_values"] = estimate

                table["columns"] = columns

                cur.execute("""
                    SELECT x.relname AS index_name,
                           am.amname AS method,
                           i.indisprimary AS is_primary,
                           i.indisunique AS is_unique,
                           i.indisvalid AS is_valid,
                           i.indisready AS is_ready,
                           (i.indpred IS NOT NULL) AS is_partial,
                           (i.indexprs IS NOT NULL) AS has_expressions,
                           pg_get_indexdef(i.indexrelid) AS definition,
                           ARRAY(
                               SELECT a.attname::text
                               FROM unnest(i.indkey::smallint[])
                                    WITH ORDINALITY AS k(attnum, position)
                               LEFT JOIN pg_attribute a
                                 ON a.attrelid = i.indrelid
                                AND a.attnum = k.attnum
                               WHERE k.position <= i.indnkeyatts
                               ORDER BY k.position
                           ) AS key_columns
                    FROM pg_index i
                    JOIN pg_class x ON x.oid = i.indexrelid
                    JOIN pg_am am ON am.oid = x.relam
                    WHERE i.indrelid = %s
                    ORDER BY x.relname
                """, (table_oid,))
                table["indexes"] = [dict(row) for row in cur.fetchall()]

    return {"schema": schema, "tables": tables}
