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

# 設定の画面（Slack 通知の出力チャンネルと Bot．admin だけ．2026-10-19）
from . import settings  # noqa

# 通知一覧（Slack_port から送った通知の記録．事務局・委員長．2026-10-19）
from . import notifications  # noqa

# Slack 通知は Slack_port 経由（2026-10-19）．読み込み時に Slack_port へ登録する
from .slack_notifier import register_to_slack_port  # noqa
register_to_slack_port()
