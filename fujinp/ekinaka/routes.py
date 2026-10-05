"""
えきなか (ekinaka) – 駅ナカキャンパス イベント管理
Blueprint routes

【権限モデル】
  admin ユーザ（user_category='admin'）は設定に関わらず全権限を持つ。

  view    : 公開日前プレビュー閲覧。
            group_name='__public__' を設定すれば全員（匿名含む）閲覧可。
  apply   : イベント申請
  approve : 承認・管理。「えきなか管理」グループ または admin。

【アクセス設定の保存方式】
  ekinaka_access_settings テーブルに group_name（ラベル）で保存。
  グループIDが変わっても影響を受けない（まいぐるマイグレーション対応）。

【公開ルール】
  status in (approved, cancelled, postponed)
  AND effective_public_date <= today  → 誰でも閲覧可（匿名）
  status in (approved, cancelled, postponed)
  AND user in view_groups             → 公開日前も閲覧可

【ステータス遷移】
  pending  → approved / rejected / withdrawn（公開日前）
  approved → cancelled / postponed / withdrawn（公開日前）
  rejected/cancelled/postponed/withdrawn → approved（承認者のみ復元）
"""

import os
import logging
import datetime
from datetime import timedelta
import calendar as cal_module
from flask import (render_template, request, redirect, url_for,
                   session, flash, abort, current_app, send_file, jsonify, send_from_directory)
from functools import wraps
from pytz import timezone
from werkzeug.utils import secure_filename  # noqa: F401 (将来の拡張用)
import mysql.connector
from markdown_converter import process_markdown_for_preview

from db import DatabaseConfig
from auth import redirect_to_dashboard
from . import ekinaka_bp

JST = timezone('Asia/Tokyo')

# ─────────────────────────────────────────────────────────────────
# イベント詳細（detail）本文用 画像アップロード設定
#   tanzax の短冊ノート用アップロードと同仕様。
#   保存先はアプリ配下 static/ekinaka_imgs/ → /ekinaka/img/<file>（image ルートが配信）
# ─────────────────────────────────────────────────────────────────
EKINAKA_IMG_SUBDIR = 'ekinaka_imgs'
# 画像・ディスプレイ画像はアプリ配下に置く（プラットフォーム規約．GitHub には送られない）．
# 旧版は Flask の static フォルダ（/static/ekinaka_imgs/）に置いていた．
EKINAKA_APP_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
EKINAKA_IMG_DIR = os.path.join(EKINAKA_APP_STATIC, EKINAKA_IMG_SUBDIR)
EKINAKA_IMG_ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}


def _ekinaka_allowed_img(filename):
    return ('.' in filename and
            filename.rsplit('.', 1)[1].lower() in EKINAKA_IMG_ALLOWED_EXTENSIONS)

# ─────────────────────────────────────────────────────────────────
# メッセージディスプレイ（e ink 800×480 png）設定
# ─────────────────────────────────────────────────────────────────
# えきなかキャンパスのテーブル上に置かれた e ink ディスプレイ（5台未満）。
# 各機内蔵プロセッサ（ラズパイ程度・知能なし）は、自機用の固定URL一本だけを
# 定期的に取りに来る。表示内容の切替はすべてサーバー側で行う。
#
# 【方式】
#   各スロット n に 2 枚を登録（a 面 / b 面）。管理画面の「次画像」ボタンで
#   現在出す面（active）を手動で a↔b 反転する。将来この切替部分は別プログラム
#   （自動運用）に置き換える。
#
#   機体が叩く固定URL : /ekinaka/display/<n>.png  ← Flask 動的ルート
#       サーバーが active 面のファイルをその場で send_file で返す。実体コピー不要。
#
# 【保存場所】
#   アプリ配下の static/ekinaka_displays/（機体へは display_serve ルートが配信）。
#   ファイル: display_<n>_a.png / display_<n>_b.png
#   active 状態: _state.json （{ "1": "a", "2": "b", ... }）
#   保存先はアプリ配下 static/ekinaka_displays/（ユーザ名に依存しない）。
EKINAKA_DISPLAY_COUNT  = 5
EKINAKA_DISPLAY_SUBDIR = 'ekinaka_displays'
EKINAKA_DISPLAY_W      = 800
EKINAKA_DISPLAY_H      = 480
EKINAKA_DISPLAY_FACES  = ('a', 'b')
EKINAKA_DISPLAY_STATE  = '_state.json'


def display_dir():
    """画像保存先の絶対パス（無ければ作成）。"""
    base = os.path.join(EKINAKA_APP_STATIC, EKINAKA_DISPLAY_SUBDIR)
    os.makedirs(base, exist_ok=True)
    return base


def display_face_filename(slot, face):
    """スロット番号・面(a/b) → 実体ファイル名。"""
    return f'display_{slot}_{face}.png'


def display_face_path(slot, face):
    return os.path.join(display_dir(), display_face_filename(slot, face))


def display_serve_url(slot):
    """機体が取りに来る固定の絶対URL（動的ルート）。"""
    return url_for('ekinaka.display_serve', slot=slot, _external=True)


def _state_path():
    return os.path.join(display_dir(), EKINAKA_DISPLAY_STATE)


def load_display_state():
    """active 面の状態を読む。{slot(int): 'a'|'b'}。欠損は 'a' 既定。"""
    import json
    state = {}
    try:
        with open(_state_path(), encoding='utf-8') as fp:
            raw = json.load(fp)
        for k, v in (raw or {}).items():
            if v in EKINAKA_DISPLAY_FACES:
                state[int(k)] = v
    except Exception:
        pass
    for i in range(1, EKINAKA_DISPLAY_COUNT + 1):
        state.setdefault(i, 'a')
    return state


def save_display_state(state):
    import json
    data = {str(k): v for k, v in state.items()}
    with open(_state_path(), 'w', encoding='utf-8') as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)


def display_slots_state():
    """
    各スロットの現況を返す。
      a_exists / b_exists : 各面の画像有無
      active              : 現在 active な面 ('a'/'b')
      active_exists       : active 面に画像があるか（機体に出るか）
      serve_url           : 機体が取りに来る固定URL
      a_mtime / b_mtime   : 各面の最終更新時刻
    """
    state = load_display_state()
    slots = []
    for i in range(1, EKINAKA_DISPLAY_COUNT + 1):
        faces = {}
        for face in EKINAKA_DISPLAY_FACES:
            fpath  = display_face_path(i, face)
            exists = os.path.isfile(fpath)
            mtime  = None
            if exists:
                try:
                    mtime = datetime.datetime.fromtimestamp(os.path.getmtime(fpath))
                except Exception:
                    mtime = None
            faces[face] = {'exists': exists, 'mtime': mtime}
        active = state.get(i, 'a')
        slots.append({
            'slot':          i,
            'active':        active,
            'active_exists': faces[active]['exists'],
            'serve_url':     display_serve_url(i),
            'a_exists':      faces['a']['exists'],
            'b_exists':      faces['b']['exists'],
            'a_mtime':       faces['a']['mtime'],
            'b_mtime':       faces['b']['mtime'],
        })
    return slots

def get_jst_now():
    return datetime.datetime.now(JST).replace(tzinfo=None)

