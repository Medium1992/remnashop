"""The "s" edition's own migration chain renders and stays apart from upstream's."""

import io
from contextlib import redirect_stdout
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parent.parent
S_INI = ROOT / "src/infrastructure/database/alembic_s.ini"
UPSTREAM_INI = ROOT / "src/infrastructure/database/alembic.ini"


def config(ini: Path) -> Config:
    # script_location in the ini is relative to the repository root.
    cfg = Config(str(ini))
    location = cfg.get_main_option("script_location") or ""
    cfg.set_main_option("script_location", str(ROOT / location))
    return cfg


def test_the_chain_renders_to_sql_with_its_own_version_table() -> None:
    out = io.StringIO()
    with redirect_stdout(out):
        command.upgrade(config(S_INI), "head", sql=True)
    sql = out.getvalue()
    assert "CREATE TABLE alembic_version_s" in sql
    assert "CREATE TABLE s_user_blocks" in sql
    assert "REFERENCES users (id) ON DELETE CASCADE" in sql
    assert "alembic_version " not in sql.replace("alembic_version_s", "")


def test_the_chains_share_no_revision() -> None:
    ours = {r.revision for r in ScriptDirectory.from_config(config(S_INI)).walk_revisions()}
    theirs = {
        r.revision for r in ScriptDirectory.from_config(config(UPSTREAM_INI)).walk_revisions()
    }
    assert ours and theirs
    assert not ours & theirs
