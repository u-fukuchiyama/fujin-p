"""
ext_engagement - 北近畿地域連携機構 外部連携申請審査システム
External Engagement Review Workflow

contract_research  / contract_project
collaborative_research / collaborative_project

の4種を統一的な枠組みで審査・記録・報告する。
"""
from flask import Blueprint

ext_engagement_bp = Blueprint(
    'ext_engagement',
    __name__,
    template_folder='templates',
    url_prefix='/ext_engagement'
)

from . import routes
# データ移行（fujinpshowcase → fujinp）の書き出し・取り込み（admin専用）
from . import migration  # noqa
