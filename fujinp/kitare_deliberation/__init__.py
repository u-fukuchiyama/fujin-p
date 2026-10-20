"""
kitare_deliberation - キターレオンライン審議
Blueprint 定義

研究倫理審査(ethics_review)を簡単化した、固定メンバーによる
オンライン審議システム。アプリ独立性原則に従い、他の Blueprint には
依存しない（user_groups.utils のみ共通基盤として利用）。

外部連携審査（受託／共同・研究／事業の申請審査．旧 ext_engagement）は
engagement/ に同梱している（2026-10-20 統合）．Blueprint は別立て（名前
ext_engagement，URL /ext_engagement）で，registry の blueprints に2本目として
載せて登録する．テーブル・Slack_port の登録名も旧来のまま．

登録例（アプリ本体の app factory 側、app.py）:
    from .kitare_deliberation import kitare_deliberation_bp
    app.register_blueprint(kitare_deliberation_bp,
                           url_prefix='/kitare_deliberation')
"""
from flask import Blueprint

kitare_deliberation_bp = Blueprint(
    'kitare_deliberation',
    __name__,
    template_folder='templates',
)

# routes をインポートしてルートを登録（循環 import 回避のため末尾で）
from . import routes  # noqa: E402,F401
# データ移行（fujinpshowcase → fujinp）の書き出し・取り込み（admin専用）
from . import migration  # noqa: E402,F401

# 設定の画面（Slack 通知の出力チャンネルと Bot．admin だけ．2026-10-19）
from . import settings  # noqa: E402,F401

# 通知一覧（Slack_port から送った通知の記録．業務会議だけ．2026-10-19）
from . import notifications  # noqa: E402,F401

# Slack 通知は Slack_port 経由（2026-10-19）．読み込み時に Slack_port へ登録する
from .slack_notifier import register_to_slack_port  # noqa: E402
register_to_slack_port()
