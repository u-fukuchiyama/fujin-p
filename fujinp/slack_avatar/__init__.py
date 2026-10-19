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
slack_avatar（Slack_avatar）— Slack につなぎ込んだ端末（練習版）

アバターは「名前・使う Bot・チャンネルの番号」の組．端末の画面でそのチャンネルの
最近のメッセージを読み，アバターの Bot として投稿する．
Slack とのやりとりはすべて Slack_port（slack_port.api）に頼み，トークンも
チャンネル ID も持たない．えふえふ（fujin_forum）に近い使い勝手を，Slack 本体を
相手にして試すための練習版で，当面は admin だけが使う．

表：slack_avatar_avatars（アバターの登録）

Slack_port への通知の登録（2026-10-18）：読み込み時に register('slack_avatar') で名乗り，
チャンネルへの投稿は notify，DM は notify_dm で送る（Bot はアバターごとに指定するので既定の Bot は持たない）．
"""
from flask import Blueprint

slack_avatar_bp = Blueprint(
    'slack_avatar',
    __name__,
    template_folder='templates',
    url_prefix='/slack_avatar'
)

from . import routes  # noqa: E402,F401

APP_NAME = 'slack_avatar'


def _register():
    """Slack_port にこのアプリを登録する（Slack_port が無ければ何もしない）"""
    try:
        from ..slack_port import api as port
    except Exception:
        try:
            from fujinp.slack_port import api as port
        except Exception:
            return
    try:
        port.register(APP_NAME, 'Slack_avatar')
    except Exception:
        pass


_register()
