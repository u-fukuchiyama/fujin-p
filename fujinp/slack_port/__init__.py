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
slack_port（Slack_port）— FUJIN-P の Slack 窓口（最初の版）

すらくみ（slack_minutes）を下敷きにした，傘下の Slack チャンネルを番号で預かるアプリ．
トークン（config.py の SLACK_BOTS）とチャンネル ID は Slack_port の内側に隠し，
他のアプリは「Bot の名前」と「チャンネルの番号」で投稿と読み取りを頼む（api.py）．

すらくみとの違い：すらくみは情報を集めるだけ，Slack_port は投稿もする．

この版の範囲：
  - Bot：config.py の SLACK_BOTS（名前 → トークン）
  - チャンネル：slack_port_channels（番号，表示名，チャンネル ID）
  - 機能：post（投稿）と read（最近のメッセージを読む）
  - 管理画面（admin）：Bot の接続確認，チャンネルの登録，参加状況の確認，試し投稿
"""
from flask import Blueprint

slack_port_bp = Blueprint(
    'slack_port',
    __name__,
    template_folder='templates',
    url_prefix='/slack_port'
)

from . import routes  # noqa: E402,F401
