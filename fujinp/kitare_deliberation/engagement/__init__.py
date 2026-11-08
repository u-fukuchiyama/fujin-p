# SPDX-FileCopyrightText: 2024-2026 Toyoaki Nishida
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# This file is part of FUJIN-P.
# Copyright (C) 2024-2026 Toyoaki Nishida
#
# FUJIN-P is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# FUJIN-P is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with FUJIN-P.  If not, see <https://www.gnu.org/licenses/>.
#
# Source: https://github.com/u-fukuchiyama/fujin-p

"""
ext_engagement - 北近畿地域連携機構 外部連携申請審査システム
External Engagement Review Workflow

2026-10-20 からキターレオンライン審議（kitare_deliberation）の中に同梱している
（fujinp/kitare_deliberation/engagement/）．Blueprint の名前 ext_engagement と
URL /ext_engagement，テーブル，Slack_port への登録名は統合前のまま．

contract_research  / contract_project
collaborative_research / collaborative_project

の4種を統一的な枠組みで審査・記録・報告する。
"""
from flask import Blueprint

# テンプレートはキターレの templates/ext_engagement/ に置く（2026-10-20）．
# アプシャのパッケージと関所はアプリ直下の templates/ だけを運ぶため，
# engagement/templates/ には置かない．Flask は全 Blueprint のテンプレート
# 置き場を探すので，'ext_engagement/index.html' の名前のまま見つかる．
ext_engagement_bp = Blueprint(
    'ext_engagement',
    __name__,
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
