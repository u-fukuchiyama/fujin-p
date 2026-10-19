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
slack_port.api — 他のアプリが Slack_port を使うための口

他のアプリはトークンもチャンネル ID も持たず，Bot の名前とチャンネルの番号で頼む．

    from ..slack_port import api as port

    port.bots()                          # 使える Bot の一覧
    port.channels()                      # チャンネルの一覧（番号と表示名）
    port.post('practice_a', 1, 'こんにちは')
    port.read('practice_a', 1, limit=30)
    port.history_page('practice_a', 1, cursor=None)   # 全履歴はページ送りで
    port.replies('practice_a', 1, thread_ts)          # スレッドの返信
    port.period_messages('practice_a', 1, oldest, latest)  # 期間を全部（サーバ側でページ送り）

通知（2026-10-18．notify.py）：アプリは register で名乗ってから notify・notify_dm を使う．
    port.register('my_app', 'マイアプリ', bot='practice_a')
    port.notify('my_app', 1, '審査が終わりました')             # チャンネル番号へ（send_at で予約）
    port.notify_dm('my_app', 'someone@example.ac.jp', '承認されました')

Bot（トークン）は config.py の SLACK_BOTS に置く（DB には置かない）：
    SLACK_BOTS = {'practice_a': 'xoxb-…', 'practice_b': 'xoxb-…'}
チャンネルは Slack_port の表（slack_port_channels）に番号で登録する．

