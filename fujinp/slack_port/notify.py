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
slack_port.notify — 登録されたアプリの通知（チャンネルと DM）と投稿の記録（2026-10-18）

FUJIN-P の Slack 通知を Slack_port に1本化するための口．notifiers.py（notify_channel・notify_dm）と
同じ使い勝手で，宛先はチャンネル ID の代わりに Slack_port のチャンネル番号を使う．

    from ..slack_port import api as port

    port.register('my_app', 'マイアプリ', bot='practice_a')     # 読み込み時に1回（何度呼んでもよい）
    port.notify('my_app', 1, '審査が終わりました')                # チャンネル 1 番へ
    port.notify('my_app', 1, '明朝の予定', send_at=datetime(...))  # 予約投稿
    port.notify_dm('my_app', 'someone@example.ac.jp', '承認されました')

登録：register() を呼んだアプリだけが notify・notify_dm を使える（呼んでいないアプリは「未登録」）．
      登録は slack_port_apps に1行残る．何度呼んでも表示名・既定の Bot・最終確認の日時を更新するだけで，
      ログの ON/OFF は変えない．
記録：アプリのログが ON（既定は OFF．管理画面で切り替える）のときだけ，投稿1回ごとに1行を残す．
      チャンネルは slack_port_log，DM は slack_port_dm_log．失敗した投稿も理由つきで残す．
