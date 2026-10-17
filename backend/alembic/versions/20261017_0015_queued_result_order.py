"""Track Redis result order so replayed jobs cannot overwrite newer results."""

from alembic import op
import sqlalchemy as sa


revision = "20261017_0015"
down_revision = "20261017_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.add_column("credentials", sa.Column("queue_event_id", sa.String(41), nullable=True))
    op.add_column("validation_batch_items", sa.Column("queue_event_id", sa.String(41), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.drop_column("validation_batch_items", "queue_event_id")
    op.drop_column("credentials", "queue_event_id")
