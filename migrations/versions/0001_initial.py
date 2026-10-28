"""Initial schema from docs/SPEC.md."""

import re

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

DDL = r"""CREATE TABLE groups (                        -- one row per room
  id             BIGSERIAL PRIMARY KEY,
  slug           TEXT NOT NULL UNIQUE CHECK (slug ~ '^[A-Za-z0-9_-]{8,32}$'),
  kind           TEXT NOT NULL CHECK (kind IN ('DEMO','GROUP')),
  name           TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 80),
  graph_version  BIGINT NOT NULL DEFAULT 1 CHECK (graph_version >= 1),   -- +1 per write, never reset
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_active_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at     TIMESTAMPTZ NOT NULL                                    -- set per kind by the service
);

CREATE TABLE members (
  id                BIGSERIAL PRIMARY KEY,
  group_id          BIGINT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  version           BIGINT NOT NULL,                                     -- graph_version that added it
  name              TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 40),
  color             TEXT NOT NULL CHECK (color ~ '^#[0-9A-Fa-f]{6}$'),
  net_balance_paise BIGINT NOT NULL DEFAULT 0,                           -- cache; the ledger is the truth
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (group_id, id)
);
CREATE UNIQUE INDEX members_group_name_ci ON members (group_id, lower(name));

CREATE TABLE expenses (                      -- every version of every expense; never updated
  id              BIGSERIAL PRIMARY KEY,
  group_id        BIGINT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  version         BIGINT NOT NULL,                                       -- graph_version of the write
  lineage_id      BIGINT NOT NULL,                                       -- id of the first version
  revision_of     BIGINT UNIQUE,                                         -- previous version; one successor max
  paid_by         BIGINT NOT NULL,
  acted_by        BIGINT,                                                -- self-reported, may be NULL
  title           TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 80),
  title_tsv       tsvector GENERATED ALWAYS AS (to_tsvector('simple', title)) STORED,
  spent_on        DATE NOT NULL,
  total_paise     BIGINT NOT NULL CHECK (total_paise BETWEEN 1 AND 100000000),
  split_type      TEXT NOT NULL CHECK (split_type IN ('EQUAL','SHARES','EXACT')),
  idempotency_key TEXT NOT NULL CHECK (char_length(idempotency_key) BETWEEN 16 AND 64),
  payload_hash    CHAR(64) NOT NULL,
  response_json   JSONB NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (group_id, idempotency_key),
  UNIQUE (group_id, id),
  CHECK ((revision_of IS NULL) = (lineage_id = id)),
  FOREIGN KEY (group_id, lineage_id)  REFERENCES expenses (group_id, id) DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY (group_id, revision_of) REFERENCES expenses (group_id, id) DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY (group_id, paid_by)     REFERENCES members (group_id, id)  DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY (group_id, acted_by)    REFERENCES members (group_id, id)  DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE expense_splits (
  group_id    BIGINT NOT NULL,
  expense_id  BIGINT NOT NULL,
  member_id   BIGINT NOT NULL,
  weight      INTEGER CHECK (weight IS NULL OR weight BETWEEN 1 AND 100),   -- SHARES only
  share_paise BIGINT NOT NULL CHECK (share_paise >= 0),
  PRIMARY KEY (expense_id, member_id),
  FOREIGN KEY (group_id, expense_id) REFERENCES expenses (group_id, id) ON DELETE CASCADE,
  FOREIGN KEY (group_id, member_id)  REFERENCES members (group_id, id) DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE settlements (
  id                   BIGSERIAL PRIMARY KEY,
  group_id             BIGINT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  version              BIGINT NOT NULL,
  from_member          BIGINT NOT NULL,
  to_member            BIGINT NOT NULL,
  acted_by             BIGINT,
  amount_paise         BIGINT NOT NULL CHECK (amount_paise BETWEEN 1 AND 100000000),
  mode                 TEXT NOT NULL CHECK (mode IN ('PROTECTED','NAIVE')),
  idempotency_key      TEXT NOT NULL CHECK (char_length(idempotency_key) BETWEEN 16 AND 64),
  payload_hash         CHAR(64) NOT NULL,
  graph_version_before BIGINT NOT NULL,
  response_json        JSONB NOT NULL,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (from_member <> to_member),
  UNIQUE (group_id, id),
  FOREIGN KEY (group_id, from_member) REFERENCES members (group_id, id) DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY (group_id, to_member)   REFERENCES members (group_id, id) DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY (group_id, acted_by)    REFERENCES members (group_id, id) DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE reversals (                     -- append-only: cancels one expense version or one settlement
  id              BIGSERIAL PRIMARY KEY,
  group_id        BIGINT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  version         BIGINT NOT NULL,
  expense_id      BIGINT UNIQUE,                                         -- reversed at most once
  settlement_id   BIGINT UNIQUE,
  reason          TEXT NOT NULL CHECK (reason IN ('EDIT','DELETE')),
  acted_by        BIGINT,
  idempotency_key TEXT CHECK (char_length(idempotency_key) BETWEEN 16 AND 64),
  payload_hash    CHAR(64),
  response_json   JSONB,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (num_nonnulls(expense_id, settlement_id) = 1),
  CHECK (reason = 'DELETE' OR expense_id IS NOT NULL),                   -- settlements are only deleted
  CHECK ((reason = 'EDIT') = (idempotency_key IS NULL)),                 -- an edit's key lives on its new version
  CHECK (num_nulls(idempotency_key, payload_hash, response_json) IN (0, 3)),
  UNIQUE (group_id, idempotency_key),
  FOREIGN KEY (group_id, expense_id)    REFERENCES expenses (group_id, id)    ON DELETE CASCADE,
  FOREIGN KEY (group_id, settlement_id) REFERENCES settlements (group_id, id) ON DELETE CASCADE,
  FOREIGN KEY (group_id, acted_by)      REFERENCES members (group_id, id) DEFERRABLE INITIALLY DEFERRED
);

-- Every balance, graph and list query reads these two views, never the raw tables.
CREATE VIEW active_expenses AS
  SELECT e.* FROM expenses e
  WHERE NOT EXISTS (SELECT 1 FROM reversals r WHERE r.expense_id = e.id);

CREATE VIEW active_settlements AS
  SELECT s.* FROM settlements s
  WHERE NOT EXISTS (SELECT 1 FROM reversals r WHERE r.settlement_id = s.id);

-- Exactly-once backstop for the protected path only; NAIVE rows are allowed to duplicate.
CREATE UNIQUE INDEX settlements_idem_protected ON settlements (group_id, idempotency_key) WHERE mode = 'PROTECTED';
CREATE INDEX expenses_group_version    ON expenses (group_id, version DESC, id DESC);
CREATE INDEX expenses_group_spent      ON expenses (group_id, spent_on DESC, id DESC);
CREATE INDEX expenses_group_lineage    ON expenses (group_id, lineage_id);
CREATE INDEX expense_splits_group      ON expense_splits (group_id);
CREATE INDEX settlements_group_version ON settlements (group_id, version DESC, id DESC);
CREATE INDEX reversals_group_version   ON reversals (group_id, version DESC, id DESC);
CREATE INDEX groups_kind_expires       ON groups (kind, expires_at);
"""


def upgrade() -> None:
    # DECISION: Execute the unchanged DDL statement by statement for asyncpg's single-command API.
    for statement in re.split(r"(?<=;)\n", DDL):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    op.execute("DROP VIEW active_settlements")
    op.execute("DROP VIEW active_expenses")
    for table in ("reversals", "settlements", "expense_splits", "expenses", "members", "groups"):
        op.execute(f"DROP TABLE {table}")
