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
まちなか (machinaka) キャンパス - 常駐型施設運営アプリ

「えきなかキャンパス」(ekinaka) と同様のイベント管理機構を持つが、
次の点が異なる:

  * 常駐している職員が専有イベントを管理する（申請者 = apply）
  * 大学の地域連携係が職員に指示を行い、日報を点検する（承認者 = approve）
  * 日報機能を備え、開閉館時刻・訪問者数・本文を記録する
  * 点検担当は事前登録された地域連携係員のみが実施可能 (inspect 権限)
"""
from flask import Blueprint

machinaka_bp = Blueprint(
    'machinaka',
    __name__,
    template_folder='templates',
    url_prefix='/machinaka'
)

from . import routes   # noqa: E402,F401
# データ移行（fujinpshowcase → fujinp）の書き出し・取り込み（admin専用）
from . import migration  # noqa: E402,F401
# サービスモード（通常稼働／サービス停止。admin が切り替える）
from . import service_mode  # noqa: E402,F401
