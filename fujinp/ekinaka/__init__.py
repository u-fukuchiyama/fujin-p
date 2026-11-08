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

from flask import Blueprint

ekinaka_bp = Blueprint(
    'ekinaka',
    __name__,
    url_prefix='/ekinaka',
    template_folder='templates'
)

from . import routes  # noqa: E402, F401
# データ移行（fujinpshowcase → fujinp）の書き出し・取り込み（admin専用）
from . import migration  # noqa: E402, F401
# サービスモード（通常稼働／サービス停止。admin が切り替える）
from . import service_mode  # noqa: E402, F401