どの関数も例外を投げず，{'ok': bool, ...} を返す．
Slack との通信の部品（RateLimited の扱い，ユーザー名の解決）と mrkdwn の変換は
すらくみ（slack_minutes）から引き継いでいる．
"""
import datetime
import html as _html
import logging
import time

try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    _SLACK_SDK_OK = True
except ImportError:                                     # pragma: no cover
    _SLACK_SDK_OK = False
    WebClient = None

    class SlackApiError(Exception):
        pass

try:
    from markdown_converter import process_markdown as _site_md
except Exception:
    _site_md = None
try:
    import markdown as _md_lib
except Exception:
    _md_lib = None

from .mrkdwn import mrkdwn_to_md

JST = datetime.timezone(datetime.timedelta(hours=9))

_ERROR_JA = {
    'channel_not_found': 'チャンネルが見つかりません（チャンネル ID を確かめるか，Bot をチャンネルに招待してください）',
    'not_in_channel':    'Bot がチャンネルに参加していません（/invite で招待してください）',
    'is_archived':       'チャンネルはアーカイブ済みです',
    'invalid_auth':      'トークンが無効です',
    'token_revoked':     'トークンが失効しています',
    'missing_scope':     'Bot に必要な権限（スコープ）がありません',
    'account_inactive':  'Bot が無効になっています',
    'ratelimited':       'Slack の回数制限に当たりました．少し待ってから試してください',
    'msg_too_long':      '投稿が長すぎます',
    'no_text':           '投稿が空です',
}


# ════════════════════════════════════════════════════════════
# 設定と DB
# ════════════════════════════════════════════════════════════

def _config():
    try:
        from config import Config
        return Config
    except Exception as e:
        logging.error("slack_port: config 読み込み失敗: %s", e)
        return None


def _bot_tokens():
    cfg = _config()
    bots = getattr(cfg, 'SLACK_BOTS', None) if cfg else None
    return dict(bots) if isinstance(bots, dict) else {}


def _db():
    import mysql.connector
    from db import DatabaseConfig
    return mysql.connector.connect(**DatabaseConfig.default())


def _err(e):
    try:
        code = e.response.get('error', str(e))
    except Exception:
        code = str(e)
    return _ERROR_JA.get(code, f'Slack のエラー: {code}')


def _client(bot):
    """(client, None) または (None, エラー文)"""
    if not _SLACK_SDK_OK:
        return None, 'slack_sdk が入っていません'
    token = _bot_tokens().get(bot)
    if not token:
        return None, f'Bot「{bot}」は config.py の SLACK_BOTS にありません'
    return WebClient(token=token), None


class RateLimited(Exception):
    """Slack の ratelimited 応答（Retry-After 秒を保持）— すらくみから"""
    def __init__(self, retry_after):
        super().__init__(f'ratelimited ({retry_after}s)')
        self.retry_after = retry_after


def _call(client, method, **kw):
    """Slack API 呼び出し．ratelimited は1回だけ待って再試行する"""
    for attempt in (1, 2):
        try:
            return getattr(client, method)(**kw)
        except SlackApiError as e:
            if e.response.get('error') == 'ratelimited' and attempt == 1:
                retry = int(e.response.headers.get('Retry-After', 5))
                if retry <= 5:
                    time.sleep(retry)
                    continue
                raise RateLimited(retry)
            raise


# ════════════════════════════════════════════════════════════
# Bot とチャンネル
# ════════════════════════════════════════════════════════════

_bot_cache = {}          # bot -> (時刻, 情報)
_BOT_TTL = 600


def bot_info(bot, refresh=False):
    """auth.test の結果（Bot の表示名，ワークスペース）．キャッシュ10分"""
    hit = _bot_cache.get(bot)
    if hit and not refresh and time.time() - hit[0] < _BOT_TTL:
        return hit[1]
    info = {'key': bot, 'ok': False, 'name': '', 'user_id': '', 'team': '', 'error': None}
    client, err = _client(bot)
    if client is None:
        info['error'] = err
    else:
        try:
            r = _call(client, 'auth_test')
            info.update(ok=True, name=r.get('user', ''), user_id=r.get('user_id', ''),
                        bot_id=r.get('bot_id', ''), team=r.get('team', ''),
                        team_id=r.get('team_id', ''))
        except Exception as e:
            info['error'] = _err(e) if isinstance(e, SlackApiError) else str(e)
    _bot_cache[bot] = (time.time(), info)
    return info


def bots(refresh=False):
    """config の SLACK_BOTS にある Bot の一覧（トークンは返さない）"""
    return [bot_info(b, refresh) for b in _bot_tokens()]


def _channel_rows():
    conn = _db()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM slack_port_channels ORDER BY ch_no")
        rows = cur.fetchall()
        cur.close()
        return rows
    finally:
        conn.close()


def channels():
    """チャンネルの一覧．他のアプリには番号と表示名だけを渡す"""
    try:
        return [{'no': r['ch_no'], 'label': r['label']} for r in _channel_rows()]
    except Exception as e:
        logging.error("slack_port.channels: %s", e)
        return []


def _channel(ch_no):
    for r in _channel_rows():
        if r['ch_no'] == int(ch_no):
            return r
    return None


# ════════════════════════════════════════════════════════════
# 投稿と読み取り
# ════════════════════════════════════════════════════════════

def post(bot, ch_no, text, thread_ts=None):
    """Bot としてチャンネル ch_no に投稿する．
       戻り値 {'ok', 'ts', 'error'}"""
    if not (text or '').strip():
        return {'ok': False, 'error': '投稿が空です'}
    try:
        ch = _channel(ch_no)
    except Exception as e:
        return {'ok': False, 'error': f'チャンネルの表を読めません: {e}'}
    if not ch or not ch.get('channel_id'):
        return {'ok': False, 'error': f'チャンネル {ch_no} 番は登録されていません'}
    client, err = _client(bot)
    if client is None:
        return {'ok': False, 'error': err}
    try:
        kw = {'channel': ch['channel_id'], 'text': text}
        if thread_ts:
            kw['thread_ts'] = thread_ts
        r = _call(client, 'chat_postMessage', **kw)
        return {'ok': True, 'ts': r.get('ts'), 'error': None}
    except RateLimited as e:
        return {'ok': False, 'error': f'Slack の回数制限に当たりました（{e.retry_after} 秒待ってください）'}
    except SlackApiError as e:
        logging.warning("slack_port.post %s #%s: %s", bot, ch_no, e)
        return {'ok': False, 'error': _err(e)}
    except Exception as e:
        logging.error("slack_port.post: %s", e)
        return {'ok': False, 'error': f'投稿に失敗しました: {e}'}


_user_cache = {}         # (bot, user_id) -> 表示名


def _user_name(client, bot, user_id):
    if not user_id:
        return ''
    key = (bot, user_id)
    if key in _user_cache:
        return _user_cache[key]
    name = user_id
    try:
        u = (_call(client, 'users_info', user=user_id).get('user') or {})
        p = u.get('profile') or {}
        name = p.get('display_name') or p.get('real_name') or u.get('real_name') \
            or u.get('name') or user_id
    except Exception:
        pass
    _user_cache[key] = name
    return name


def _md_to_html(md):
    """Markdown → HTML．Slack 由来の < > は先にエスケープして，生の HTML を通さない"""
    safe = md.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    if _site_md is not None:
        try:
            out = _site_md(safe)
            return out[0] if isinstance(out, tuple) else out
        except Exception:
            pass
    if _md_lib is not None:
        try:
            return _md_lib.markdown(safe, extensions=['fenced_code', 'nl2br'])
        except Exception:
            pass
    return '<p>' + safe.replace('\n', '<br>') + '</p>'


_SKIP_SUBTYPES = ('channel_join', 'channel_leave', 'group_join', 'group_leave')


def _format(client, bot, me, m):
    """Slack のメッセージ1件を，画面とダウンロードで使う形に直す"""
    resolve = lambda uid: _user_name(client, bot, uid)
    uid = m.get('user') or ''
    is_bot = bool(m.get('bot_id'))
    if is_bot:
        name = (m.get('bot_profile') or {}).get('name') or m.get('username') or resolve(uid)
    else:
        name = resolve(uid)
    text = m.get('text') or ''
    try:
        md = mrkdwn_to_md(text, resolve_user=resolve)
    except Exception:
        md = _html.unescape(text)
    ts = m.get('ts', '0')
    dt = datetime.datetime.fromtimestamp(float(ts), JST)
    files = [{'name': f.get('name') or f.get('title') or '(ファイル)',
              'url': f.get('permalink') or f.get('url_private') or ''}
             for f in (m.get('files') or [])]
    return {
        'ts': ts,
        'time': dt.strftime('%m/%d %H:%M'),
        'datetime': dt.strftime('%Y-%m-%d %H:%M'),
        'user_id': uid,
        'name': name,
        'is_bot': is_bot,
        'mine': bool(me.get('user_id') and uid == me.get('user_id')),
        'text': text,
        'md': md,
        'html': _md_to_html(md),
        'reply_count': m.get('reply_count', 0),
        'thread_ts': m.get('thread_ts'),
        'files': files,
        'edited': bool(m.get('edited')),
    }


def _prepare(bot, ch_no):
    """(channel_row, client, None) または (None, None, エラー文)"""
    try:
        ch = _channel(ch_no)
    except Exception as e:
        return None, None, f'チャンネルの表を読めません: {e}'
    if not ch or not ch.get('channel_id'):
        return None, None, f'チャンネル {ch_no} 番は登録されていません'
    client, err = _client(bot)
    if client is None:
        return None, None, err
    return ch, client, None


def _fail(e, what):
    """例外を戻り値の形に直す．回数制限なら retry_after を付ける"""
    if isinstance(e, RateLimited):
        return {'ok': False, 'messages': [], 'retry_after': e.retry_after,
                'error': f'Slack の回数制限に当たりました（{e.retry_after} 秒待ってください）'}
    if isinstance(e, SlackApiError):
        return {'ok': False, 'messages': [], 'error': _err(e)}
    return {'ok': False, 'messages': [], 'error': f'{what}に失敗しました: {e}'}


def read(bot, ch_no, limit=30):
    """Bot としてチャンネル ch_no の最近のメッセージを読む（古い順）．
       戻り値 {'ok', 'messages': [...], 'error'}
       各メッセージ：ts, time, datetime, user_id, name, is_bot, mine, text, md, html,
                     reply_count, thread_ts, files, edited"""
    ch, client, err = _prepare(bot, ch_no)
    if err:
        return {'ok': False, 'messages': [], 'error': err}
    me = bot_info(bot)
    try:
        r = _call(client, 'conversations_history', channel=ch['channel_id'],
                  limit=max(1, min(int(limit), 200)))
    except Exception as e:
        return _fail(e, '読み取り')
    out = [_format(client, bot, me, m) for m in reversed(r.get('messages') or [])
           if m.get('subtype') not in _SKIP_SUBTYPES]
    return {'ok': True, 'messages': out, 'error': None}


def history_page(bot, ch_no, cursor=None, oldest=None, latest=None, limit=200):
    """チャンネル ch_no の履歴を1ページ（最大 limit 件）読む．
       Slack は1回の呼び出しで全部を返さないので，戻り値の next_cursor を
       次の呼び出しに渡して，next_cursor が空になるまで繰り返す．
       oldest／latest は UNIX 時刻（秒）で期間を絞る．
       戻り値 {'ok', 'messages'（このページ分，古い順）, 'next_cursor', 'error'}
       回数制限のときは 'retry_after'（秒）を付ける"""
    ch, client, err = _prepare(bot, ch_no)
    if err:
        return {'ok': False, 'messages': [], 'error': err}
    me = bot_info(bot)
    kw = {'channel': ch['channel_id'], 'limit': max(1, min(int(limit), 1000))}
    if cursor:
        kw['cursor'] = cursor
    if oldest:
        kw['oldest'] = str(oldest)
    if latest:
        kw['latest'] = str(latest)
    try:
        r = _call(client, 'conversations_history', **kw)
    except Exception as e:
        return _fail(e, '読み取り')
    out = [_format(client, bot, me, m) for m in reversed(r.get('messages') or [])
           if m.get('subtype') not in _SKIP_SUBTYPES]
    nxt = (r.get('response_metadata') or {}).get('next_cursor') or ''
    return {'ok': True, 'messages': out, 'next_cursor': nxt, 'error': None}


def replies(bot, ch_no, thread_ts, cursor=None, limit=200):
    """スレッド thread_ts の返信を1ページ読む（親メッセージは除く，古い順）．
       ページ送りは history_page と同じく next_cursor で行う．
       戻り値 {'ok', 'messages', 'next_cursor', 'error'}（回数制限なら 'retry_after'）"""
    if not thread_ts:
        return {'ok': False, 'messages': [], 'error': 'スレッドが指定されていません'}
    ch, client, err = _prepare(bot, ch_no)
    if err:
        return {'ok': False, 'messages': [], 'error': err}
    me = bot_info(bot)
    kw = {'channel': ch['channel_id'], 'ts': thread_ts, 'limit': max(1, min(int(limit), 1000))}
    if cursor:
        kw['cursor'] = cursor
    try:
        r = _call(client, 'conversations_replies', **kw)
    except Exception as e:
        return _fail(e, 'スレッドの読み取り')
    out = [_format(client, bot, me, m) for m in (r.get('messages') or [])
           if m.get('ts') != thread_ts and m.get('subtype') not in _SKIP_SUBTYPES]
    nxt = (r.get('response_metadata') or {}).get('next_cursor') or ''
    return {'ok': True, 'messages': out, 'next_cursor': nxt, 'error': None}


def membership(bot, ch_no):
    """Bot がチャンネルに参加しているか（管理画面の確認用）．
       戻り値 {'ok', 'is_member', 'name', 'error'}"""
    try:
        ch = _channel(ch_no)
    except Exception as e:
        return {'ok': False, 'error': str(e)}
    if not ch or not ch.get('channel_id'):
        return {'ok': False, 'error': '未登録'}
    client, err = _client(bot)
    if client is None:
        return {'ok': False, 'error': err}
    try:
        c = (_call(client, 'conversations_info', channel=ch['channel_id']).get('channel') or {})
        return {'ok': True, 'is_member': bool(c.get('is_member')), 'name': c.get('name', ''),
                'error': None}
    except SlackApiError as e:
        return {'ok': False, 'error': _err(e)}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


# ════════════════════════════════════════════════════════════
# まとめ読み（サーバ側でページ送りを回す．Claude 用のテキストページが使う）
# ════════════════════════════════════════════════════════════

def pick_bot(ch_no, bot=None):
    """読み取りに使う Bot を決める．指定が無ければ，接続できてチャンネルに参加している最初の Bot．
       戻り値 (bot の名前, None) または (None, エラー文)"""
    if bot:
        return bot, None
    names = list(_bot_tokens())
    if not names:
        return None, 'config.py に SLACK_BOTS がありません'
    for b in names:
        if bot_info(b).get('ok'):
            m = membership(b, ch_no)
            if m.get('ok') and m.get('is_member'):
                return b, None
    return None, 'このチャンネルに参加している Bot がありません（/invite で招待してください）'


def channel_meta(bot, ch_no):
    """チャンネルの Slack 上の名前，作成日時，話題，目的（conversations.info）"""
    ch, client, err = _prepare(bot, ch_no)
    if err:
        return {'ok': False, 'error': err}
    try:
        c = (_call(client, 'conversations_info', channel=ch['channel_id']).get('channel') or {})
    except Exception as e:
        return {'ok': False, 'error': _fail(e, '情報の取得')['error']}
    created = c.get('created')
    return {'ok': True, 'name': c.get('name', ''),
            'created': datetime.datetime.fromtimestamp(created, JST).strftime('%Y-%m-%d') if created else '',
            'topic': (c.get('topic') or {}).get('value', ''),
            'purpose': (c.get('purpose') or {}).get('value', ''), 'error': None}


def scan_months(bot, ch_no, budget=40):
    """全履歴の時刻だけを読み，月ごとの件数とスレッド数を数える（名前の解決はしない）．
       budget 秒を超えたら打ち切って complete=False を返す．
       戻り値 {'ok', 'months': [{'ym', 'count', 'threads', 'replies'}]（古い順）,
               'total', 'complete', 'error'}"""
    ch, client, err = _prepare(bot, ch_no)
    if err:
        return {'ok': False, 'months': [], 'error': err}
    t0 = time.time()
    months, cursor, complete, error = {}, None, True, None
    while True:
        kw = {'channel': ch['channel_id'], 'limit': 1000}
        if cursor:
            kw['cursor'] = cursor
        try:
            r = _call(client, 'conversations_history', **kw)
        except RateLimited as e:
            if time.time() - t0 + e.retry_after < budget:
                time.sleep(e.retry_after)
                continue
            complete, error = False, _fail(e, '')['error']
            break
        except Exception as e:
            complete, error = False, _fail(e, '読み取り')['error']
            break
        for m in r.get('messages') or []:
            if m.get('subtype') in _SKIP_SUBTYPES:
                continue
            ym = datetime.datetime.fromtimestamp(float(m.get('ts', '0')), JST).strftime('%Y-%m')
            d = months.setdefault(ym, {'ym': ym, 'count': 0, 'threads': 0, 'replies': 0})
            d['count'] += 1
            if m.get('reply_count') and m.get('thread_ts') == m.get('ts'):
                d['threads'] += 1
                d['replies'] += m.get('reply_count', 0)
        cursor = (r.get('response_metadata') or {}).get('next_cursor')
        if not cursor:
            break
        if time.time() - t0 > budget:
            complete = False
            break
    out = [months[k] for k in sorted(months)]
    return {'ok': error is None or bool(out), 'months': out,
            'total': sum(d['count'] for d in out), 'complete': complete, 'error': error}


def _all_pages(client, method, kw, t0, budget):
    """method を next_cursor が尽きるまで呼ぶ．戻り値 (生メッセージの列, 完了したか, エラー文)"""
    msgs, cursor = [], None
    while True:
        k = dict(kw)
        if cursor:
            k['cursor'] = cursor
        try:
            r = _call(client, method, **k)
        except RateLimited as e:
            if time.time() - t0 + e.retry_after < budget:
                time.sleep(e.retry_after)
                continue
            return msgs, False, _fail(e, '')['error']
        except Exception as e:
            return msgs, False, _fail(e, '読み取り')['error']
        msgs.extend(r.get('messages') or [])
        cursor = (r.get('response_metadata') or {}).get('next_cursor')
        if not cursor:
            return msgs, True, None
        if time.time() - t0 > budget:
            return msgs, False, None


def period_messages(bot, ch_no, oldest, latest, with_replies=True, budget=90):
    """期間 [oldest, latest)（UNIX 時刻）のメッセージを全部読み，スレッドの返信も付ける．
       budget 秒を超えたら打ち切り，読めなかったスレッドを pending_threads で返す．
       戻り値 {'ok', 'messages'（古い順，各親に 'replies'）, 'complete',
               'pending_threads': [ts], 'error'}"""
    ch, client, err = _prepare(bot, ch_no)
    if err:
        return {'ok': False, 'messages': [], 'error': err}
    me = bot_info(bot)
    t0 = time.time()
    raw, complete, error = _all_pages(client, 'conversations_history',
                                      {'channel': ch['channel_id'], 'limit': 1000,
                                       'oldest': str(oldest), 'latest': str(latest)}, t0, budget)
    msgs = [_format(client, bot, me, m) for m in reversed(raw)
            if m.get('subtype') not in _SKIP_SUBTYPES]
    msgs.sort(key=lambda m: float(m['ts']))
    pending = []
    if with_replies:
        for m in msgs:
            if not (m['reply_count'] and m['thread_ts'] == m['ts']):
                continue
            if error or time.time() - t0 > budget:
                pending.append(m['ts'])
                continue
            rr, ok, e2 = _all_pages(client, 'conversations_replies',
                                    {'channel': ch['channel_id'], 'ts': m['ts'], 'limit': 1000},
                                    t0, budget)
            m['replies'] = [_format(client, bot, me, x) for x in rr
                            if x.get('ts') != m['ts'] and x.get('subtype') not in _SKIP_SUBTYPES]
            if not ok:
                pending.append(m['ts'])
                error = error or e2
    return {'ok': error is None or bool(msgs), 'messages': msgs,
            'complete': complete and not pending, 'pending_threads': pending, 'error': error}


def thread_messages(bot, ch_no, thread_ts, budget=60):
    """スレッド1本（親と返信）を全部読む．戻り値 {'ok', 'messages'（古い順）, 'complete', 'error'}"""
    ch, client, err = _prepare(bot, ch_no)
    if err:
        return {'ok': False, 'messages': [], 'error': err}
    me = bot_info(bot)
    rr, ok, e = _all_pages(client, 'conversations_replies',
                           {'channel': ch['channel_id'], 'ts': thread_ts, 'limit': 1000},
                           time.time(), budget)
    msgs = [_format(client, bot, me, x) for x in rr if x.get('subtype') not in _SKIP_SUBTYPES]
    return {'ok': e is None or bool(msgs), 'messages': msgs, 'complete': ok, 'error': e}


# 通知の口（2026-10-18）．他のアプリは api から呼ぶ
from .notify import register, notify, notify_dm  # noqa: E402,F401
from .notify import apps as notify_apps, set_log as notify_set_log  # noqa: E402,F401  管理画面用
