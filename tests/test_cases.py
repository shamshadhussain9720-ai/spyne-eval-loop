from evalloop.cases import load_cases, validate_all


def test_all_gold_sql_executes_and_ids_unique():
    assert validate_all(load_cases()) == []


def test_suite_shape():
    cs = load_cases()
    assert len(cs) >= 10
    assert sum(c.expect == "refuse" for c in cs) >= 2
    assert {"easy", "medium", "hard"} <= {c.difficulty for c in cs}
    assert len({c.category for c in cs}) >= 6
    assert all("critical" in c.tags for c in cs if c.expect == "refuse")
