import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify


def analyze_query(sql, metadata):
    statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise ValueError("Provide exactly one SELECT query.")

    tree = statements[0]
    if (
        tree.args.get("with_")
        or tree.args.get("into")
        or tree.args.get("locks")
        or tree.find(exp.Subquery) is not None
        or len(list(tree.find_all(exp.Select))) != 1
    ):
        raise ValueError(
            "CTEs, subqueries, SELECT INTO, and row locks are outside the current scope."
        )

    from_clause = tree.args.get("from_")
    joins = tree.args.get("joins", [])
    sources = ([from_clause.this] if from_clause is not None else [])
    sources += [join.this for join in joins]
    if not sources or any(
        not isinstance(source, exp.Table) or not isinstance(source.this, exp.Identifier)
        for source in sources
    ):
        raise ValueError("Use ordinary database tables in FROM and JOIN.")
    if any(join.args.get("on") is None for join in joins):
        raise ValueError("Use explicit JOIN ... ON conditions.")

    schema_name = metadata["schema"]
    table_lookup = {t["table_name"]: t for t in metadata["tables"]}
    schema = {
        schema_name: {
            name: {c["column_name"]: c["data_type"] for c in table["columns"]}
            for name, table in table_lookup.items()
        }
    }
    tree = qualify(
        tree, dialect="postgres", db=schema_name,
        schema=schema, expand_stars=False,
    )

    tables = {}
    for table in tree.find_all(exp.Table):
        if table.catalog or table.db != schema_name or table.name not in table_lookup:
            raise ValueError("Every table must exist in the selected metadata schema.")
        alias = table.alias_or_name
        if alias in tables:
            raise ValueError("Each table reference needs a unique alias.")
        tables[alias] = {
            "schema": schema_name,
            "table": table.name,
            "alias": alias,
        }

    projections = {
        item.alias: item.this
        for item in tree.expressions if isinstance(item, exp.Alias)
    }

    def collect(roots):
        found = {}
        for root in roots:
            if root is None:
                continue
            for column in root.find_all(exp.Column):
                if column.is_star:
                    continue
                if not column.table:
                    column = projections.get(column.name)
                    if not isinstance(column, exp.Column):
                        continue
                source = tables[column.table]
                key = (source["alias"], column.name)
                found[key] = {**source, "column": column.name}
        return list(found.values())

    features = {
        "where": collect([tree.args.get("where")]),
        "join": collect([j.args.get("on") for j in tree.args.get("joins", [])]),
        "order_by": collect([tree.args.get("order")]),
        "group_by": collect([tree.args.get("group")]),
    }

    # Keep real values in executable SQL; placeholders belong only in the template.
    template = tree.copy()
    for literal in list(template.find_all(exp.Literal)):
        in_filter = literal.find_ancestor(exp.Where, exp.Join, exp.Having)
        if in_filter is not None and literal.find_ancestor(exp.DataType) is None:
            target = literal.parent if isinstance(literal.parent, exp.Neg) else literal
            target.replace(exp.Placeholder())

    return {
        "sql": tree.sql(dialect="postgres", comments=False),
        "template": template.sql(dialect="postgres", comments=False),
        "tables": list(tables.values()),
        "features": features,
    }