どの関数も例外を投げず，{'ok': bool, ...} を返す．記録の失敗は投稿の結果に影響しない．
DM には Bot に users:read.email と im:write のスコープが要る．
"""
import datetime
import logging
import threading

JST = datetime.timezone(datetime.timedelta(hours=9))

_REGISTRY = {}           # app_name -> {'display_name', 'bot'}（このプロセスで register されたもの）
_LOCK = threading.Lock()
_READY = {'ok': False}

_ERROR_JA_EXTRA = {
    'users_not_found': 'このメールアドレスの Slack ユーザーが見つかりません',
    'time_in_past':    '指定した発信時刻が過去です（未来の時刻を指定してください）',
    'time_too_far':    '指定した発信時刻が遠すぎます（120日以内にしてください）',
    'missing_scope':   'Bot に必要な権限（スコープ）がありません（DM には users:read.email と im:write が要ります）',
    'cannot_dm_bot':   'Bot には DM を送れません',
}

DDL = [
    "CREATE TABLE IF NOT EXISTS `slack_port_apps` ("
    "`app_name` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'アプリ名（register の第1引数）', "
    "`display_name` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '表示名', "
    "`default_bot` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '既定の Bot（SLACK_BOTS の鍵）', "
    "`log_on` tinyint NOT NULL DEFAULT 0 COMMENT '投稿を記録するか（opt in）', "
    "`registered_at` datetime NOT NULL COMMENT '最初に登録された日時', "
    "`seen_at` datetime DEFAULT NULL COMMENT '最後に register された日時', "
    "`last_used_at` datetime DEFAULT NULL COMMENT '最後に notify・notify_dm した日時', "
    "PRIMARY KEY (`app_name`)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci",
    "CREATE TABLE IF NOT EXISTS `slack_port_log` ("
    "`id` int NOT NULL AUTO_INCREMENT, "
    "`at` datetime NOT NULL COMMENT '投稿を頼まれた日時', "
    "`app_name` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL, "
    "`ch_no` int NOT NULL COMMENT 'チャンネル番号', "
    "`ch_label` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'そのときのチャンネルの表示名', "
    "`bot` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL, "
    "`text` mediumtext COLLATE utf8mb4_unicode_ci, "
    "`ok` tinyint NOT NULL DEFAULT 0, "
    "`error` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL, "
    "`ts` varchar(32) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'Slack の ts（即時投稿）', "
    "`scheduled_id` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '予約投稿の ID', "
    "`post_at` datetime DEFAULT NULL COMMENT '予約の発信時刻', "
    "`sender` varchar(200) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '発送した人（自動なら空）', "
    "PRIMARY KEY (`id`), KEY `idx_slack_port_log_app` (`app_name`,`at`)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci",
    "CREATE TABLE IF NOT EXISTS `slack_port_dm_log` ("
    "`id` int NOT NULL AUTO_INCREMENT, "
    "`at` datetime NOT NULL COMMENT '送信を頼まれた日時', "
    "`app_name` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL, "
    "`email` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '宛先のメールアドレス', "
    "`slack_user` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '宛先の Slack の表示名', "
    "`bot` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL, "
    "`text` mediumtext COLLATE utf8mb4_unicode_ci, "
    "`ok` tinyint NOT NULL DEFAULT 0, "
    "`error` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL, "
    "`ts` varchar(32) COLLATE utf8mb4_unicode_ci DEFAULT NULL, "
    "`scheduled_id` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL, "
    "`post_at` datetime DEFAULT NULL, "
    "`sender` varchar(200) COLLATE utf8mb4_unicode_ci DEFAULT NULL, "
    "PRIMARY KEY (`id`), KEY `idx_slack_port_dm_log_app` (`app_name`,`at`)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci",
]


def _api():
    from . import api
    return api


def _now():
    return datetime.datetime.now(JST).replace(tzinfo=None)


def _conn():
    """表が無ければ作ってから接続を返す"""
    conn = _api()._db()
    if not _READY['ok']:
        cur = conn.cursor()
        for s in DDL:
            cur.execute(s)
        conn.commit()
        cur.close()
        _READY['ok'] = True
    return conn


def _q(sql, params=(), fetch=None):
    """1文を実行する．fetch='one'/'all' で結果を返す"""
    conn = _conn()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(sql, params)
        out = cur.fetchone() if fetch == 'one' else cur.fetchall() if fetch == 'all' else cur.lastrowid
        conn.commit()
        cur.close()
        return out
    finally:
        conn.close()


def _err_text(e):
    api = _api()
    try:
        code = e.response.get('error', str(e))
    except Exception:
        code = str(e)
    return _ERROR_JA_EXTRA.get(code) or api._ERROR_JA.get(code) or f'Slack のエラー: {code}'


def _post_at(send_at):
    """send_at → (UNIX 秒, None)．即時は (None, None)．誤りは (None, 'エラー')．naive は JST とみなす"""
    if send_at is None:
        return None, None
    if isinstance(send_at, bool):
        return None, '発信時刻の指定が不正です'
    if isinstance(send_at, (int, float)):
        return int(send_at), None
    if isinstance(send_at, datetime.datetime):
        if send_at.tzinfo is None:
            send_at = send_at.replace(tzinfo=JST)
        return int(send_at.timestamp()), None
    return None, '発信時刻は None（即時）・datetime・UNIX 秒のいずれかで指定してください'


def _jst(post_at):
    return datetime.datetime.fromtimestamp(post_at, JST).replace(tzinfo=None) if post_at else None


# ════════════════════════════════════════════════════════════
# 登録
# ════════════════════════════════════════════════════════════

def register(app, display_name=None, bot=None):
    """アプリを Slack_port に登録する（読み込み時に呼ぶ．何度呼んでもよい）．戻り値 {'ok', 'error'}"""
    app = (app or '').strip()
    if not app or len(app) > 64:
        return {'ok': False, 'error': 'アプリ名を64文字以内で指定してください'}
    with _LOCK:
        _REGISTRY[app] = {'display_name': display_name or app, 'bot': bot or None}
    return _save_registration(app)


def _save_registration(app):
    r = _REGISTRY.get(app) or {}
    now = _now()
    try:
        _q("INSERT INTO slack_port_apps (app_name, display_name, default_bot, registered_at, seen_at) "
           "VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE display_name=VALUES(display_name), "
           "default_bot=VALUES(default_bot), seen_at=VALUES(seen_at)",
           (app, r.get('display_name'), r.get('bot'), now, now))
        return {'ok': True, 'error': None}
    except Exception as e:
        logging.warning("slack_port.register %s: %s", app, e)
        return {'ok': False, 'error': f'登録を表に書けませんでした: {e}'}


def _app_row(app):
    """登録の行（無ければ，このプロセスで register 済みなら書き直して読む）．未登録なら None"""
    if app not in _REGISTRY:
        return None
    try:
        row = _q("SELECT * FROM slack_port_apps WHERE app_name=%s", (app,), 'one')
        if not row:
            _save_registration(app)
            row = _q("SELECT * FROM slack_port_apps WHERE app_name=%s", (app,), 'one')
        return row or {'app_name': app, 'default_bot': _REGISTRY[app].get('bot'), 'log_on': 0}
    except Exception as e:
        logging.warning("slack_port app row %s: %s", app, e)
        return {'app_name': app, 'default_bot': _REGISTRY[app].get('bot'), 'log_on': 0}


def _check(app, text, bot, send_at):
    """(row, bot, post_at, None) または (None, None, None, エラー文)"""
    row = _app_row(app)
    if row is None:
        return None, None, None, f'アプリ「{app}」は Slack_port に登録されていません（register を呼んでください）'
    if not (text or '').strip():
        return None, None, None, '投稿が空です'
    bot = bot or row.get('default_bot')
    if not bot:
        return None, None, None, 'Bot が指定されていません（register の bot か，notify の bot で指定してください）'
    post_at, err = _post_at(send_at)
    if err:
        return None, None, None, err
    return row, bot, post_at, None


def _touch(app):
    try:
        _q("UPDATE slack_port_apps SET last_used_at=%s WHERE app_name=%s", (_now(), app))
    except Exception as e:
        logging.warning("slack_port touch %s: %s", app, e)


def _send(client, channel, text, post_at, thread_ts=None):
    api = _api()
    if post_at is None:
        kw = {'channel': channel, 'text': text, 'link_names': True}
        if thread_ts:
            kw['thread_ts'] = thread_ts
        r = api._call(client, 'chat_postMessage', **kw)
        return {'ok': True, 'ts': r.get('ts'), 'scheduled_message_id': None, 'error': None}
    kw = {'channel': channel, 'text': text, 'post_at': post_at, 'link_names': True}
    if thread_ts:
        kw['thread_ts'] = thread_ts
    r = api._call(client, 'chat_scheduleMessage', **kw)
    return {'ok': True, 'ts': None, 'scheduled_message_id': r.get('scheduled_message_id'),
            'post_at': r.get('post_at', post_at), 'error': None}


def _failure(e):
    api = _api()
    if isinstance(e, api.RateLimited):
        return f'Slack の回数制限に当たりました（{e.retry_after} 秒待ってください）'
    if isinstance(e, api.SlackApiError):
        return _err_text(e)
    return f'送れませんでした: {e}'


# ════════════════════════════════════════════════════════════
# チャンネルへの通知
# ════════════════════════════════════════════════════════════

def notify(app, ch_no, text, bot=None, send_at=None, thread_ts=None, sender=None):
    """登録されたアプリとして，チャンネル ch_no に投稿する（send_at があれば予約投稿）．
       戻り値 {'ok', 'ts', 'scheduled_message_id', 'error'}"""
    api = _api()
    res = {'ok': False, 'ts': None, 'scheduled_message_id': None, 'error': None}
    row, bot, post_at, err = _check(app, text, bot, send_at)
    ch = None
    if not err:
        try:
            ch = api._channel(ch_no)
        except Exception as e:
            err = f'チャンネルの表を読めません: {e}'
    if not err and (not ch or not ch.get('channel_id')):
        err = f'チャンネル {ch_no} 番は登録されていません'
    client = None
    if not err:
        try:
            client, err = api._client(bot)
        except Exception as e:
            err = f'Slack に接続できません: {e}'
    if not err:
        try:
            res = _send(client, ch['channel_id'], text, post_at, thread_ts)
        except Exception as e:
            err = _failure(e)
            logging.warning("slack_port.notify %s #%s: %s", app, ch_no, err)
    if err:
        res = {'ok': False, 'ts': None, 'scheduled_message_id': None, 'error': err}
    if row is not None:
        _touch(app)
        if row.get('log_on'):
            try:
                _q("INSERT INTO slack_port_log (at, app_name, ch_no, ch_label, bot, text, ok, error, ts, "
                   "scheduled_id, post_at, sender) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                   (_now(), app, _int(ch_no), (ch or {}).get('label'), bot, text, 1 if res['ok'] else 0,
                    (res['error'] or '')[:255] or None, res.get('ts'), res.get('scheduled_message_id'),
                    _jst(post_at), (sender or None)))
            except Exception as e:
                logging.warning("slack_port log %s: %s", app, e)
    return res


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


# ════════════════════════════════════════════════════════════
# DM
# ════════════════════════════════════════════════════════════

def notify_dm(app, email, text, bot=None, send_at=None, sender=None):
    """登録されたアプリとして，メールアドレスの人に DM を送る（send_at があれば予約）．
       戻り値 {'ok', 'ts', 'scheduled_message_id', 'slack_user', 'error'}"""
    api = _api()
    email = (email or '').strip()
    res = {'ok': False, 'ts': None, 'scheduled_message_id': None, 'slack_user': None, 'error': None}
    row, bot, post_at, err = _check(app, text, bot, send_at)
    if not err and ('@' not in email or len(email) > 255):
        err = 'メールアドレスを指定してください'
    client = None
    if not err:
        try:
            client, err = api._client(bot)
        except Exception as e:
            err = f'Slack に接続できません: {e}'
    who = None
    if not err:
        try:
            u = (api._call(client, 'users_lookupByEmail', email=email).get('user') or {})
            p = u.get('profile') or {}
            who = p.get('display_name') or p.get('real_name') or u.get('name') or u.get('id')
            dm = (api._call(client, 'conversations_open', users=u.get('id')).get('channel') or {}).get('id')
            if not dm:
                err = 'DM を開けませんでした'
            else:
                res = _send(client, dm, text, post_at)
                res['slack_user'] = who
        except Exception as e:
            err = _failure(e)
            logging.warning("slack_port.notify_dm %s: %s", app, err)
    if err:
        res = {'ok': False, 'ts': None, 'scheduled_message_id': None, 'slack_user': who, 'error': err}
    if row is not None:
        _touch(app)
        if row.get('log_on'):
            try:
                _q("INSERT INTO slack_port_dm_log (at, app_name, email, slack_user, bot, text, ok, error, ts, "
                   "scheduled_id, post_at, sender) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                   (_now(), app, email or None, who, bot, text, 1 if res['ok'] else 0,
                    (res['error'] or '')[:255] or None, res.get('ts'), res.get('scheduled_message_id'),
                    _jst(post_at), (sender or None)))
            except Exception as e:
                logging.warning("slack_port dm log %s: %s", app, e)
    return res


# ════════════════════════════════════════════════════════════
# 管理画面から使う
# ════════════════════════════════════════════════════════════

def apps():
    """登録されたアプリの一覧（記録の件数つき）"""
    rows = _q("SELECT a.*, (SELECT COUNT(*) FROM slack_port_log l WHERE l.app_name=a.app_name) AS n_log, "
              "(SELECT COUNT(*) FROM slack_port_dm_log d WHERE d.app_name=a.app_name) AS n_dm "
              "FROM slack_port_apps a ORDER BY a.app_name", fetch='all')
    for r in rows:
        r['loaded'] = r['app_name'] in _REGISTRY
    return rows


def app(app_name):
    return _q("SELECT * FROM slack_port_apps WHERE app_name=%s", (app_name,), 'one')


def set_log(app_name, on):
    """ログの ON/OFF を切り替える．戻り値は変えた行数"""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE slack_port_apps SET log_on=%s WHERE app_name=%s", (1 if on else 0, app_name))
        n = cur.rowcount
        conn.commit()
        cur.close()
        return n
    finally:
        conn.close()


def log_rows(app_name, kind='channel', limit=100, offset=0):
    """記録を新しい順に．kind は 'channel' か 'dm'．戻り値 (行, 全件数)"""
    table = 'slack_port_dm_log' if kind == 'dm' else 'slack_port_log'
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    total = _q(f"SELECT COUNT(*) AS n FROM {table} WHERE app_name=%s", (app_name,), 'one')['n']
    rows = _q(f"SELECT * FROM {table} WHERE app_name=%s ORDER BY at DESC, id DESC LIMIT %s OFFSET %s",
              (app_name, limit, offset), 'all')
    return rows, total
