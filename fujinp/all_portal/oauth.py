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

"""オール：OAuth 2.1 の認可サーバと保護リソースの目印（ふぁいんだ for Claude と同じ作り）

Claude のカスタムコネクタは，MCP の窓口が 401 を返すと WWW-Authenticate の resource_metadata から
保護リソースの目印（RFC 9728）を読み，認可サーバの目印（RFC 8414）をたどって，
動的クライアント登録（RFC 7591）→ 認可コード＋PKCE（S256）→ トークン，の順に進む．
本人の確認は FUJIN-P のログイン（セッション）で行い，許可画面で「許可する」を押すと認可コードを出す．
トークンは乱数で作り，表には SHA-256 の値だけを置く．アクセストークン1時間，リフレッシュトークン30日で，
リフレッシュのたびに取り替える（使い回されたら同じ系列をすべて失効させる）．
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

from flask import Response, abort, redirect, render_template, request, session

from config import Config
from decorators import login_required

from . import all_portal_bp as bp
from .core import DB, cursor as core_cursor, now_jst, user_active, user_info  # noqa: F401
from db import get_db_cursor

logger = logging.getLogger(__name__)

SCOPE = 'all.access'
CODE_TTL = timedelta(minutes=10)
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)
CSRF_KEY = 'all_portal_csrf_token'
DEFAULT_REDIRECTS = ('https://claude.ai/api/mcp/auth_callback',
                     'https://claude.com/api/mcp/auth_callback')

DDL = [
    "CREATE TABLE IF NOT EXISTS `all_oauth_clients` ("
    "`client_id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL, "
    "`client_name` varchar(200) COLLATE utf8mb4_unicode_ci DEFAULT NULL, "
    "`redirect_uris` text COLLATE utf8mb4_unicode_ci NOT NULL, "
    "`created_at` datetime NOT NULL, PRIMARY KEY (`client_id`)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci",
    "CREATE TABLE IF NOT EXISTS `all_oauth_codes` ("
    "`code_hash` char(64) COLLATE utf8mb4_unicode_ci NOT NULL, "
    "`client_id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL, `user_id` int NOT NULL, "
    "`redirect_uri` text COLLATE utf8mb4_unicode_ci NOT NULL, "
    "`code_challenge` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL, "
    "`scope` varchar(200) COLLATE utf8mb4_unicode_ci DEFAULT NULL, "
    "`resource` text COLLATE utf8mb4_unicode_ci, "
    "`expires_at` datetime NOT NULL, `used` tinyint NOT NULL DEFAULT 0, "
    "PRIMARY KEY (`code_hash`)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci",
    "CREATE TABLE IF NOT EXISTS `all_oauth_tokens` ("
    "`id` int NOT NULL AUTO_INCREMENT, "
    "`token_hash` char(64) COLLATE utf8mb4_unicode_ci NOT NULL, "
    "`kind` varchar(10) COLLATE utf8mb4_unicode_ci NOT NULL, "
    "`family` char(32) COLLATE utf8mb4_unicode_ci NOT NULL, "
    "`client_id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL, `user_id` int NOT NULL, "
    "`scope` varchar(200) COLLATE utf8mb4_unicode_ci DEFAULT NULL, "
    "`resource` text COLLATE utf8mb4_unicode_ci, "
    "`expires_at` datetime NOT NULL, `revoked` tinyint NOT NULL DEFAULT 0, "
    "`used` tinyint NOT NULL DEFAULT 0, "
    "`created_at` datetime NOT NULL, `last_used_at` datetime DEFAULT NULL, "
    "PRIMARY KEY (`id`), UNIQUE KEY `uq_hash` (`token_hash`), KEY `idx_family` (`family`), "
    "KEY `idx_user` (`user_id`)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci",
]
_ready = {'ok': False}


def db():
    if not _ready['ok']:
        with get_db_cursor(database=DB) as (cur, conn):
            for s in DDL:
                cur.execute(s)
            conn.commit()
        _ready['ok'] = True
    return get_db_cursor(database=DB)


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def csrf_token():
    t = session.get(CSRF_KEY)
    if not t:
        t = secrets.token_urlsafe(32)
        session[CSRF_KEY] = t
    return t


def csrf_ok():
    expected = session.get(CSRF_KEY)
    submitted = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token')
    return bool(expected and submitted and hmac.compare_digest(str(expected), str(submitted)))


# ── URL ──

def base_url():
    """PythonAnywhere は手前で https を終端するので，外向きの URL は https で組む"""
    return 'https://' + request.host


def issuer():
    return base_url() + bp.url_prefix


def mcp_url():
    return base_url() + bp.url_prefix + '/mcp'


def prm_url():
    return base_url() + '/.well-known/oauth-protected-resource' + bp.url_prefix + '/mcp'


def allowed_redirects():
    extra = getattr(Config, 'ALL_PORTAL_REDIRECT_URIS', None) or []
    if isinstance(extra, str):
        extra = [extra]
    return set(DEFAULT_REDIRECTS) | {str(x) for x in extra}


def cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type, Mcp-Protocol-Version'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    resp.headers['Access-Control-Expose-Headers'] = 'WWW-Authenticate'
    return resp


def json_resp(obj, status=200, extra_headers=None):
    r = Response(json.dumps(obj, ensure_ascii=False, default=str), status=status, mimetype='application/json')
    r.headers['Cache-Control'] = 'no-store'
    for k, v in (extra_headers or {}).items():
        r.headers[k] = v
    return cors(r)


def oauth_error(code, desc, status=400):
    return json_resp({'error': code, 'error_description': desc}, status)


# ── 目印（metadata） ──

def as_metadata():
    p = issuer()
    return {
        'issuer': p,
        'authorization_endpoint': p + '/oauth/authorize',
        'token_endpoint': p + '/oauth/token',
        'registration_endpoint': p + '/oauth/register',
        'revocation_endpoint': p + '/oauth/revoke',
        'response_types_supported': ['code'],
        'grant_types_supported': ['authorization_code', 'refresh_token'],
        'code_challenge_methods_supported': ['S256'],
        'token_endpoint_auth_methods_supported': ['none'],
        'revocation_endpoint_auth_methods_supported': ['none'],
        'scopes_supported': [SCOPE],
        'authorization_response_iss_parameter_supported': True,
        'service_documentation': base_url() + bp.url_prefix + '/connect',
    }


def prm_metadata():
    return {
        'resource': mcp_url(),
        'authorization_servers': [issuer()],
        'scopes_supported': [SCOPE],
        'bearer_methods_supported': ['header'],
        'resource_name': 'オール（FUJIN-P）',
    }


def view_as_metadata():
    if request.method == 'OPTIONS':
        return cors(Response(status=204))
    return json_resp(as_metadata())


def view_prm():
    if request.method == 'OPTIONS':
        return cors(Response(status=204))
    return json_resp(prm_metadata())


bp.add_url_rule('/.well-known/oauth-authorization-server', 'wk_as', view_as_metadata, methods=['GET', 'OPTIONS'])
bp.add_url_rule('/.well-known/openid-configuration', 'wk_oidc', view_as_metadata, methods=['GET', 'OPTIONS'])
bp.add_url_rule('/.well-known/oauth-protected-resource', 'wk_prm', view_prm, methods=['GET', 'OPTIONS'])


@bp.record_once
def _root_well_known(state):
    """サイトの根元の /.well-known/ にも目印を置く（経路挿入形）．他のアプリが置いた規則は上書きしない"""
    app = state.app
    prefix = (state.url_prefix or bp.url_prefix or '').rstrip('/')
    rules = {r.rule for r in app.url_map.iter_rules()}
    plan = [
        ('/.well-known/oauth-authorization-server' + prefix, 'all_portal_root_as', view_as_metadata),
        ('/.well-known/openid-configuration' + prefix, 'all_portal_root_oidc', view_as_metadata),
        ('/.well-known/oauth-protected-resource' + prefix + '/mcp', 'all_portal_root_prm', view_prm),
        ('/.well-known/oauth-protected-resource' + prefix, 'all_portal_root_prm2', view_prm),
    ]
    for rule, endpoint, view in plan:
        if rule not in rules and endpoint not in app.view_functions:
            app.add_url_rule(rule, endpoint, view, methods=['GET', 'OPTIONS'])


# ── 動的クライアント登録 ──

@bp.route('/oauth/register', methods=['POST', 'OPTIONS'])
def oauth_register():
    if request.method == 'OPTIONS':
        return cors(Response(status=204))
    meta = request.get_json(silent=True) or {}
    uris = meta.get('redirect_uris') or []
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
        return oauth_error('invalid_redirect_uri', 'redirect_uris が要ります')
    bad = [u for u in uris if u not in allowed_redirects()]
    if bad:
        return oauth_error('invalid_redirect_uri', '登録できない戻り先です：' + ', '.join(bad))
    client_id = 'all_' + secrets.token_urlsafe(24)
    name = str(meta.get('client_name') or 'Claude')[:200]
    with db() as (cur, conn):
        cur.execute("INSERT INTO all_oauth_clients (client_id, client_name, redirect_uris, created_at) "
                    "VALUES (%s,%s,%s,%s)", (client_id, name, json.dumps(uris), now_jst()))
        conn.commit()
    return json_resp({
        'client_id': client_id,
        'client_id_issued_at': int(datetime.now(timezone.utc).timestamp()),
        'client_name': name, 'redirect_uris': uris,
        'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code'],
        'token_endpoint_auth_method': 'none', 'scope': SCOPE,
    }, 201)


def get_client(client_id):
    if not client_id:
        return None
    with db() as (cur, conn):
        cur.execute("SELECT * FROM all_oauth_clients WHERE client_id = %s", (client_id,))
        row = cur.fetchone()
    if row:
        row['redirect_uris'] = json.loads(row['redirect_uris'] or '[]')
    return row


# ── 認可（許可画面） ──

def back_with(redirect_uri, params):
    sep = '&' if urlsplit(redirect_uri).query else '?'
    return redirect(redirect_uri + sep + urlencode(params), code=302)


def check_authorize_params(src):
    p = {k: (src.get(k) or '').strip() for k in
         ('response_type', 'client_id', 'redirect_uri', 'state', 'code_challenge',
          'code_challenge_method', 'scope', 'resource')}
    client = get_client(p['client_id'])
    if not client:
        return None, p, 'このクライアントは登録されていません．Claude のコネクタを一度外して登録し直してください．'
    if p['redirect_uri'] not in client['redirect_uris']:
        return None, p, '戻り先（redirect_uri）が登録と一致しません．'
    return client, p, ''


ADMIN_ONLY = 'Claude からオールへの接続は，いまは admin だけが許可できます．'


@bp.route('/oauth/authorize', methods=['GET'])
@login_required
def oauth_authorize():
    if session.get('user_category') != 'admin':
        return render_template('all_portal/consent.html', error=ADMIN_ONLY), 403
    client, p, err = check_authorize_params(request.args)
    if err:
        return render_template('all_portal/consent.html', error=err), 400
    if p['response_type'] != 'code':
        return back_with(p['redirect_uri'], {'error': 'unsupported_response_type', 'state': p['state']})
    if not p['code_challenge'] or p['code_challenge_method'] != 'S256':
        return back_with(p['redirect_uri'], {'error': 'invalid_request', 'state': p['state'],
                                             'error_description': 'PKCE (S256) が要ります'})
    if p['resource'] and p['resource'].rstrip('/') != mcp_url():
        return back_with(p['redirect_uri'], {'error': 'invalid_target', 'state': p['state']})
    from .core import portals_for, web_who
    return render_template('all_portal/consent.html', error='', client=client, p=p,
                           me=user_info(session.get('user_id')), portals=portals_for(web_who()))


@bp.route('/oauth/authorize', methods=['POST'])
@login_required
def oauth_authorize_post():
    if not csrf_ok():
        abort(403)
    if session.get('user_category') != 'admin':
        return render_template('all_portal/consent.html', error=ADMIN_ONLY), 403
    client, p, err = check_authorize_params(request.form)
    if err:
        return render_template('all_portal/consent.html', error=err), 400
    if request.form.get('decision') != 'allow':
        return back_with(p['redirect_uri'], {'error': 'access_denied', 'state': p['state'], 'iss': issuer()})
    if not p['code_challenge'] or p['code_challenge_method'] != 'S256':
        return back_with(p['redirect_uri'], {'error': 'invalid_request', 'state': p['state']})
    uid = int(session.get('user_id'))
    code = secrets.token_urlsafe(32)
    with db() as (cur, conn):
        cur.execute("DELETE FROM all_oauth_codes WHERE expires_at < %s", (now_jst(),))
        cur.execute("INSERT INTO all_oauth_codes (code_hash, client_id, user_id, redirect_uri, code_challenge, "
                    "scope, resource, expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (sha(code), client['client_id'], uid, p['redirect_uri'], p['code_challenge'],
                     SCOPE, p['resource'] or mcp_url(), now_jst() + CODE_TTL))
        conn.commit()
    logger.info('all_portal authorize: user=%s client=%s', uid, client['client_id'])
    return back_with(p['redirect_uri'], {'code': code, 'state': p['state'], 'iss': issuer()})


# ── トークン ──

def issue_tokens(cur, client_id, user_id, resource, family=None):
    family = family or secrets.token_hex(16)
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(40)
    t = now_jst()
    for kind, tok, ttl in (('access', access, ACCESS_TTL), ('refresh', refresh, REFRESH_TTL)):
        cur.execute("INSERT INTO all_oauth_tokens (token_hash, kind, family, client_id, user_id, scope, resource, "
                    "expires_at, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (sha(tok), kind, family, client_id, user_id, SCOPE, resource, t + ttl, t))
    return {'access_token': access, 'token_type': 'Bearer', 'expires_in': int(ACCESS_TTL.total_seconds()),
            'refresh_token': refresh, 'scope': SCOPE}


def pkce_ok(verifier, challenge):
    if not verifier or not (43 <= len(verifier) <= 128):
        return False
    calc = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    return hmac.compare_digest(calc, challenge)


@bp.route('/oauth/token', methods=['POST', 'OPTIONS'])
def oauth_token():
    if request.method == 'OPTIONS':
        return cors(Response(status=204))
    f = request.form
    grant = f.get('grant_type')
    client_id = f.get('client_id') or ''
    if not client_id and request.authorization and request.authorization.username:
        client_id = request.authorization.username
    if not get_client(client_id):
        return oauth_error('invalid_client', 'クライアントが登録されていません', 401)
    with db() as (cur, conn):
        if grant == 'authorization_code':
            code = f.get('code') or ''
            cur.execute("SELECT * FROM all_oauth_codes WHERE code_hash = %s", (sha(code),))
            row = cur.fetchone()
            if not row or row['used'] or row['expires_at'] < now_jst() or row['client_id'] != client_id:
                return oauth_error('invalid_grant', '認可コードが無効か期限切れです')
            cur.execute("UPDATE all_oauth_codes SET used = 1 WHERE code_hash = %s", (sha(code),))
            conn.commit()
            if (f.get('redirect_uri') or '') != row['redirect_uri']:
                return oauth_error('invalid_grant', 'redirect_uri が一致しません')
            if not pkce_ok(f.get('code_verifier') or '', row['code_challenge']):
                return oauth_error('invalid_grant', 'PKCE の検証に失敗しました')
            if not user_active(row['user_id']):
                return oauth_error('invalid_grant', 'この利用者は使えません')
            out = issue_tokens(cur, client_id, row['user_id'], row['resource'])
            conn.commit()
            return json_resp(out)
        if grant == 'refresh_token':
            tok = f.get('refresh_token') or ''
            cur.execute("SELECT * FROM all_oauth_tokens WHERE token_hash = %s AND kind = 'refresh'", (sha(tok),))
            row = cur.fetchone()
            if not row or row['client_id'] != client_id:
                return oauth_error('invalid_grant', 'リフレッシュトークンが無効です')
            if row['used'] or row['revoked']:
                cur.execute("UPDATE all_oauth_tokens SET revoked = 1 WHERE family = %s", (row['family'],))
                conn.commit()
                logger.warning('all_portal refresh reuse: family=%s user=%s', row['family'], row['user_id'])
                return oauth_error('invalid_grant', 'リフレッシュトークンは使用済みです')
            if row['expires_at'] < now_jst() or not user_active(row['user_id']):
                return oauth_error('invalid_grant', 'リフレッシュトークンが期限切れか，利用者が使えません')
            cur.execute("UPDATE all_oauth_tokens SET used = 1, last_used_at = %s WHERE id = %s",
                        (now_jst(), row['id']))
            cur.execute("UPDATE all_oauth_tokens SET revoked = 1 WHERE family = %s AND kind = 'access'",
                        (row['family'],))
            out = issue_tokens(cur, client_id, row['user_id'], row['resource'], row['family'])
            conn.commit()
            return json_resp(out)
    return oauth_error('unsupported_grant_type', 'authorization_code か refresh_token です')


@bp.route('/oauth/revoke', methods=['POST', 'OPTIONS'])
def oauth_revoke():
    if request.method == 'OPTIONS':
        return cors(Response(status=204))
    tok = request.form.get('token') or ''
    with db() as (cur, conn):
        cur.execute("SELECT family FROM all_oauth_tokens WHERE token_hash = %s", (sha(tok),))
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE all_oauth_tokens SET revoked = 1 WHERE family = %s", (row['family'],))
            conn.commit()
    return cors(Response(status=200))


# ── MCP の窓口で使う ──

def bearer_grant():
    """有効なアクセストークンなら {'user_id', 'client_id', 'family'} を返す．無ければ None"""
    h = request.headers.get('Authorization', '')
    if not h.lower().startswith('bearer '):
        return None
    tok = h[7:].strip()
    if not tok:
        return None
    with db() as (cur, conn):
        cur.execute("SELECT id, user_id, client_id, family, resource, expires_at, revoked FROM all_oauth_tokens "
                    "WHERE token_hash = %s AND kind = 'access'", (sha(tok),))
        row = cur.fetchone()
        if not row or row['revoked'] or row['expires_at'] < now_jst():
            return None
        if row['resource'] and row['resource'].rstrip('/') != mcp_url():
            return None
        cur.execute("UPDATE all_oauth_tokens SET last_used_at = %s WHERE id = %s", (now_jst(), row['id']))
        conn.commit()
    return row


def unauthorized(desc='認証が要ります'):
    extra = f'Bearer resource_metadata="{prm_url()}", scope="{SCOPE}"'
    if request.headers.get('Authorization'):
        extra += ', error="invalid_token", error_description="token is invalid or expired"'
    return json_resp({'error': 'unauthorized', 'error_description': desc}, 401, {'WWW-Authenticate': extra})


def connections(user_id=None):
    """有効な接続（未使用・未失効・期限内のリフレッシュトークン1本を1接続と数える）"""
    with db() as (cur, conn):
        sql = ("SELECT t.family, t.client_id, t.user_id, t.expires_at, c.client_name, "
               "(SELECT MIN(x.created_at) FROM all_oauth_tokens x WHERE x.family = t.family) AS since, "
               "(SELECT MAX(x.last_used_at) FROM all_oauth_tokens x WHERE x.family = t.family) AS last_used "
               "FROM all_oauth_tokens t LEFT JOIN all_oauth_clients c ON c.client_id = t.client_id "
               "WHERE t.kind = 'refresh' AND t.used = 0 AND t.revoked = 0 AND t.expires_at > %s")
        params = [now_jst()]
        if user_id is not None:
            sql += " AND t.user_id = %s"
            params.append(user_id)
        cur.execute(sql + " ORDER BY since DESC", params)
        rows = cur.fetchall()
    for r in rows:
        r['user'] = user_info(r['user_id'])['name']
    return rows


def revoke_family(family, user_id=None):
    with db() as (cur, conn):
        if user_id is None:
            cur.execute("UPDATE all_oauth_tokens SET revoked = 1 WHERE family = %s", (family,))
        else:
            cur.execute("UPDATE all_oauth_tokens SET revoked = 1 WHERE family = %s AND user_id = %s",
                        (family, user_id))
        n = cur.rowcount
        conn.commit()
    return n