# ─────────────────────────────────────────────────────────────────
# アクセスログ記録
# ─────────────────────────────────────────────────────────────────
def _log_access(endpoint: str, ref_event_id=None):
    """
    ekinaka_access_logs にアクセス1行を非同期的に記録。
    例外は握りつぶし（ログ失敗でアプリを止めない）。
    """
    try:
        # プロキシ越しの実IP取得（X-Forwarded-For 対応）
        ip = (request.headers.get('X-Forwarded-For', '') or '').split(',')[0].strip() \
             or request.remote_addr
        ua = (request.user_agent.string or '')[:512]
        uid = current_uid()
        now = get_jst_now()

        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO ekinaka_access_logs
              (accessed_at, endpoint, user_id, ip_address, user_agent, ref_event_id)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (now, endpoint, uid, ip, ua, ref_event_id))
        conn.commit()
    except Exception:
        pass
    finally:
        try:
            if 'conn' in locals() and conn.is_connected():
                cur.close(); conn.close()
        except Exception:
            pass

# 匿名公開マーカー（document_archive の public sentinel 相当）
PUBLIC_SENTINEL = '__public__'


# ─────────────────────────────────────────────────────────────────
# Jinja2 フィルタ
# ─────────────────────────────────────────────────────────────────
@ekinaka_bp.app_template_filter('fmtdate')
def fmtdate(d, fmt='%Y/%m/%d'):
    if d is None:
        return ''
    if isinstance(d, str):
        d = datetime.date.fromisoformat(d)
    return d.strftime(fmt)


@ekinaka_bp.app_template_filter('fmtdatetime')
def fmtdatetime(dt, fmt='%Y/%m/%d %H:%M'):
    if dt is None:
        return ''
    return dt.strftime(fmt)

# ─────────────────────────────────────────────────────────────────
# 祝日データ（date → 祝日名）
# ─────────────────────────────────────────────────────────────────
JP_HOLIDAYS = {
    # 2026年
    datetime.date(2026,  1,  1): '元日',
    datetime.date(2026,  1, 12): '成人の日',
    datetime.date(2026,  2, 11): '建国記念の日',
    datetime.date(2026,  2, 23): '天皇誕生日',
    datetime.date(2026,  3, 20): '春分の日',
    datetime.date(2026,  4, 29): '昭和の日',
    datetime.date(2026,  5,  3): '憲法記念日',
    datetime.date(2026,  5,  4): 'みどりの日',
    datetime.date(2026,  5,  5): 'こどもの日',
    datetime.date(2026,  5,  6): '振替休日',      # 5/3（憲法記念日）が日曜
    datetime.date(2026,  7, 20): '海の日',
    datetime.date(2026,  8, 11): '山の日',
    datetime.date(2026,  9, 21): '敬老の日',
    datetime.date(2026,  9, 23): '秋分の日',
    datetime.date(2026, 10, 12): 'スポーツの日',
    datetime.date(2026, 11,  3): '文化の日',
    datetime.date(2026, 11, 23): '勤労感謝の日',
    # 11/23は月曜なので振替休日なし（11/24を削除）
    # 2027年（〜3月31日）
    datetime.date(2027,  1,  1): '元日',
    datetime.date(2027,  1, 11): '成人の日',
    datetime.date(2027,  2, 11): '建国記念の日',
    datetime.date(2027,  2, 23): '天皇誕生日',
    datetime.date(2027,  3, 22): '春分の日',
}

def build_month_calendar(events, year, month):
    """月カレンダーデータ構築（日曜始まり）。日→イベント数マップ付き週リストを返す。"""
    day_events = {}
    for e in events:
        start = e['event_start_date']
        end   = e['event_end_date']
        if isinstance(start, str):
            start = datetime.date.fromisoformat(start)
        if isinstance(end, str):
            end = datetime.date.fromisoformat(end)
        d = start
        while d <= end:
            if d.year == year and d.month == month:
                day_events[d.day] = day_events.get(d.day, 0) + 1
            d += timedelta(days=1)

    first_day    = datetime.date(year, month, 1)
    first_offset = (first_day.weekday() + 1) % 7  # 0=日, 1=月 ... 6=土
    last_day     = cal_module.monthrange(year, month)[1]

    flat = [None] * first_offset + list(range(1, last_day + 1))
    while len(flat) % 7:
        flat.append(None)

    weeks = []
    for i in range(0, len(flat), 7):
        week = []
        for d in flat[i:i+7]:
            holiday_name = JP_HOLIDAYS.get(datetime.date(year, month, d)) if d else None
            week.append({
                'day':     d,
                'count':   day_events.get(d, 0) if d else 0,
                'holiday': holiday_name,
            })
        weeks.append(week)
    return weeks

# ─────────────────────────────────────────────────────────────────
# 管理者チェック（document_archive と同パターン）
# ─────────────────────────────────────────────────────────────────
def is_admin_user():
    """管理者かどうかを判定（user_category='admin'）"""
    return session.get('user_category') == 'admin'

EKINAKA_ADMIN_GROUP = 'えきなか管理'

def is_ekinaka_admin(user_id=None):
    """えきなか管理者グループ または admin"""
    if is_admin_user():
        return True
    if not user_id:
        return False
    return EKINAKA_ADMIN_GROUP in user_group_names(user_id)

# ─────────────────────────────────────────────────────────────────
# グループ名ベースの権限チェック
# ─────────────────────────────────────────────────────────────────
def current_uid():
    return session.get('user_id')


def _ug(name):
    """まいぐる（ユーザとグループ）の公開APIを遅延で引く．無ければ None．"""
    try:
        from fujinp.user_groups import utils as _ug_utils
        return getattr(_ug_utils, name, None)
    except Exception:
        return None


