"""Database-backed configuration - system_settings table + provider config columns

Revision ID: 003
Revises: 002
Create Date: 2026-09-27

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic.
revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- 触发器函数（早期迁移已创建则跳过）----
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION trigger_set_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """))

    # ======== system_settings ========
    op.create_table(
        "system_settings",
        sa.Column("id", UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("key", sa.String(100), nullable=False, unique=True,
                  comment="设置键（点分命名空间）"),
        sa.Column("value", JSONB, nullable=False,
                  server_default='{"v": null}',
                  comment='设置值（统一 {"v": ...} 包装）'),
        sa.Column("description", sa.String(500), comment="设置说明"),
        sa.Column("is_secret", sa.Boolean, nullable=False, server_default="false",
                  comment="是否敏感值（true 时加密存储、读取时掩码）"),
        sa.Column("updated_by", sa.String(100), comment="最后修改人"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        comment="系统设置键值对（点分命名空间）",
    )
    op.execute(sa.text(
        "CREATE TRIGGER set_updated_at BEFORE UPDATE ON system_settings "
        "FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();"
    ))

    # ======== providers 表新增配置管理列 ========
    op.add_column(
        "providers",
        sa.Column("config_overrides", JSONB, server_default="{}",
                  comment="后台修改的配置覆盖项（DB 优先于 YAML，含加密后的 api_secret 等）"),
    )
    op.add_column(
        "providers",
        sa.Column("config_source", sa.String(10), server_default="YAML",
                  comment="配置来源：YAML / DB"),
    )


def downgrade() -> None:
    op.drop_column("providers", "config_source")
    op.drop_column("providers", "config_overrides")
    op.drop_table("system_settings")
