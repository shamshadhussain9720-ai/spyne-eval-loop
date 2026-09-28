from evalloop import db
from evalloop.graders import norm_rows, parse_verdict, rows_match


def test_writes_are_blocked():
    assert not db.execute("DELETE FROM Invoice").ok
    assert db.execute("SELECT COUNT(*) FROM Invoice").rows[0][0] > 0   # still intact


def test_multi_statement_rejected():
    assert not db.execute("SELECT 1; DROP TABLE Track").ok


def test_runaway_query_times_out():
    r = db.execute("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM r) SELECT COUNT(*) FROM r", timeout_s=0.3)
    assert not r.ok and "interrupt" in r.error.lower()


def n(rows):
    return norm_rows(rows)


def test_row_order_ignored_unless_ordered():
    assert rows_match(n([(1,), (2,)]), n([(2,), (1,)]), ordered=False)
    assert not rows_match(n([(1,), (2,)]), n([(2,), (1,)]), ordered=True)


def test_float_rounding_tolerance():
    assert rows_match(n([(2328.6,)]), n([(2328.6000000001,)]), ordered=False)


def test_extra_and_reordered_columns_ok_but_misaligned_rows_are_not():
    assert rows_match(n([("USA",)]), n([("USA", 13)]), ordered=False)
    assert rows_match(n([("a", 1)]), n([(1, "a")]), ordered=False)
    # same column values, wrong pairing => must fail
    assert not rows_match(n([("a", 1), ("b", 2)]), n([("a", 2), ("b", 1)]), ordered=False)


def test_wrong_row_count_or_missing_column_fails():
    assert not rows_match(n([(1,), (2,)]), n([(1,)]), ordered=False)
    assert not rows_match(n([("a", 1)]), n([("a",)]), ordered=False)


def test_judge_verdict_parsing_is_robust():
    assert parse_verdict('noise {"verdict": "Correct", "reason": "ok"} trailing')["verdict"] == "correct"
    assert parse_verdict("no json at all")["verdict"] == "invalid"
