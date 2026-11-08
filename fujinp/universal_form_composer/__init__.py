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

"""ゆにこん（universal_form_composer）— こんかの部品群を「プロジェクト」として預かり，
フォーム（作品）の閲覧・書き出し，部品と SQL テーブルの対応付け，担当者への依頼を受け持つ。

v0.1 は殻：プロジェクトの一覧・閲覧・基本情報の編集・削除・エクスポート・インポート。
"""

from flask import Blueprint

unicon_bp = Blueprint(
    'unicon', __name__,
    url_prefix='/unicon',
    template_folder='templates',
)

from . import routes  # noqa: E402,F401