def _user_group_names_direct(user_id):
    """
    ユーザが現在所属するグループ名リスト（valid_from/valid_until考慮）。
    user_group_memberships ↔ user_groups を JOIN してグループ名を取得。
    """
    if not user_id:
        return []
    try:
        now  = get_jst_now()
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT g.name
            FROM user_group_memberships m
            JOIN user_groups g ON g.id = m.group_id
            WHERE m.user_id = %s
              AND (m.valid_from  IS NULL OR m.valid_from  <= %s)
              AND (m.valid_until IS NULL OR m.valid_until >= %s)
        """, (user_id, now, now))
        return [r['name'] for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()


def user_group_names(user_id):
    """現在所属するグループ名．まいぐるの公開API（台帳由来のグループも含む）を使い，
       古いまいぐるで API が無いときだけ user_group_memberships を直接読む．"""
    if not user_id:
        return []
    fn = _ug('get_user_group_names')
    if fn:
        try:
            return list(fn(user_id) or [])
        except Exception:
            pass
    return _user_group_names_direct(user_id)


def access_group_names(access_type):
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT group_name
            FROM ekinaka_access_settings
            WHERE access_type = %s
        """, (access_type,))
        return [r['group_name'] for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close()
            conn.close()


def has_perm(access_type, user_id=None):
    if is_admin_user():
        return True

    allowed = access_group_names(access_type)

    if PUBLIC_SENTINEL in allowed:
        return True

    if not user_id:
        return False

    return bool(set(user_group_names(user_id)) & set(allowed))


def require_perm(access_type):
    """デコレータ: 権限なければ 403"""
    def deco(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not has_perm(access_type, current_uid()):
                abort(403)
            return f(*args, **kwargs)
        return wrapped
    return deco


# ─────────────────────────────────────────────────────────────────
# 公開日・可視性ロジック
# ─────────────────────────────────────────────────────────────────
def eff_public_date(event):
    return event.get('approver_public_date') or event.get('applicant_public_date')


VISIBLE_STATUSES = ('approved', 'cancelled', 'postponed')


def is_public_anonymous(event):
    if event['status'] not in VISIBLE_STATUSES:
        return False
    pd = eff_public_date(event)
    return pd is not None and pd <= datetime.date.today()


def can_view(event, user_id=None):
    """閲覧可否。admin / 申請者 / 承認者は常に可。"""
    if is_admin_user():
        return True
    if user_id:
        if event.get('applicant_id') == user_id:
            return True
        if has_perm('approve', user_id):
            return True
    if is_public_anonymous(event):
        return True
    if event['status'] in VISIBLE_STATUSES and has_perm('view', user_id):
        return True
    return False


# ─────────────────────────────────────────────────────────────────
# 表示用ラベル・装飾
# ─────────────────────────────────────────────────────────────────
STATUS_META = {
    'pending':   ('申請中', '#f59e0b'),
    'approved':  ('承認済', '#10b981'),
    'rejected':  ('不承認', '#ef4444'),
    'withdrawn': ('取下済', '#6b7280'),
    'cancelled': ('中止',   '#dc2626'),
    'postponed': ('延期',   '#7c3aed'),
}

LOC_LABEL = {'none': 'なし', 'partial': '一部専有', 'full': '全部専有'}


def enrich(event):
    st = event.get('status', 'pending')
    event['status_label'], event['status_color'] = STATUS_META.get(st, (st, '#888'))
    event['location_label']      = LOC_LABEL.get(event.get('location_use', 'none'), '')
    event['eff_public_date']     = eff_public_date(event)
    event['is_public_anonymous'] = is_public_anonymous(event)
    return event


# ─────────────────────────────────────────────────────────────────
# ダッシュボード
# ─────────────────────────────────────────────────────────────────
@ekinaka_bp.route('/return_to_fujin')
def return_to_fujin():
    """FUJIN-Pダッシュボードに戻る"""
    return redirect_to_dashboard()


@ekinaka_bp.route('/')
def index():
    """アプリのトップ（ダッシュボード）．"""
    _log_access('dashboard')          # ← 追加
    uid        = current_uid()
    apply_ok   = has_perm('apply', uid)
    approve_ok = has_perm('approve', uid)
    admin_ok   = is_admin_user()
    ekinaka_admin_ok = is_ekinaka_admin(uid)


    mode       = request.args.get('mode', 'week')
    anchor_str = request.args.get('anchor', datetime.date.today().isoformat())
    try:
        anchor = datetime.date.fromisoformat(anchor_str)
    except ValueError:
        anchor = datetime.date.today()
    today = datetime.date.today()

    if mode == 'day':
        period_start = anchor
        period_end   = anchor
        prev_anchor  = anchor - timedelta(days=1)
        next_anchor  = anchor + timedelta(days=1)
        period_label = anchor.strftime('%Y年%m月%d日')
    elif mode == 'month':
        period_start = anchor.replace(day=1)
        if period_start.month == 12:
            period_end = period_start.replace(year=period_start.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            period_end = period_start.replace(month=period_start.month + 1, day=1) - timedelta(days=1)
        prev_anchor  = (period_start - timedelta(days=1)).replace(day=1)
        next_anchor  = period_end + timedelta(days=1)
        period_label = anchor.strftime('%Y年%m月')
    else:  # week
        weekday      = anchor.weekday()
        period_start = anchor - timedelta(days=weekday)
        period_end   = period_start + timedelta(days=6)
        prev_anchor  = period_start - timedelta(days=7)
        next_anchor  = period_start + timedelta(days=7)
        period_label = (f"{period_start.strftime('%Y年%m月%d日')}〜"
                        f"{period_end.strftime('%m月%d日')}")

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT e.*, u.full_name AS applicant_name
            FROM ekinaka_events e
            LEFT JOIN users u ON u.id = e.applicant_id
            WHERE e.event_start_date <= %s
              AND e.event_end_date   >= %s
            ORDER BY e.event_start_date, e.event_end_date
        """, (period_end, period_start))
        events_raw = cur.fetchall()

        cur.execute("""
            SELECT e.*, u.full_name AS applicant_name
            FROM ekinaka_events e
            LEFT JOIN users u ON u.id = e.applicant_id
            WHERE e.event_end_date < %s
            ORDER BY e.event_end_date DESC
            LIMIT 30
        """, (period_start,))
        past_raw = cur.fetchall()
    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
        events_raw, past_raw = [], []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    events = [enrich(e) for e in events_raw if can_view(e, uid)]
    past   = [enrich(e) for e in past_raw   if can_view(e, uid)]

    # 公演データを一括取得してイベントに紐付け
    all_event_ids = [e['id'] for e in events + past]
    perf_map = {}
    if all_event_ids:
        try:
            conn = mysql.connector.connect(**DatabaseConfig.default())
            cur  = conn.cursor(dictionary=True)
            fmt  = ','.join(['%s'] * len(all_event_ids))
            cur.execute(f"""
                SELECT event_id, start_datetime, end_datetime, note
                FROM ekinaka_performances
                WHERE event_id IN ({fmt})
                ORDER BY start_datetime
            """, all_event_ids)
            for p in cur.fetchall():
                try:
                    p['note_html'] = process_markdown_for_preview(p['note']) if p['note'] else ''
                except Exception:
                    p['note_html'] = p['note'] or ''
                perf_map.setdefault(p['event_id'], []).append(p)
        except Exception:
            pass
        finally:
            if 'conn' in locals() and conn.is_connected():
                cur.close(); conn.close()

    for e in events + past:
        e['performances'] = perf_map.get(e['id'], [])

    cal_weeks = build_month_calendar(events, anchor.year, anchor.month) if mode == 'month' else None

    # 同月末日を計算
    same_month_end = period_end.replace(day=1)
    if same_month_end.month == 12:
        same_month_end = same_month_end.replace(year=same_month_end.year+1, month=1, day=1) - timedelta(days=1)
    else:
        same_month_end = same_month_end.replace(month=same_month_end.month+1, day=1) - timedelta(days=1)

    # 翌月・翌々月のイベント存在チェック（anchor基準）
    anchor_month   = anchor.replace(day=1)
    next_month     = (anchor_month.replace(month=anchor_month.month+1)
                      if anchor_month.month < 12
                      else anchor_month.replace(year=anchor_month.year+1, month=1))
    after_next     = (next_month.replace(month=next_month.month+1)
                      if next_month.month < 12
                      else next_month.replace(year=next_month.year+1, month=1))
    after_after_next = (after_next.replace(month=after_next.month+1)
                        if after_next.month < 12
                        else after_next.replace(year=after_next.year+1, month=1))

    month_hint = {}
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT
              SUM(event_end_date > %s AND event_start_date <= %s) AS same_cnt,
              SUM(event_end_date >= %s AND event_start_date < %s) AS next_cnt,
              SUM(event_end_date >= %s AND event_start_date < %s) AS after_cnt
            FROM ekinaka_events
            WHERE status IN ('approved','cancelled','postponed')
              AND COALESCE(approver_public_date, applicant_public_date) <= %s
        """, (period_end, same_month_end,
              next_month, after_next,
              after_next, after_after_next,
              today))
        row = cur.fetchone()
        if row and (row['same_cnt'] or 0) > 0 and period_end.month == anchor_month.month:
            month_hint['same'] = anchor_month
        if row and (row['next_cnt'] or 0) > 0:
            month_hint['this'] = next_month
        if row and (row['after_cnt'] or 0) > 0:
            month_hint['next'] = after_next
    except Exception:
        pass
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    return render_template('ekinaka_dashboard.html',
        events=events, past=past,
        cal_weeks=cal_weeks,
        mode=mode, anchor=anchor,
        period_label=period_label,
        prev_anchor=prev_anchor, next_anchor=next_anchor,
        today=today,
        uid=uid, apply_ok=apply_ok, approve_ok=approve_ok,
        admin_ok=admin_ok,
        ekinaka_admin_ok=ekinaka_admin_ok,
        month_hint=month_hint
    )


# ─────────────────────────────────────────────────────────────────
# イベント詳細
# ─────────────────────────────────────────────────────────────────
@ekinaka_bp.route('/event/<int:eid>')
def event_detail(eid):
    _log_access('event_detail', ref_event_id=eid)   # ← 追加
    uid = current_uid()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT e.*,
                   u.full_name AS applicant_name,
                   a.full_name AS approver_name
            FROM ekinaka_events e
            LEFT JOIN users u ON u.id = e.applicant_id
            LEFT JOIN users a ON a.id = e.approver_id
            WHERE e.id = %s
        """, (eid,))
        event = cur.fetchone()
        if not event:
            abort(404)
        cur.execute("""
            SELECT * FROM ekinaka_performances
            WHERE event_id = %s ORDER BY start_datetime
        """, (eid,))
        performances = cur.fetchall()
    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
        abort(500)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    if not can_view(event, uid):
        abort(403)

    enrich(event)
    today        = datetime.date.today()
    pub_date     = eff_public_date(event)
    past_public  = pub_date is not None and pub_date <= today
    is_applicant = uid is not None and event['applicant_id'] == uid
    is_approver  = has_perm('approve', uid)

    # detail / summary / other_notes を Markdown → HTML 変換
    def _md(text):
        if not text:
            return ''
        try:
            return process_markdown_for_preview(text)
        except Exception:
            return text

    detail_html      = _md(event.get('detail'))
    summary_html     = _md(event.get('summary'))
    other_notes_html = _md(event.get('other_notes'))

    return render_template('ekinaka_event_detail.html',
        event=event, performances=performances,
        is_applicant=is_applicant, is_approver=is_approver,
        past_public=past_public, today=today, uid=uid,
        apply_ok=has_perm('apply', uid),
        detail_html=detail_html,
        summary_html=summary_html,
        other_notes_html=other_notes_html,
        is_ekinaka_admin=is_ekinaka_admin(uid)
    )


# ─────────────────────────────────────────────────────────────────
# 申請フォーム（新規）
# ─────────────────────────────────────────────────────────────────
@ekinaka_bp.route('/apply', methods=['GET', 'POST'])
@require_perm('apply')
def apply_new():
    uid = current_uid()
    if request.method == 'POST':
        title    = request.form.get('title', '').strip()
        summary  = request.form.get('summary', '').strip()
        s_date   = request.form.get('event_start_date', '').strip()
        e_date   = request.form.get('event_end_date', '').strip()
        loc_use  = request.form.get('location_use', 'none')
        other    = request.form.get('other_notes', '').strip()
        detail   = request.form.get('detail', '').strip()
        pub_date = request.form.get('applicant_public_date', '').strip() or None

        if not title or not s_date or not e_date:
            flash('イベント名・開始日・終了日は必須です', 'error')
            return redirect(url_for('ekinaka.apply_new'))

        try:
            conn = mysql.connector.connect(**DatabaseConfig.default())
            cur  = conn.cursor()
            cur.execute("""
                INSERT INTO ekinaka_events
                  (title, summary, detail, event_start_date, event_end_date,
                   location_use, other_notes, applicant_id, applicant_public_date, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending')
            """, (title, summary, detail, s_date, e_date, loc_use, other, uid, pub_date))
            conn.commit()
            new_id = cur.lastrowid
            flash('申請を受け付けました', 'success')
            return redirect(url_for('ekinaka.event_detail', eid=new_id))
        except Exception as ex:
            conn.rollback()
            flash(f'エラー: {ex}', 'error')
        finally:
            if 'conn' in locals() and conn.is_connected():
                cur.close(); conn.close()

    return render_template('ekinaka_apply.html', event=None, mode='new')


# ─────────────────────────────────────────────────────────────────
# 申請編集（pending のみ）
# ─────────────────────────────────────────────────────────────────
@ekinaka_bp.route('/event/<int:eid>/edit', methods=['GET', 'POST'])
@require_perm('apply')
def apply_edit(eid):
    uid = current_uid()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM ekinaka_events WHERE id=%s", (eid,))
        event = cur.fetchone()
    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
        abort(500)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    if not event:
        abort(404)
    if not (is_admin_user() or event['applicant_id'] == uid or has_perm('approve', uid)):
        abort(403)
    if event['status'] != 'pending':
        flash('申請中のイベントのみ編集できます', 'error')
        return redirect(url_for('ekinaka.event_detail', eid=eid))

    if request.method == 'POST':
        title    = request.form.get('title', '').strip()
        summary  = request.form.get('summary', '').strip()
        detail   = request.form.get('detail', '').strip()
        s_date   = request.form.get('event_start_date', '').strip()
        e_date   = request.form.get('event_end_date', '').strip()
        loc_use  = request.form.get('location_use', 'none')
        other    = request.form.get('other_notes', '').strip()
        pub_date = request.form.get('applicant_public_date', '').strip() or None
        try:
            conn = mysql.connector.connect(**DatabaseConfig.default())
            cur  = conn.cursor()
            cur.execute("""
                UPDATE ekinaka_events SET
                  title=%s, summary=%s, detail=%s,
                  event_start_date=%s, event_end_date=%s,
                  location_use=%s, other_notes=%s, applicant_public_date=%s
                WHERE id=%s
            """, (title, summary, detail, s_date, e_date, loc_use, other, pub_date, eid))
            conn.commit()
            flash('保存しました', 'success')
            return redirect(url_for('ekinaka.event_detail', eid=eid))
        except Exception as ex:
            conn.rollback()
            flash(f'エラー: {ex}', 'error')
        finally:
            if 'conn' in locals() and conn.is_connected():
                cur.close(); conn.close()

    return render_template('ekinaka_apply.html', event=event, mode='edit')


# ─────────────────────────────────────────────────────────────────
# マイ申請一覧
# ─────────────────────────────────────────────────────────────────
@ekinaka_bp.route('/my')
@require_perm('apply')
def my_events():
    uid = current_uid()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT * FROM ekinaka_events
            WHERE applicant_id = %s
            ORDER BY event_start_date DESC
        """, (uid,))
        events = [enrich(e) for e in cur.fetchall()]
    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
        events = []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return render_template('ekinaka_my.html',
        events=events, uid=uid,
        approve_ok=has_perm('approve', uid))


# ─────────────────────────────────────────────────────────────────
# ステータス操作（申請者）
# ─────────────────────────────────────────────────────────────────
def _get_event_for_applicant(eid, uid):
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM ekinaka_events WHERE id=%s", (eid,))
        event = cur.fetchone()
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    if not event:
        abort(404)
    if not (is_admin_user() or event['applicant_id'] == uid):
        abort(403)
    return event


@ekinaka_bp.route('/event/<int:eid>/withdraw', methods=['POST'])
@require_perm('apply')
def withdraw(eid):
    uid   = current_uid()
    event = _get_event_for_applicant(eid, uid)
    today = datetime.date.today()
    pd    = eff_public_date(event)

    if event['status'] == 'approved' and pd and pd <= today:
        flash('公開日を過ぎているため取り下げできません。中止または延期を使用してください。', 'error')
        return redirect(url_for('ekinaka.event_detail', eid=eid))
    if event['status'] not in ('pending', 'approved'):
        flash('この操作はできません', 'error')
        return redirect(url_for('ekinaka.event_detail', eid=eid))

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("UPDATE ekinaka_events SET status='withdrawn' WHERE id=%s", (eid,))
        conn.commit()
        flash('取り下げました（記録は残ります）', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('ekinaka.event_detail', eid=eid))


@ekinaka_bp.route('/event/<int:eid>/cancel', methods=['POST'])
@require_perm('apply')
def cancel_event(eid):
    uid   = current_uid()
    event = _get_event_for_applicant(eid, uid)
    if event['status'] != 'approved':
        flash('承認済みイベントのみ中止できます', 'error')
        return redirect(url_for('ekinaka.event_detail', eid=eid))
    note = request.form.get('cancel_note', '').strip()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute(
            "UPDATE ekinaka_events SET status='cancelled', cancel_note=%s WHERE id=%s",
            (note, eid))
        conn.commit()
        flash('中止としました（申請者はこの操作を取り消せません）', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('ekinaka.event_detail', eid=eid))


@ekinaka_bp.route('/event/<int:eid>/postpone', methods=['POST'])
@require_perm('apply')
def postpone_event(eid):
    uid   = current_uid()
    event = _get_event_for_applicant(eid, uid)
    if event['status'] != 'approved':
        flash('承認済みイベントのみ延期できます', 'error')
        return redirect(url_for('ekinaka.event_detail', eid=eid))
    note = request.form.get('cancel_note', '').strip()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute(
            "UPDATE ekinaka_events SET status='postponed', cancel_note=%s WHERE id=%s",
            (note, eid))
        conn.commit()
        flash('延期としました（申請者はこの操作を取り消せません）', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('ekinaka.event_detail', eid=eid))


# ─────────────────────────────────────────────────────────────────
# 公演管理
# ─────────────────────────────────────────────────────────────────
@ekinaka_bp.route('/event/<int:eid>/performance/add', methods=['POST'])
@require_perm('apply')
def perf_add(eid):
    uid = current_uid()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM ekinaka_events WHERE id=%s", (eid,))
        event = cur.fetchone()
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    if not event:
        abort(404)
    if not (is_admin_user() or event['applicant_id'] == uid or has_perm('approve', uid)):
        abort(403)
    if event['status'] not in ('pending', 'approved'):
        flash('申請中または承認済みのイベントのみ公演を追加できます', 'error')
        return redirect(url_for('ekinaka.event_detail', eid=eid))

    s_dt = request.form.get('start_datetime', '').strip()
    e_dt = request.form.get('end_datetime', '').strip()
    note = request.form.get('perf_note', '').strip()
    if not s_dt or not e_dt:
        flash('開始・終了日時は必須です', 'error')
        return redirect(url_for('ekinaka.event_detail', eid=eid))

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO ekinaka_performances (event_id, start_datetime, end_datetime, note)
            VALUES (%s, %s, %s, %s)
        """, (eid, s_dt, e_dt, note))
        conn.commit()
        flash('公演を追加しました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('ekinaka.event_detail', eid=eid))


@ekinaka_bp.route('/performance/<int:pid>/edit', methods=['POST'])
@require_perm('apply')
def perf_edit(pid):
    uid = current_uid()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT p.*, e.applicant_id
            FROM ekinaka_performances p
            JOIN ekinaka_events e ON e.id = p.event_id
            WHERE p.id = %s
        """, (pid,))
        perf = cur.fetchone()
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    if not perf:
        abort(404)
    if not (is_admin_user() or perf['applicant_id'] == uid or has_perm('approve', uid)):
        abort(403)

    s_dt = request.form.get('start_datetime', '').strip()
    e_dt = request.form.get('end_datetime', '').strip()
    note = request.form.get('perf_note', '').strip()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("""
            UPDATE ekinaka_performances SET start_datetime=%s, end_datetime=%s, note=%s
            WHERE id=%s
        """, (s_dt, e_dt, note, pid))
        conn.commit()
        flash('公演を更新しました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('ekinaka.event_detail', eid=perf['event_id']))


@ekinaka_bp.route('/performance/<int:pid>/delete', methods=['POST'])
@require_perm('apply')
def perf_delete(pid):
    uid = current_uid()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT p.*, e.applicant_id
            FROM ekinaka_performances p
            JOIN ekinaka_events e ON e.id = p.event_id
            WHERE p.id = %s
        """, (pid,))
        perf = cur.fetchone()
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    if not perf:
        abort(404)
    if not (is_admin_user() or perf['applicant_id'] == uid or has_perm('approve', uid)):
        abort(403)

    eid = perf['event_id']
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("DELETE FROM ekinaka_performances WHERE id=%s", (pid,))
        conn.commit()
        flash('公演を削除しました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('ekinaka.event_detail', eid=eid))


# ─────────────────────────────────────────────────────────────────
# 承認者 管理画面
# ─────────────────────────────────────────────────────────────────
@ekinaka_bp.route('/manage')
@require_perm('approve')
def manage():
    filter_st = request.args.get('filter', 'active')
    where_map = {
        'pending': "WHERE e.status = 'pending'",
        'closed':  "WHERE e.status IN ('rejected','withdrawn','cancelled','postponed')",
        'all':     '',
    }
    where = where_map.get(filter_st, "WHERE e.status IN ('pending','approved')")
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute(f"""
            SELECT e.*, u.full_name AS applicant_name
            FROM ekinaka_events e
            LEFT JOIN users u ON u.id = e.applicant_id
            {where}
            ORDER BY
              CASE e.status WHEN 'pending' THEN 0 ELSE 1 END,
              e.event_start_date DESC
        """)
        events = [enrich(e) for e in cur.fetchall()]
    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
        events = []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return render_template('ekinaka_manage.html', events=events,
                           filter_st=filter_st, today=datetime.date.today())


@ekinaka_bp.route('/event/<int:eid>/approve', methods=['POST'])
@require_perm('approve')
def do_approve(eid):
    uid      = current_uid()
    notes    = request.form.get('approver_notes', '').strip()
    pub_date = request.form.get('approver_public_date', '').strip() or None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("""
            UPDATE ekinaka_events SET
              status='approved', approver_id=%s, approve_datetime=%s,
              approver_notes=%s, approver_public_date=%s
            WHERE id=%s
        """, (uid, get_jst_now(), notes, pub_date, eid))
        conn.commit()
        flash('承認しました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('ekinaka.event_detail', eid=eid))

@ekinaka_bp.route('/event/<int:eid>/reject', methods=['POST'])
@require_perm('approve')
def do_reject(eid):
    uid   = current_uid()
    notes = request.form.get('approver_notes', '').strip()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("""
            UPDATE ekinaka_events SET
              status='rejected', approver_id=%s, approve_datetime=%s, approver_notes=%s
            WHERE id=%s
        """, (uid, get_jst_now(), notes, eid))
        conn.commit()
        flash('不承認としました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(request.referrer or url_for('ekinaka.manage'))


@ekinaka_bp.route('/event/<int:eid>/set_status', methods=['POST'])
@require_perm('approve')
def set_status(eid):
    uid    = current_uid()
    new_st = request.form.get('new_status', '')
    note   = request.form.get('cancel_note', '').strip()
    if new_st not in STATUS_META:
        flash('無効なステータスです', 'error')
        return redirect(url_for('ekinaka.event_detail', eid=eid))
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("""
            UPDATE ekinaka_events SET
              status=%s, cancel_note=%s, approver_id=%s, approve_datetime=%s
            WHERE id=%s
        """, (new_st, note or None, uid, get_jst_now(), eid))
        conn.commit()
        flash(f'ステータスを「{STATUS_META[new_st][0]}」に変更しました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('ekinaka.event_detail', eid=eid))


@ekinaka_bp.route('/event/<int:eid>/update_notes', methods=['POST'])
@require_perm('approve')
def update_notes(eid):
    notes    = request.form.get('approver_notes', '').strip()
    pub_date = request.form.get('approver_public_date', '').strip() or None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("""
            UPDATE ekinaka_events SET approver_notes=%s, approver_public_date=%s
            WHERE id=%s
        """, (notes, pub_date, eid))
        conn.commit()
        flash('備考・公開日を更新しました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('ekinaka.event_detail', eid=eid))


@ekinaka_bp.route('/event/<int:eid>/update_detail', methods=['POST'])
@require_perm('apply')
def update_detail(eid):
    uid = current_uid()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("SELECT applicant_id FROM ekinaka_events WHERE id=%s", (eid,))
        event = cur.fetchone()
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    if not event:
        abort(404)
    if not (is_admin_user() or event['applicant_id'] == uid or has_perm('approve', uid)):
        abort(403)

    detail = request.form.get('detail', '').strip()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("UPDATE ekinaka_events SET detail=%s WHERE id=%s", (detail, eid))
        conn.commit()
        flash('メッセージを更新しました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('ekinaka.event_detail', eid=eid))

@ekinaka_bp.route('/event/<int:eid>/update_info', methods=['POST'])
@require_perm('apply')
def update_info(eid):
    """イベント情報（概要・その他）の更新。update_detail と同じ権限モデル。"""
    uid = current_uid()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("SELECT applicant_id FROM ekinaka_events WHERE id=%s", (eid,))
        event = cur.fetchone()
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    if not event:
        abort(404)
    if not (is_admin_user() or event['applicant_id'] == uid or has_perm('approve', uid)):
        abort(403)

    summary = request.form.get('summary', '').strip()
    other   = request.form.get('other_notes', '').strip()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("UPDATE ekinaka_events SET summary=%s, other_notes=%s WHERE id=%s",
                    (summary, other, eid))
        conn.commit()
        flash('イベント情報を更新しました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('ekinaka.event_detail', eid=eid))


@ekinaka_bp.route('/event/<int:eid>/update_title', methods=['POST'])
def update_title(eid):
    uid = current_uid()
    if not is_ekinaka_admin(uid):
        abort(403)
    title = request.form.get('title', '').strip()
    if not title:
        flash('タイトルは空にできません', 'error')
        return redirect(url_for('ekinaka.event_detail', eid=eid))
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("UPDATE ekinaka_events SET title=%s WHERE id=%s", (title, eid))
        conn.commit()
        flash('タイトルを更新しました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('ekinaka.event_detail', eid=eid))
# ─────────────────────────────────────────────────────────────────
# detail 編集用: Markdown プレビュー / 画像アップロード API
#   （tanzax 短冊ノートと同仕様の手作りエディタ用）
# ─────────────────────────────────────────────────────────────────
@ekinaka_bp.route('/api/preview_markdown', methods=['POST'])
def api_preview_markdown():
    """detail の Markdown → HTML プレビュー（FUJIN-P 共通コンバータ使用）。"""
    if current_uid() is None:
        return jsonify({'html': '', 'error': 'ログインが必要です'}), 403
    data = request.get_json(silent=True) or {}
    md = data.get('markdown', '') or ''
    if not md.strip():
        return jsonify({'html': ''})
    try:
        return jsonify({'html': process_markdown_for_preview(md)})
    except Exception as e:
        logging.error("ekinaka api_preview_markdown error: %s", e)
        import html as _html
        return jsonify({
            'html': '<pre style="white-space:pre-wrap">%s</pre>' % _html.escape(md),
            'error': str(e),
        })


@ekinaka_bp.route('/api/upload_image', methods=['POST'])
def api_upload_image():
    """detail 本文用の画像アップロード。アプリ配下 static/ekinaka_imgs/ に保存し /ekinaka/img/<名前> で配信。"""
    uid = current_uid()
    if uid is None or not (has_perm('apply', uid) or has_perm('approve', uid)):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'ファイルが選択されていません'}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({'success': False, 'error': 'ファイルが選択されていません'}), 400
    if not _ekinaka_allowed_img(file.filename):
        return jsonify({'success': False,
                        'error': '許可されていないファイル形式です（png/jpg/jpeg/gif/webp）'}), 400
    try:
        folder = EKINAKA_IMG_DIR
        os.makedirs(folder, exist_ok=True)
        ts = get_jst_now().strftime('%Y%m%d_%H%M%S')
        filename = secure_filename(file.filename)
        name, ext = os.path.splitext(filename)
        unique = f"{name}_{ts}{ext}"
        file.save(os.path.join(folder, unique))
        return jsonify({'success': True, 'filename': unique,
                        'url': url_for('ekinaka.image', filename=unique)})
    except Exception as e:
        logging.error("ekinaka api_upload_image error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


# ─────────────────────────────────────────────────────────────────
# アクセス権限設定（グループ名で管理）
# ─────────────────────────────────────────────────────────────────
@ekinaka_bp.route('/settings', methods=['GET', 'POST'])
def settings():
    if not is_admin_user():
        abort(403)
    if request.method == 'POST':
        try:
            conn = mysql.connector.connect(**DatabaseConfig.default())
            cur  = conn.cursor(dictionary=True)

            # 画面送信された group_id から name を引くための辞書

            cur.execute("DELETE FROM ekinaka_access_settings")
            for atype in ('view', 'apply', 'approve'):
                if request.form.get(f'{atype}_public'):
                    cur.execute("""
                        INSERT IGNORE INTO ekinaka_access_settings (access_type, group_name)
                        VALUES (%s, %s)
                    """, (atype, PUBLIC_SENTINEL))
                for gname in request.form.getlist(f'{atype}_groups'):
                    gname = (gname or '').strip()
                    if gname:
                        cur.execute("""
                            INSERT IGNORE INTO ekinaka_access_settings (access_type, group_name)
                            VALUES (%s, %s)
                        """, (atype, gname))


            conn.commit()
            flash('設定を保存しました', 'success')
            return redirect(url_for('ekinaka.settings'))

        except Exception as ex:
            conn.rollback()
            flash(f'エラー: {ex}', 'error')

        finally:
            if 'conn' in locals() and conn.is_connected():
                cur.close()
                conn.close()

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)

        cur.execute("""
            SELECT access_type, group_name
            FROM ekinaka_access_settings
            ORDER BY access_type, group_name
        """)
        rows = cur.fetchall()

        cur.execute("""
            SELECT id, name
            FROM user_groups
            ORDER BY name
        """)
        all_groups = cur.fetchall()

    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
        rows, all_groups = [], []

    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close()
            conn.close()

    by_type = {'view': set(), 'apply': set(), 'approve': set()}
    for r in rows:
        by_type[r['access_type']].add(r['group_name'])

    return render_template(
        'ekinaka_settings.html',
        by_type=by_type,
        all_groups=all_groups,
        public_sentinel=PUBLIC_SENTINEL,
        is_admin=is_admin_user()
    )


# ─────────────────────────────────────────────────────────────────
# メッセージディスプレイ設定（管理者専用）
#   各機 2 枚（a/b）を登録、「次画像」で active 面を手動反転。
#   機体は固定URL /ekinaka/display/<n>.png を取りに来る（動的配信）。
# ─────────────────────────────────────────────────────────────────
def _save_uploaded_png(file_storage, slot, face):
    """アップロードされた PNG を 1 面分保存。(ok: bool, err: str|None) を返す。"""
    name_ok = file_storage.filename.lower().endswith('.png')
    mime_ok = (file_storage.mimetype or '').lower() in (
        'image/png', 'application/octet-stream', '')
    if not (name_ok and mime_ok):
        return False, 'PNG ファイルを選択してください'

    data = file_storage.read()
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        return False, '有効な PNG ではありません'

    # Pillow があれば 800×480 へ正規化（無ければそのまま保存）
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert('RGB')
        if img.size != (EKINAKA_DISPLAY_W, EKINAKA_DISPLAY_H):
            img = img.resize((EKINAKA_DISPLAY_W, EKINAKA_DISPLAY_H))
        out = io.BytesIO()
        img.save(out, format='PNG')
        data = out.getvalue()
    except ImportError:
        pass

    with open(display_face_path(slot, face), 'wb') as fp:
        fp.write(data)
    return True, None


@ekinaka_bp.route('/display_settings', methods=['GET', 'POST'])
def display_settings():
    if not is_ekinaka_admin(current_uid()):
        abort(403)

    if request.method == 'POST':
        display_dir()  # ディレクトリ確保
        updated = 0
        errors  = []

        # 各スロット × 各面（a/b）のアップロードを処理
        for i in range(1, EKINAKA_DISPLAY_COUNT + 1):
            for face in EKINAKA_DISPLAY_FACES:
                f = request.files.get(f'display_{i}_{face}')
                if not f or not f.filename:
                    continue  # この面は今回アップロードなし
                try:
                    ok, err = _save_uploaded_png(f, i, face)
                    if ok:
                        updated += 1
                    else:
                        errors.append(f'ディスプレイ{i} {face.upper()}面: {err}')
                except Exception as ex:
                    errors.append(f'ディスプレイ{i} {face.upper()}面: 保存エラー {ex}')

        if updated:
            flash(f'{updated} 枚の画像を更新しました', 'success')
        if errors:
            for e in errors:
                flash(e, 'error')
        if not updated and not errors:
            flash('アップロードするファイルが選択されていません', 'error')

        return redirect(url_for('ekinaka.display_settings'))

    return render_template(
        'ekinaka_display_settings.html',
        slots=display_slots_state(),
        disp_w=EKINAKA_DISPLAY_W,
        disp_h=EKINAKA_DISPLAY_H,
        count=EKINAKA_DISPLAY_COUNT,
    )


@ekinaka_bp.route('/display_advance/<int:slot>', methods=['POST'])
def display_advance(slot):
    """「次画像」: 指定スロットの active 面を a↔b 反転（手動切替）。"""
    if not is_ekinaka_admin(current_uid()):
        abort(403)
    if not (1 <= slot <= EKINAKA_DISPLAY_COUNT):
        abort(404)

    state   = load_display_state()
    cur     = state.get(slot, 'a')
    new     = 'b' if cur == 'a' else 'a'
    state[slot] = new
    try:
        save_display_state(state)
        flash(f'ディスプレイ{slot}: 表示を {new.upper()} 面に切り替えました', 'success')
    except Exception as ex:
        flash(f'ディスプレイ{slot}: 切替エラー {ex}', 'error')
    return redirect(url_for('ekinaka.display_settings'))


# ─────────────────────────────────────────────────────────────────
# メッセージディスプレイ配信（機体が取りに来る固定URL・認証なし）
#   /ekinaka/display/<n>.png → active 面のファイルをその場で返す。
# ─────────────────────────────────────────────────────────────────
@ekinaka_bp.route('/display/<int:slot>.png')
def display_serve(slot):
    if not (1 <= slot <= EKINAKA_DISPLAY_COUNT):
        abort(404)
    _log_access('display_serve', ref_event_id=slot)

    state  = load_display_state()
    active = state.get(slot, 'a')
    fpath  = display_face_path(slot, active)

    # active 面が未登録なら、もう一方が在ればフォールバック
    if not os.path.isfile(fpath):
        other = 'b' if active == 'a' else 'a'
        alt   = display_face_path(slot, other)
        if os.path.isfile(alt):
            fpath = alt
        else:
            abort(404)

    # e ink は更新頻度が低いのでキャッシュは抑制（常に最新の active を取得）
    resp = send_file(fpath, mimetype='image/png', max_age=0)
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp


@ekinaka_bp.route('/display_preview/<int:slot>/<face>.png')
def display_preview(slot, face):
    """管理画面プレビュー用：指定スロットの指定面(a/b)を返す（管理者のみ）。"""
    if not is_ekinaka_admin(current_uid()):
        abort(403)
    if not (1 <= slot <= EKINAKA_DISPLAY_COUNT) or face not in EKINAKA_DISPLAY_FACES:
        abort(404)
    fpath = display_face_path(slot, face)
    if not os.path.isfile(fpath):
        abort(404)
    resp = send_file(fpath, mimetype='image/png', max_age=0)
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp


@ekinaka_bp.route('/public')
def public_view():
    """
    閲覧専用エンドポイント。
    ログイン状態・権限に関わらず一般公開イベントのみ表示。
    admin でも申請・管理ボタンは一切出ない。
    """
    _log_access('public')
    mode       = request.args.get('mode', 'week')
    anchor_str = request.args.get('anchor', datetime.date.today().isoformat())
    try:
        anchor = datetime.date.fromisoformat(anchor_str)
    except ValueError:
        anchor = datetime.date.today()
    today = datetime.date.today()

    if mode == 'day':
        period_start = anchor
        period_end   = anchor
        prev_anchor  = anchor - timedelta(days=1)
        next_anchor  = anchor + timedelta(days=1)
        period_label = anchor.strftime('%Y年%m月%d日')
    elif mode == 'month':
        period_start = anchor.replace(day=1)
        if period_start.month == 12:
            period_end = period_start.replace(year=period_start.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            period_end = period_start.replace(month=period_start.month + 1, day=1) - timedelta(days=1)
        prev_anchor  = (period_start - timedelta(days=1)).replace(day=1)
        next_anchor  = period_end + timedelta(days=1)
        period_label = anchor.strftime('%Y年%m月')
    else:
        weekday      = anchor.weekday()
        period_start = anchor - timedelta(days=weekday)
        period_end   = period_start + timedelta(days=6)
        prev_anchor  = period_start - timedelta(days=7)
        next_anchor  = period_start + timedelta(days=7)
        period_label = (f"{period_start.strftime('%Y年%m月%d日')}〜"
                        f"{period_end.strftime('%m月%d日')}")

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT e.*, u.full_name AS applicant_name
            FROM ekinaka_events e
            LEFT JOIN users u ON u.id = e.applicant_id
            WHERE e.event_start_date <= %s
              AND e.event_end_date   >= %s
            ORDER BY e.event_start_date, e.event_end_date
        """, (period_end, period_start))
        events_raw = cur.fetchall()

        cur.execute("""
            SELECT e.*, u.full_name AS applicant_name
            FROM ekinaka_events e
            LEFT JOIN users u ON u.id = e.applicant_id
            WHERE e.event_end_date < %s
            ORDER BY e.event_end_date DESC
            LIMIT 30
        """, (period_start,))
        past_raw = cur.fetchall()
    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
        events_raw, past_raw = [], []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    events = [enrich(e) for e in events_raw if is_public_anonymous(e)]
    past   = [enrich(e) for e in past_raw   if is_public_anonymous(e)]

    all_event_ids = [e['id'] for e in events + past]
    perf_map = {}
    if all_event_ids:
        try:
            conn = mysql.connector.connect(**DatabaseConfig.default())
            cur  = conn.cursor(dictionary=True)
            fmt  = ','.join(['%s'] * len(all_event_ids))
            cur.execute(f"""
                SELECT event_id, start_datetime, end_datetime, note
                FROM ekinaka_performances
                WHERE event_id IN ({fmt})
                ORDER BY start_datetime
            """, all_event_ids)
            for p in cur.fetchall():
                try:
                    p['note_html'] = process_markdown_for_preview(p['note']) if p['note'] else ''
                except Exception:
                    p['note_html'] = p['note'] or ''
                perf_map.setdefault(p['event_id'], []).append(p)
        except Exception:
            pass
        finally:
            if 'conn' in locals() and conn.is_connected():
                cur.close(); conn.close()

    for e in events + past:
        e['performances'] = perf_map.get(e['id'], [])

    cal_weeks = build_month_calendar(events, anchor.year, anchor.month) if mode == 'month' else None

    # 同月末日を計算
    same_month_end = period_end.replace(day=1)
    if same_month_end.month == 12:
        same_month_end = same_month_end.replace(year=same_month_end.year+1, month=1, day=1) - timedelta(days=1)
    else:
        same_month_end = same_month_end.replace(month=same_month_end.month+1, day=1) - timedelta(days=1)

    # 翌月・翌々月（anchor基準）
    anchor_month     = anchor.replace(day=1)
    next_month       = (anchor_month.replace(month=anchor_month.month+1)
                        if anchor_month.month < 12
                        else anchor_month.replace(year=anchor_month.year+1, month=1))
    after_next       = (next_month.replace(month=next_month.month+1)
                        if next_month.month < 12
                        else next_month.replace(year=next_month.year+1, month=1))
    after_after_next = (after_next.replace(month=after_next.month+1)
                        if after_next.month < 12
                        else after_next.replace(year=after_next.year+1, month=1))

    month_hint = {}
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT
              SUM(event_end_date > %s AND event_start_date <= %s) AS same_cnt,
              SUM(event_end_date >= %s AND event_start_date < %s) AS next_cnt,
              SUM(event_end_date >= %s AND event_start_date < %s) AS after_cnt
            FROM ekinaka_events
            WHERE status IN ('approved','cancelled','postponed')
              AND COALESCE(approver_public_date, applicant_public_date) <= %s
        """, (period_end, same_month_end,
              next_month, after_next,
              after_next, after_after_next,
              today))
        row = cur.fetchone()
        if row and (row['same_cnt'] or 0) > 0 and period_end.month == anchor_month.month:
            month_hint['same'] = anchor_month
        if row and (row['next_cnt'] or 0) > 0:
            month_hint['this'] = next_month
        if row and (row['after_cnt'] or 0) > 0:
            month_hint['next'] = after_next
    except Exception:
        pass
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    return render_template('ekinaka_public.html',
        events=events, past=past,
        cal_weeks=cal_weeks,
        mode=mode, anchor=anchor,
        period_label=period_label,
        prev_anchor=prev_anchor, next_anchor=next_anchor,
        today=today,
        uid=None,
        apply_ok=False,
        approve_ok=False,
        month_hint=month_hint
    )

@ekinaka_bp.route('/public/event/<int:eid>')
def public_event_detail(eid):
    """閲覧専用イベント詳細。セッション無視で一般公開情報のみ。"""
    _log_access('public_event_detail', ref_event_id=eid)  # ← 追加
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT e.*
            FROM ekinaka_events e
            WHERE e.id = %s
        """, (eid,))
        event = cur.fetchone()
        if not event:
            abort(404)
        cur.execute("""
            SELECT * FROM ekinaka_performances
            WHERE event_id = %s ORDER BY start_datetime
        """, (eid,))
        performances = cur.fetchall()
    except Exception as e:
        abort(500)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    if not is_public_anonymous(event):
        abort(403)

    enrich(event)

    # 公演のMD変換
    for p in performances:
        try:
            p['note_html'] = process_markdown_for_preview(p['note']) if p['note'] else ''
        except Exception:
            p['note_html'] = p['note'] or ''

    # detail / summary / other_notes MD変換
    def _md(text):
        if not text:
            return ''
        try:
            return process_markdown_for_preview(text)
        except Exception:
            return text

    detail_html      = _md(event.get('detail'))
    summary_html     = _md(event.get('summary'))
    other_notes_html = _md(event.get('other_notes'))

    return render_template('ekinaka_public_event_detail.html',
        event=event,
        performances=performances,
        detail_html=detail_html,
        summary_html=summary_html,
        other_notes_html=other_notes_html
    )

