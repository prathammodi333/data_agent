"""Ground string filters in the demo's schema and actual stored values."""

import re
from sqlglot import exp, parse_one


def canonical(value: str) -> str:
    return re.sub(r"[\s_-]+", "", value).casefold()


def resolve_values(value: str, column: str, examples: list[str]) -> list[str]:
    # A small business glossary supplements spelling/case normalization.
    if column == "payment_method" and canonical(value) in {"card", "cards"}:
        return ["credit_card", "debit_card"]
    exact = [candidate for candidate in examples if candidate.casefold() == value.casefold()]
    if exact:
        return exact
    matches = [candidate for candidate in examples if canonical(candidate) == canonical(value)]
    return matches or [value]


def normalize_filter_values(query: str, context: dict) -> str:
    """Normalize direct string equality/IN filters; identifiers come from DB metadata."""
    if not context:
        return query
    tree = parse_one(query, read="postgres")
    tables = {table.alias_or_name: table.name for table in tree.find_all(exp.Table)
              if table.name in context["columns"]}

    def normalize(node):
        if not isinstance(node, (exp.EQ, exp.NEQ, exp.In)):
            return node
        column = node.this
        while isinstance(column, (exp.Lower, exp.Upper)):
            column = column.this
        if not isinstance(column, exp.Column):
            return node
        candidates = [table for alias, table in tables.items()
                      if (not column.table or column.table == alias)
                      and column.name in context["columns"][table]]
        if len(set(candidates)) != 1:
            return node
        table = candidates[0]
        kind = context["columns"][table][column.name]
        if kind not in {"text", "character varying", "character", "USER-DEFINED"}:
            return node
        literals = node.expressions if isinstance(node, exp.In) else [node.expression]
        if not literals or any(not isinstance(value, exp.Literal) or not value.is_string for value in literals):
            return node
        examples = context["values"].get(f"{table}.{column.name}", [])
        exact_values, unmatched = [], []
        for literal in literals:
            resolved = resolve_values(literal.this, column.name, examples)
            if (all(value in examples for value in resolved)
                    or column.name == "payment_method" and canonical(literal.this) in {"card", "cards"}
                    or column.name in {"email", "phone", "license_plate"}):
                exact_values.extend(resolved)
            else:
                unmatched.extend(resolved)
        predicates = []
        if exact_values:
            left = exp.Lower(this=exp.Cast(this=column.copy(), to=exp.DataType.build("text")))
            predicates.append(exp.In(this=left, expressions=[exp.Literal.string(value.lower())
                              for value in dict.fromkeys(exact_values)]))
        if unmatched:
            # Spelling fallback handles categorical values missing from the sample.
            left = exp.Cast(this=column.copy(), to=exp.DataType.build("text"))
            for separator in ("_", "-", " "):
                left = exp.Replace(this=left, expression=exp.Literal.string(separator),
                                   replacement=exp.Literal.string(""))
            predicates.append(exp.In(this=exp.Lower(this=left), expressions=[exp.Literal.string(canonical(value))
                              for value in dict.fromkeys(unmatched)]))
        result = predicates[0] if len(predicates) == 1 else exp.Paren(this=exp.Or(this=predicates[0], expression=predicates[1]))
        return exp.Not(this=result) if isinstance(node, exp.NEQ) else result

    return tree.transform(normalize).sql(dialect="postgres")
