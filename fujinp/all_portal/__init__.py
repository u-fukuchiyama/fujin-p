# SPDX-FileCopyrightText: 2026 Toyoaki Nishida
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# This file is part of FUJIN-P.
# Copyright (C) 2026 Toyoaki Nishida
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

"""オール（all_portal）：人間と Claude に同じ文書の束を渡すポータル

機関・グループ・個人・システムごとのポータル（文書の束）を持ち，サイトでの権限（閲覧・リクエスト・アクト）に
応じて人間には Web 画面で，AI には MCP（OAuth 認証つき）で，同じ中身を同じ規則で見せる．
"""

from flask import Blueprint

all_portal_bp = Blueprint('all_portal', __name__,
                          url_prefix='/all_portal',
                          template_folder='templates')

from . import oauth   # noqa: E402,F401
from . import routes  # noqa: E402,F401
from . import mcp     # noqa: E402,F401
