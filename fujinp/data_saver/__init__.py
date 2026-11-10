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


# data_saver/__init__.py
# Data Saver - アプリのデータ置き場ディレクトリのバックアップ & リストア（管理者専用）
#
# 設計方針:
#   - SQL Saver のファイル版．DB ではなく，アプリがファイルを書き込むディレクトリを扱う
#   - 対象は data_saver_dirs テーブル（ID・アプリ名・ディレクトリ・保存記録）で管理する
#   - Save   : 1行のディレクトリを zip にしてダウンロード（ファイル名 <ID>_<日時>.zip）
#   - Inspect: アップロードした zip がどのID・アプリ・ディレクトリのものかを表示
#   - Restore: zip に記録された ID の行が指すディレクトリへ，上書きで展開する
#              （zip に無い既存ファイルは消さない）
#   - 対象ディレクトリは FUJIN-P のルート（既定は本アプリの親ディレクトリ）の配下に限る
#   - Blueprint 全体を admin 限定（SQL Saver と同じ before_request 方式）
#   - 対象ディレクトリは Claude に全アプリを一括で調べてもらい，その結果（JSON／テキスト）を
#     貼り付けて表と照合し，追加・訂正・削除をまとめて反映する（routes_bulk.py）

from flask import Blueprint

data_saver_bp = Blueprint(
    'data_saver',
    __name__,
    template_folder='templates',
    url_prefix='/data_saver'
)

from . import routes        # noqa: E402,F401  アクセス制御・画面・表・Save・Inspect・Restore
from . import routes_bulk   # noqa: E402,F401  Claude 向け一括調査と結果の照合・反映
