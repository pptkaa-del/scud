from shared.database.connection import resolve_min_connections


def test_explicit_arg_wins():
    assert resolve_min_connections(max_workers=20, min_connections=4, env_value="99") == 4


def test_env_used_when_arg_none():
    assert resolve_min_connections(max_workers=20, min_connections=None, env_value="7") == 7


def test_defaults_to_max_workers_when_unset():
    # The fix: a cold pool (min << max) forces psycopg2 to create connections
    # under its global lock on the hot path -> lock convoy. Warm by default.
    assert resolve_min_connections(max_workers=20, min_connections=None, env_value=None) == 20


def test_blank_env_treated_as_unset():
    assert resolve_min_connections(max_workers=5, min_connections=None, env_value="") == 5