# ─────────────────────────────────────────────────────────────────
# アクセスログ管理（admin専用）
# ─────────────────────────────────────────────────────────────────
@ekinaka_bp.route('/admin/access_log')
def access_log():
    if not is_admin_user():
        abort(403)

    # --- フィルタ ---
    page     = max(1, int(request.args.get('page', 1)))
    per_page = 50
    offset   = (page - 1) * per_page
    ep_filter = request.args.get('endpoint', '')

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)

        # サマリー統計
        cur.execute("""
            SELECT
              COUNT(*)                                        AS total,
              COUNT(DISTINCT DATE(accessed_at))              AS active_days,
              COUNT(DISTINCT user_id)                        AS unique_users,
              SUM(user_id IS NULL)                           AS anon_count,
              SUM(user_id IS NOT NULL)                       AS login_count
            FROM ekinaka_access_logs
        """)
        stats = cur.fetchone()

        # エンドポイント別集計
        cur.execute("""
            SELECT endpoint, COUNT(*) AS cnt
            FROM ekinaka_access_logs
            GROUP BY endpoint ORDER BY cnt DESC
        """)
        by_endpoint = cur.fetchall()

        # 直近7日 日別アクセス数
        cur.execute("""
            SELECT DATE(accessed_at) AS day, COUNT(*) AS cnt
            FROM ekinaka_access_logs
            WHERE accessed_at >= DATE_SUB(CURDATE(), INTERVAL 6 DAY)
            GROUP BY day ORDER BY day
        """)
        daily = cur.fetchall()

        # ログ一覧（ページング）
        where = "WHERE endpoint = %s" if ep_filter else ""
        params_count = (ep_filter,) if ep_filter else ()
        cur.execute(f"SELECT COUNT(*) AS n FROM ekinaka_access_logs {where}", params_count)
        total_rows = cur.fetchone()['n']

        params_list = (ep_filter, per_page, offset) if ep_filter else (per_page, offset)
        cur.execute(f"""
            SELECT l.*, u.full_name AS user_name
            FROM ekinaka_access_logs l
            LEFT JOIN users u ON u.id = l.user_id
            {where}
            ORDER BY l.accessed_at DESC
            LIMIT %s OFFSET %s
        """, params_list)
        logs = cur.fetchall()

    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
        stats, by_endpoint, daily, logs, total_rows = {}, [], [], [], 0
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    total_pages = max(1, (total_rows + per_page - 1) // per_page)

    return render_template('ekinaka_access_log.html',
        stats=stats,
        by_endpoint=by_endpoint,
        daily=daily,
        logs=logs,
        page=page,
        total_pages=total_pages,
        ep_filter=ep_filter,
        endpoints=['dashboard', 'public', 'event_detail', 'public_event_detail', 'display_serve'],
    )


@ekinaka_bp.route('/img/<path:filename>')
def image(filename):
    """イベント詳細の画像．公開ページからも参照されるのでログイン不要．
       名前はアップロード時刻つきで推測しにくい．アプリ配下に無ければ旧置き場を見る．"""
    name = secure_filename(filename)
    if not name or name != filename:
        abort(404)
    folders = [EKINAKA_IMG_DIR]
    try:
        if current_app.static_folder:
            folders.append(os.path.join(current_app.static_folder, EKINAKA_IMG_SUBDIR))
    except Exception:
        pass
    folders.append(os.path.join(os.path.expanduser('~'), 'static', EKINAKA_IMG_SUBDIR))
    for folder in folders:
        if os.path.isfile(os.path.join(folder, name)):
            return send_from_directory(folder, name)
    abort(404)


# 旧エンドポイント名 ekinaka.dashboard でも同じトップを開けるようにしておく（ブックマーク・旧ランチャ用）
ekinaka_bp.add_url_rule('/', endpoint='dashboard', view_func=index)
