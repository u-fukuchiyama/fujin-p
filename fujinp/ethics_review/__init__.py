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
ethics_review - 人を対象とする研究倫理審査システム

連携審査システム ext_engagement をベースに、論文査読型の
申請者チャンネルと審査トラックの二系統を持つ倫理審査モジュール。
"""
from flask import Blueprint

ethics_review_bp = Blueprint(
    'ethics_review',
    __name__,
    url_prefix='/ethics_review',
    template_folder='templates',
)

from . import routes  # noqa
# データ移行（fujinpshowcase → fujinp）の書き出し・取り込み（admin専用）
from . import migration  # noqa

# 設定の画面（Slack 通知の出力チャンネルと Bot．admin だけ．2026-10-19）
from . import settings  # noqa

# 通知一覧（Slack_port から送った通知の記録．体制運営パネルから．2026-10-19）
from . import notifications  # noqa

# Slack 通知は Slack_port 経由（2026-10-19）．読み込み時に Slack_port へ登録する
from .slack_notifier import register_to_slack_port  # noqa
register_to_slack_port()
