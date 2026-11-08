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
mid_term_progress - 事業全貌（中期計画・年度計画の策定・報告・評価・閲覧）アプリ
"""
from flask import Blueprint

mid_term_progress_bp = Blueprint(
    'mid_term_progress',
    __name__,
    template_folder='templates',
    url_prefix='/mid_term_progress'
)

from . import routes
from . import workflow   # 編集者用・執筆者用ダッシュボード
from . import structure  # 年度計画策定（細目集の組み立てと部門の割り当て）
from . import progress   # 編集者ダッシュボード（入口）と進捗状況報告
from . import authorview # 執筆者ダッシュボード
from . import hojin      # 法人評価と計画番号ごとの評価
from . import migrate    # まるごと移行（8表を1ファイルで書き出し・取り込み）
from . import progress_import  # 進捗状況報告の取り込み（xlsx の報告内容 → progress_text）
from . import yearview   # トップページの年度ごとの表示（構成員向け・開示の設定に従う）
