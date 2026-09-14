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
surakumine（すらくみね） - もう一つの Slack ワークスペース専用の議事録アプリ

すらくみ（slack_minutes）v2.3 から分岐した専用版．コードと画面は同じだが，
Slack トークン・テーブル・添付の置き場・URL をすべて別に持ち，
すらくみとは別のワークスペースだけを相手にする．

依存：
  - Config.SURAKUMINE_BOT_TOKEN（このアプリ専用．通知用の SLACK_BOT_TOKEN とは別）
  - 追加Slackスコープ：channels:read / channels:history /
                       groups:read / groups:history / users:read /
                       files:read（★v2.0 添付ファイル取得用）
  - サブモジュール mrkdwn.py（Slack mrkdwn → Markdown 復元）★v2.0

v1.0（2026-09-09）：すらくみ v2.3 から分岐．以下は分岐元での変遷．
v2.0（2026-08-27）：完全アーカイブ化．全メッセージ上書き同期・スレッド返信・
リアクション・添付ファイル実体の保存，Markdown 復元，チャンネル閲覧画面（目録）．
v2.1（2026-08-27）：閲覧の開放．admin 以外には公開アーカイブの一覧だけを表示し，
チャンネルごとの公開範囲（surakumine_channels.visibility）で閲覧を制御する．
v2.2（2026-08-27）：公開範囲をマイノート／コレポと同じ5区分（非公開・ゲストにも・
構成員だけ・グループ・構成員＋グループ）にし，まいぐるのグループを指定できるようにした．
"""
from flask import Blueprint

surakumine_bp = Blueprint(
    'surakumine',
    __name__,
    template_folder='templates',
    url_prefix='/surakumine'
)

from . import routes
