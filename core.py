# -*- coding: utf-8 -*-
""" 粉笔抓取核心：请求、历史分页、三类筛选、逐场明细、断点续传、榜单计算
   v2.0 审计修复版：统一数据层级、请求状态化、失败清单、未分类明示、
   题量权重（固定：2027行政执法模考各题型出现题数/总题数，设置页可改）、优先级得分、用时口径分离。 """
import os, sys, json, re, time, datetime, threading
from collections import defaultdict
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, 'data')
API = 'https://tiku.fenbi.com'
COMMON = 'kav=125&av=127&hav=125&app=web&apcid=0&deviceId=v1_3a2g6g39aiqq8_d51a&gav=2'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36'
TYPE_NAMES = {1:'单选题',2:'多选题',3:'不定项选择题',4:'填空题',5:'判断题',6:'阅读理解(5选7)',
              11:'论证/证明题',12:'主观题/作文',13:'计算题',14:'资料分析/阅读理解',15:'分析题',
              16:'改错题',50:'其他',61:'填空题'}


class Cancelled(Exception): pass

class LoginExpired(Exception): pass

class FetchFailed(Exception): pass


def load_json(path, default=None):
    if os.path.exists(path):
        try: return json.load(open(path, encoding='utf-8'))
        except Exception: pass
    return default


def atomic_dump(obj, path):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def build_kp_index(tree_json):
    """ 从官方分类树 JSON 建索引，供实时把题目知识点归到 一级/二级 分类。
        返回 (id->路径, 名字->[路径...])。
        官方树每个节点都有唯一 id，题目知识点也带 id，两者直接对应，
        比按名字匹配可靠：像「特殊问法」在片段阅读和定义判断下各有一个同名节点，
        只有 id 才能区分。 """
    data = tree_json.get('data', tree_json) if isinstance(tree_json, dict) else {}
    roots = data.get('baseKeypointVOS') or data.get('baseKeypoints') or []
    id2path, name2paths = {}, defaultdict(list)

    def walk(ns, path):
        for n in ns or []:
            i, nm = n.get('id'), n.get('name') or ''
            if i is None or not nm:
                continue
            p = path + [nm]
            id2path[i] = p
            name2paths[nm].append(p)
            walk(n.get('children'), p)
    walk(roots, [])
    return id2path, name2paths


def resolve_cat(kp_list, kp_index, fallback_index=None):
    """ 题目知识点 -> (一级, 二级, 完整路径, 是否应跳过)。
        kp_list 是 [{id, name}, ...]，kp_index 是 build_kp_index 的返回值。
        优先用 id 精确匹配；id 不在树里时退回名字，但只接受树里唯一的名字
        （重名节点不猜，宁可标未分类，也不静默归错）。
        一道题带多个知识点时取最深（最具体）的那个作考点。
        fallback_index 传国考树索引：当前树里找不到、但国考树里有时，
        说明这是国考独有分类，返回 skip=True，由上层按广西模式不统计。 """
    id2path, name2paths = kp_index

    def pick(index):
        id2p, n2p = index
        best = None
        for k in kp_list or []:
            i, nm = k.get('id'), k.get('name')
            p = id2p.get(i)
            if p is None and nm:
                cands = n2p.get(nm) or []
                if len(cands) == 1:
                    p = cands[0]
            if not p or len(p) < 2:
                continue
            cand = (len(p), p[0], p[1], tuple(p))
            if best is None or cand[0] > best[0]:
                best = cand
        return best

    best = pick(kp_index)
    if best:
        return best[1], best[2], best[3], False
    if fallback_index is not None and pick(fallback_index):
        return None, None, None, True
    return None, None, None, False
    return None, None, False


class FenbiClient:
    """ getj 返回 (state, data)：
        state ∈ {'ok','empty','login_expired','failed','rate_limited'}
        data 为解析后的 JSON（ok/empty 时有效）或 None。 """

    def __init__(self, cookie_header, cancel_event=None, log=None):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': UA, 'Accept': 'application/json',
                                     'Cookie': cookie_header,
                                     'Referer': 'https://spa.fenbi.com/'})
        self.cancel = cancel_event or threading.Event()
        self.log = log or (lambda s: None)
        # 请求统计：排查时用来判断是网络问题、风控还是接口改版
        self.stats = defaultdict(int)
        self.slowest = []          # [(秒, path)] 最慢请求，排查超时用
        self.cookie_len = len(str(cookie_header or ''))

    def getj(self, path, retry=2, timeout=25):
        last_state = 'failed'
        short = path.split('?')[0]
        for i in range(retry + 1):
            if self.cancel.is_set(): raise Cancelled()
            t0 = time.time()
            try:
                r = self.session.get(API + path, timeout=timeout)
                el = time.time() - t0
                self.stats['req'] += 1
                self.stats['bytes'] += len(r.content or b'')
                self._note_slow(el, short)
                if r.status_code in (401, 403):
                    self.stats['http_%d' % r.status_code] += 1
                    self.log('[请求] %s -> HTTP %d（登录态失效或风控）耗时%.2fs' % (short, r.status_code, el))
                    return 'login_expired', None
                if r.status_code == 429:
                    self.stats['http_429'] += 1
                    last_state = 'rate_limited'
                    self.log('[请求] %s -> HTTP 429 限流，%.1fs 后重试(%d/%d)' % (short, 2 ** i, i + 1, retry + 1))
                    time.sleep(2 ** i)
                    continue
                if r.status_code >= 500:
                    self.stats['http_5xx'] += 1
                    last_state = 'failed'
                    self.log('[请求] %s -> HTTP %d，%.1fs 后重试(%d/%d)' % (short, r.status_code, 0.6, i + 1, retry + 1))
                    time.sleep(0.6)
                    continue
                if r.status_code != 200:
                    self.stats['http_other'] += 1
                    self.log('[请求] %s -> 非预期 HTTP %d，耗时%.2fs' % (short, r.status_code, el))
                t = r.text.strip()
                if not t:
                    self.stats['empty'] += 1
                    return 'empty', None
                if t[0] in '{[':
                    try:
                        d = json.loads(t)
                    except (ValueError, TypeError):
                        self.stats['bad_json'] += 1
                        last_state = 'failed'   # 非法JSON不当成功，走重试
                        self.log('[请求] %s -> JSON 解析失败，前80字：%s' % (short, t[:80]))
                        time.sleep(0.6)
                        continue
                    # 业务层登录失效标志
                    if isinstance(d, dict) and d.get('code') in (-1, 401, 1001, '401'):
                        msg = str(d.get('msg') or '')
                        if any(w in msg for w in ('登录', '凭证', '失效', '未登录')):
                            self.stats['biz_login_expired'] += 1
                            self.log('[请求] %s -> 业务层登录失效：%s' % (short, msg[:80]))
                            return 'login_expired', None
                    self.stats['ok'] += 1
                    return 'ok', d
                # 返回登录页/风控页 HTML
                if '登录' in t[:500] or 'login' in t[:500].lower():
                    self.stats['html_login'] += 1
                    self.log('[请求] %s -> 返回登录/风控页面（HTML），前80字：%s' % (short, t[:80]))
                    return 'login_expired', None
                self.stats['html_other'] += 1
                self.log('[请求] %s -> 非 JSON 响应，前80字：%s' % (short, t[:80]))
                last_state = 'failed'
            except requests.RequestException as e:
                el = time.time() - t0
                self.stats['net_error'] += 1
                self.log('[请求] %s -> 网络异常(%s) 耗时%.2fs，%.1fs 后重试(%d/%d)'
                         % (short, type(e).__name__, el, 0.6, i + 1, retry + 1))
                last_state = 'failed'
            time.sleep(0.6)
        self.stats['give_up'] += 1
        self.log('[请求] %s -> 重试%d次仍失败，放弃（状态=%s）' % (short, retry + 1, last_state))
        return last_state, None

    def _note_slow(self, el, short):
        """ 记录最慢的 5 个请求，日志里能看出是不是个别接口拖慢整体 """
        self.slowest.append((el, short))
        self.slowest.sort(key=lambda x: -x[0])
        del self.slowest[5:]

    def getj_or_none(self, path, retry=2, timeout=25):
        """ 兼容旧调用：只取 data，失败返回 None（调用方需自行检查） """
        st, d = self.getj(path, retry=retry, timeout=timeout)
        if st == 'login_expired': raise LoginExpired('登录已失效，请重新登录')
        return d if st == 'ok' else None

    def stats_text(self):
        """ 请求统计摘要（写进日志，供排查网络/风控/接口改版） """
        s = dict(self.stats)
        parts = ['请求总数=%d' % s.get('req', 0),
                 '成功=%d' % s.get('ok', 0),
                 '空响应=%d' % s.get('empty', 0),
                 '网络异常=%d' % s.get('net_error', 0),
                 'HTTP401/403=%d' % (s.get('http_401', 0) + s.get('http_403', 0)),
                 'HTTP429=%d' % s.get('http_429', 0),
                 'HTTP5xx=%d' % s.get('http_5xx', 0),
                 '非法JSON=%d' % s.get('bad_json', 0),
                 'HTML响应=%d' % (s.get('html_login', 0) + s.get('html_other', 0)),
                 '放弃=%d' % s.get('give_up', 0),
                 '下载=%.1fMB' % (s.get('bytes', 0) / 1048576.0)]
        return '请求统计：' + '｜'.join(parts)

    def fetch_history(self, category_id, stop_before=None, cache_file=None,
                      max_pages=200):
        """ 按 cursor 分页抓历史；history 按 updatedTime 降序。
            返回 (items, reached_boundary, incomplete, errors)
            incomplete=True 表示分页未走完（失败/达到上限），榜单必须标注数据不完整。 """
        cached = load_json(cache_file, None) if cache_file else None
        if cached is not None and not stop_before:
            return cached, True, False, []
        cursor = ''; items = []; reached = False; incomplete = False; errors = []
        seen_cursor = set()
        for page in range(max_pages):
            if self.cancel.is_set(): raise Cancelled()
            path = ('/combine/exercise/getExerciseBriefHistory?categoryId=%d&cursor=%s'
                    '&limit=15&routecs=xingce' % (category_id, requests.utils.quote(str(cursor))))
            st, d = self.getj(path)
            if st == 'login_expired': raise LoginExpired('登录已失效，请重新登录')
            if st != 'ok':
                incomplete = True
                errors.append({'page': page, 'state': st})
                self.log('历史第%d页请求失败(%s)，已停止' % (page + 1, st))
                break
            data = d.get('data') or {}
            chunk = data.get('historyItems') or data.get('items') or []
            if not chunk:
                reached = True
                break
            items += chunk
            self.log('[历史] cat%d 第%d页 %d 场，累计 %d 场，末场时间=%s'
                     % (category_id, page + 1, len(chunk), len(items),
                        time.strftime('%Y-%m-%d %H:%M', time.localtime(
                            (chunk[-1].get('updatedTime') or 0) / 1000))
                        if chunk[-1].get('updatedTime') else '未知'))
            if stop_before:
                last = chunk[-1].get('updatedTime') or 0
                if last and last < stop_before:
                    reached = True
                    break
            cursor = data.get('cursor', '')
            if not cursor:
                reached = True
                break
            if cursor in seen_cursor:
                incomplete = True
                errors.append({'page': page, 'state': 'cursor_repeat'})
                self.log('历史第%d页 cursor 重复，已停止' % (page + 1))
                break
            seen_cursor.add(cursor)
            time.sleep(0.1)
        else:
            incomplete = True
            errors.append({'page': max_pages, 'state': 'max_pages'})
            self.log('历史达到%d页上限，数据可能不完整' % max_pages)
        if cache_file and reached and not incomplete:
            atomic_dump(items, cache_file)
        return items, reached, incomplete, errors

    def fetch_report(self, eid):
        return self.getj('/api/xingce/exercises/%s/report/v2' % eid, retry=1)

    def fetch_solution(self, key):
        """ 统一返回包装结构 {'exerciseId':…,'key':…,'data':…}；
            调用方一律按 sol['data'] 取内容，消除层级歧义。 """
        st, d = self.getj('/combine/exercise/getSolution?format=json&key=%s&routecs=xingce' % key,
                          retry=1)
        if st == 'login_expired': raise LoginExpired('登录已失效，请重新登录')
        if st != 'ok' or not isinstance(d, dict): return None
        data = d.get('data') or None
        if not data: return None
        return {'data': data}

    def fetch_source(self, request_key):
        st, d = self.getj('/combine/static/solution?key=%s&routecs=xingce&type=1&%s'
                          % (request_key, COMMON), retry=1)
        if st == 'login_expired': raise LoginExpired('登录已失效，请重新登录')
        if st != 'ok' or not isinstance(d, dict): return []
        data = d.get('data') or d or {}
        return data.get('solutions') or []

    def fetch_meta(self, qids, progress=None):
        """ 批量补 meta，返回 (meta_dict, need_retry_qids) """
        meta = {}; failed = []
        for i in range(0, len(qids), 50):
            if self.cancel.is_set(): raise Cancelled()
            chunk = qids[i:i + 50]
            st, d = self.getj('/api/xingce/question/meta?ids=' + ','.join(chunk), retry=1)
            if st == 'login_expired': raise LoginExpired('登录已失效，请重新登录')
            items = []
            if st == 'ok':
                if isinstance(d, list):
                    items = d                      # 接口直接返回数组
                elif isinstance(d, dict):
                    items = d.get('data') or d.get('items') or []
            if not items:
                failed.extend(chunk)
            else:
                for x in items:
                    if isinstance(x, dict) and 'id' in x: meta[str(x['id'])] = x
            if progress: progress(i + len(chunk), len(qids))
            time.sleep(0.1)
        return meta, failed


def classify(sheet_type, name):
    name = str(name or '')
    if sheet_type in (1, 2): return '模考'
    if sheet_type == 151 or name.endswith('-背题'): return '背题'
    if sheet_type in (3, 63): return '专项'
    return None


def sheet_type_of(item):
    return item.get('sheetType') or item.get('type')


def year_from(s):
    m = re.search(r'((?:19|20)\d{2})', str(s or ''))
    return m.group(1) if m else None


def choice_to_letter(c):
    try: return 'ABCD'[int(c)]
    except Exception: return None


def clean_text(s):
    if not s: return ''
    return re.sub(r'<[^>]+>', '', str(s)).replace('&nbsp;', ' ').strip()[:120]


class Crawler:
    """ 协调抓取：历史→筛选场次→逐场 report/solution/source→落盘明细 jsonl。 """

    def __init__(self, client, cache_dir, log=None, cancel=None, progress=None):
        self.client = client; self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.log = log or (lambda s: None)
        self.cancel = cancel or threading.Event()
        self.progress = progress or (lambda done, total, msg: None)
        self.reports_file = os.path.join(cache_dir, 'reports.jsonl')
        self.sol_file = os.path.join(cache_dir, 'solution.jsonl')
        self.src_file = os.path.join(cache_dir, 'source.jsonl')
        self.meta_file = os.path.join(cache_dir, 'meta.json')
        self.status_file = os.path.join(cache_dir, 'fetch_status.jsonl')
        self._skipped_guangxi = 0
        self.incomplete = False
        self.summary = {}

    def _load_jsonl(self, path):
        d = {}
        if os.path.exists(path):
            for line in open(path, encoding='utf-8'):
                try:
                    o = json.loads(line); d[str(o.get('exerciseId'))] = o
                except Exception: pass
        return d

    def _append_jsonl(self, path, obj):
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(obj, ensure_ascii=False) + '\n')

    def run(self, kinds, days, force=False, exam_tree='guokao'):
        """ kinds: set of '专项','模考','背题'; days: None=全部, 否则最近N天(含今天)
            分类来源：官方分类树（data/categories_*.json）+ 每题自带的知识点 id，
            不依赖任何按题号生成的映射表。
            返回明细列表(每行 dict)，同时写 cache_dir/rows.jsonl。 """
        client = self.client
        t_start = time.time()
        self.log('===== 开始抓取 =====')
        self.log('运行环境：Python %s｜平台 %s｜程序目录 %s'
                 % (sys.version.split()[0], sys.platform, BASE))
        self.log('参数：类型=%s｜天数=%s｜分类树=%s｜强制刷新=%s｜缓存目录=%s'
                 % ('/'.join(sorted(kinds)) or '无', '全部' if not days else '最近%d天' % days,
                    exam_tree, force, self.cache_dir))
        self.log('登录态：Cookie 长度 %d 字节' % getattr(client, 'cookie_len', 0))
        now = datetime.datetime.now()
        start_ms = None
        if days:
            start = datetime.datetime(now.year, now.month, now.day) - datetime.timedelta(days=days - 1)
            start_ms = int(start.timestamp() * 1000)
            self.log('抓取范围：%s 00:00 起（最近%d天，含今天，按创建时间统计）'
                     % (start.strftime('%Y-%m-%d'), days))
        # 历史：记录不完整状态
        hist_incomplete = False
        items_all = {}
        def add_hist(cid):
            nonlocal hist_incomplete
            hist, _, inc, errs = client.fetch_history(
                cid, stop_before=start_ms,
                cache_file=None if start_ms else os.path.join(self.cache_dir, 'history%d.json' % cid))
            if inc: hist_incomplete = True; self.incomplete = True
            self.log('历史(cat%d) %d 场%s' % (cid, len(hist), '【不完整】' if inc else ''))
            for it in hist: items_all[str(it.get('exerciseId'))] = it
        if '模考' in kinds and (kinds & {'专项', '背题'}):
            add_hist(4)
        else:
            if kinds & {'专项', '背题'}: add_hist(3)
            if '模考' in kinds: add_hist(1)
        # 筛选
        targets = []
        for eid, it in items_all.items():
            st = sheet_type_of(it); name = it.get('sheetName') or it.get('name')
            k = classify(st, name)
            if k not in kinds: continue
            t = it.get('updatedTime') or 0
            if start_ms and (not t or t < start_ms): continue
            targets.append((eid, it))
        targets.sort(key=lambda x: -(x[1].get('updatedTime') or 0))
        self.log('筛选结果：%d 场（按类型=%s）' % (len(targets), '/'.join(sorted(kinds)) or '无'))
        if not targets:
            self.log('没有符合条件的场次，可能是时间范围太窄或该类型下没有记录')
            self.summary = dict(total_sessions=0, in_ranking=0, unclassified=0,
                                failed=0, empty_sessions=0, incomplete=hist_incomplete)
            return []

        rep_cache = self._load_jsonl(self.reports_file)
        sol_cache = self._load_jsonl(self.sol_file)
        src_cache = self._load_jsonl(self.src_file)
        self.log('本地缓存：报告 %d 场｜解题 %d 场｜题目来源 %d 场'
                 % (len(rep_cache), len(sol_cache), len(src_cache)))
        failures = []
        empty_sessions = 0
        for idx, (eid, it) in enumerate(targets):
            if self.cancel.is_set(): raise Cancelled()
            self.progress(idx, len(targets), '抓取场次 %d/%d' % (idx + 1, len(targets)))
            status = {'exerciseId': eid, 'name': it.get('sheetName') or it.get('name')}
            rep = rep_cache.get(eid)
            if not rep or force:
                st, d = client.fetch_report(eid)
                if st == 'login_expired': raise LoginExpired('登录已失效，请重新登录')
                if st == 'ok' and isinstance(d, dict) and d.get('exerciseId'):
                    rep = d; rep_cache[eid] = rep
                    self._append_jsonl(self.reports_file, rep)
                else:
                    status['report'] = st
                    self.log('[场次] %s「%s」报告获取失败：%s' % (eid, status['name'], st))
            if not rep:
                if status.get('report') not in ('empty',):
                    failures.append(status); self.incomplete = True
                elif status.get('report') == 'empty':
                    self.log('[场次] %s「%s」报告为空（通常是 0 题的背题占位，非错误）'
                             % (eid, status['name']))
                self._append_jsonl(self.status_file, status)
                continue
            created = rep.get('createdTime')
            if start_ms and (not created or created < start_ms): continue
            answers = rep.get('answers') or []
            if not answers:
                empty_sessions += 1
                status['answers'] = 'empty'
                self._append_jsonl(self.status_file, status)
                continue
            key = it.get('exerciseKey') or rep.get('exerciseKey')
            sol = sol_cache.get(eid)
            if (not sol or force) and key:
                sol = client.fetch_solution(key)   # 统一包装结构
                if sol:
                    sol_cache[eid] = dict(sol, exerciseId=eid, key=key)
                    self._append_jsonl(self.sol_file, sol_cache[eid])
                else:
                    status['solution'] = 'failed'
                    self.log('[场次] %s「%s」解题数据为空（接口返回 ok 但 data 为空）'
                             % (eid, status['name']))
            elif not key:
                status['exerciseKey'] = 'missing'
                self.log('[场次] %s「%s」缺少 exerciseKey，无法取逐题用时'
                         % (eid, status['name']))
            src = src_cache.get(eid)
            if (not src or force) and sol:
                # 统一从 sol['data'] 取 requestKey（层级修复核心）
                rk = ((sol.get('data') or {}).get('switchVO') or {}).get('requestKey')
                if rk:
                    solutions = client.fetch_source(rk)
                    if solutions:
                        src_cache[eid] = {'exerciseId': eid, 'solutions': solutions}
                        self._append_jsonl(self.src_file, src_cache[eid])
                    else:
                        status['source'] = 'failed'
                        self.log('[场次] %s「%s」题目来源为空' % (eid, status['name']))
                else:
                    status['requestKey'] = 'missing'
                    self.log('[场次] %s「%s」solution 里没有 requestKey（接口结构可能变更）'
                             % (eid, status['name']))
            if any(k in status for k in ('report', 'solution', 'source', 'requestKey')):
                failures.append(status); self.incomplete = True
            self._append_jsonl(self.status_file, status)

        # 组装答题明细
        qids = set()
        for eid, _ in targets:
            rep = rep_cache.get(eid) or {}
            for a in rep.get('answers') or []:
                if a.get('status') == 10: continue
                if a.get('questionId') is not None: qids.add(str(a['questionId']))
        meta = load_json(self.meta_file, {}) or {}
        need = [q for q in sorted(qids) if q not in meta]
        meta_missing = []
        if need:
            self.log('补抓题目元数据 %d 题' % len(need))
            got, meta_missing = client.fetch_meta(
                need, progress=lambda i, n: self.progress(i, n, '补抓题目元数据 %d/%d' % (i, n)))
            meta.update(got)
            atomic_dump(meta, self.meta_file)
        if meta_missing:
            self.log('题目元数据仍有 %d 题未取到（将明确标注）' % len(meta_missing))
            self.incomplete = True

        # 官方分类树：每题的知识点直接按 id 归到 一级/二级 分类（不再依赖题号映射表）
        if exam_tree == 'guangxi':
            tree_file = 'categories_guangxi.json'
            fb_file = 'categories_guokao.json'
        else:
            tree_file, fb_file = 'categories_guokao.json', None
        kp_index = build_kp_index(load_json(os.path.join(DATA_DIR, tree_file), {}) or {})
        fb_index = build_kp_index(load_json(os.path.join(DATA_DIR, fb_file), {}) or {}) if fb_file else None
        self.log('分类标准：%s（官方分类树 %d 个节点）'
                 % ('广西·B类' if exam_tree == 'guangxi' else '国考·行政执法类', len(kp_index[0])))
        rows = []
        unclassified = []
        status_counts = defaultdict(int)
        for eid, it in targets:
            rep = rep_cache.get(eid) or {}
            created = rep.get('createdTime')
            if start_ms and (not created or created < start_ms): continue
            dt = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(created / 1000)) if created else None
            name = rep.get('name') or it.get('sheetName') or it.get('name')
            kp_top = rep.get('keypoints') or []
            top_cat = kp_top[0].get('name') if kp_top and kp_top[0].get('name') else None
            if not top_cat:
                mm = re.search(r'（([^）]+)）', str(name or ''))
                top_cat = mm.group(1) if mm else None
            kind = classify(sheet_type_of(it), name)
            ua_map = {}
            sol = sol_cache.get(eid)
            if sol:
                for k, v in ((sol.get('data') or {}).get('userAnswers') or {}).items():
                    qid = v.get('id')
                    if qid is not None: ua_map[str(qid)] = v
            src_map = {}
            s = src_cache.get(eid)
            if s:
                for x in s.get('solutions') or []:
                    if 'id' in x: src_map[str(x['id'])] = x
            for a in rep.get('answers') or []:
                stt = a.get('status')
                status_counts[str(stt)] += 1
                if stt == 10: continue
                qid = a.get('questionId')
                if qid is None: continue
                sk = str(qid)
                m = meta.get(sk) or {}; q = src_map.get(sk) or {}; ua = ua_map.get(sk) or {}
                kp = q.get('keypoints') or []
                kp_names = [x.get('name') for x in kp if x.get('name')]
                kp_name = ' > '.join(kp_names) if kp_names else None
                source = q.get('source')
                ratio = m.get('correctRatio')
                l1, l2, l_path, skip = resolve_cat(kp, kp_index, fb_index)
                # 广西模式：国考独有分类题按用户要求不统计，明示数量
                if skip:
                    self._skipped_guangxi += 1
                    continue
                row = {
                    '练习ID': eid, '练习名称': name, '做题时间': dt, '类别': kind, '题目ID': qid,
                    '是否正确': '正确' if a.get('correct') else '错误',
                    '题目状态': {1: '已作答', -1: '答错', 0: '未作答/标记'}.get(stt, str(stt)),
                    '单题用时(秒)': ua.get('time'), '一级分类': top_cat, '细分题型': kp_name or top_cat,
                    '题目知识点': kp_name, '全站正确率%': round(ratio, 2) if isinstance(ratio, (int, float)) else None,
                    '原题年份': year_from(source), '题目来源': source,
                    '板块': l1, '题型': l2,
                    '用时来源': '接口' if ua.get('time') is not None else '缺失',
                }
                rows.append(row)
                if not l1:
                    # 未分类：明示并单列，绝不静默丢弃
                    unclassified.append(row)
        atomic_dump(rows, os.path.join(self.cache_dir, 'rows.jsonl'))
        atomic_dump(unclassified, os.path.join(self.cache_dir, 'unclassified.jsonl'))
        # 字段缺口统计：排查"为什么这一列是空的"
        miss_time = sum(1 for r in rows if r.get('单题用时(秒)') is None)
        miss_ratio = sum(1 for r in rows if r.get('全站正确率%') is None)
        miss_kp = sum(1 for r in rows if not r.get('题目知识点'))
        miss_src = sum(1 for r in rows if not r.get('题目来源'))
        self.log('字段缺口：用时缺 %d 条｜全站正确率缺 %d 条｜知识点缺 %d 条｜题目来源缺 %d 条'
                 % (miss_time, miss_ratio, miss_kp, miss_src))
        self.log('题目状态分布：%s' % '｜'.join(
            '%s=%d' % (k, v) for k, v in sorted(status_counts.items(), key=lambda x: -x[1])))
        if getattr(self.client, 'slowest', None):
            self.log('最慢请求：' + '｜'.join('%.2fs %s' % (s, p) for s, p in self.client.slowest))
        if hasattr(self.client, 'stats_text'):
            self.log(self.client.stats_text())
        self.summary = {
            'total_sessions': len(targets), 'in_ranking': len(rows) - len(unclassified),
            'unclassified': len(unclassified), 'failed': len(failures),
            'empty_sessions': empty_sessions, 'incomplete': self.incomplete or hist_incomplete,
            'skipped_guangxi': self._skipped_guangxi,
            'meta_missing': len(meta_missing),
            'status_counts': dict(status_counts), 'failures': failures,
            'exam_tree': exam_tree, 'days': days,
            'elapsed_sec': round(time.time() - t_start, 1),
        }
        atomic_dump(self.summary, os.path.join(self.cache_dir, 'fetch_summary.json'))
        extra = ('（广西模式：跳过%d条国考独有分类题）' % self._skipped_guangxi) \
            if exam_tree == 'guangxi' else ''
        self.log('完成，共 %d 条做题记录，其中未分类 %d 条（已单列明示）%s'
                 % (len(rows), len(unclassified), extra))
        self.log('总耗时 %.1f 秒' % (time.time() - t_start))
        if failures:
            self.log('失败场次 %d 场，明细：' % len(failures))
            for f in failures[:30]:
                self.log('  · %s「%s」%s' % (f.get('exerciseId'), f.get('name'),
                                             {k: v for k, v in f.items()
                                              if k not in ('exerciseId', 'name')}))
        if self.incomplete:
            self.log('【注意】本次抓取不完整，榜单数据可能缺失，请查看失败清单')
        return rows

# 板块顺序（粉笔口径一级分类）
BOARD_ORDER = ['言语理解与表达', '判断推理', '资料分析', '数量关系', '常识判断', '政治理论']


def _cell(row):
    """ 取官方一级/二级分类；二级缺失或与一级同名时归入'其他'（明示，不静默删除） """
    l1 = row.get('板块') or None
    l2 = row.get('题型') or None
    if l1 and (not l2 or l2 == l1): l2 = '其他'
    return l1, l2


def build_ranking(rows, targets, mock_weights=None, include_unclassified=True):
    """ 榜单按粉笔官方分类：板块=一级，题型=二级。
        优先级得分 = 原性价比 × (1 + 题量权重)。
        未分类题单列一组，明示数量，不静默丢弃。 """
    mock_weights = mock_weights or {}
    agg = defaultdict(lambda: [0, 0, 0.0, 0, 0])  # correct, total, time_sum, valid_time_cnt, zero_time_cnt
    src_map = {}
    for r in rows:
        if r.get('是否正确') not in ('正确', '错误'): continue
        if not include_unclassified and not (r.get('板块') or None): continue
        l1, l2 = _cell(r)
        if not l1: l1, l2 = '未分类', '未分类'
        key = (l1, l2)
        agg[key][1] += 1
        if r['是否正确'] == '正确': agg[key][0] += 1
        try:
            sec = float(r.get('单题用时(秒)'))
            agg[key][2] += sec
            agg[key][3] += 1
            if sec == 0: agg[key][4] += 1
        except (TypeError, ValueError):
            pass  # 缺失用时不当0，不参与平均
        # 目标来源追踪
        if l2 in targets: src_map[key] = '题型设置'
        elif l1 in targets: src_map[key] = '板块设置'
        else: src_map[key] = '未配置（用默认80%/60秒/0.8）'
    out = []
    for (l1, l2), (c, n, t, tv, z) in agg.items():
        tg = targets.get(l2) or targets.get(l1) or {}
        tp = float(tg.get('target_acc', 80))
        tt = float(tg.get('target_time', 60))
        score = float(tg.get('score', 0.8))
        acc = c / n * 100
        avg = t / tv if tv else 0.0
        gap = max(tp - acc, 0)
        tm = avg / tt if tt else 0
        base = gap * max(tm, 0.5)
        coef = score / 0.8
        weighted = base * coef
        w = mock_weights.get(l2, 0.0)
        priority = round(weighted * (1 + w), 1)
        out.append({
            '板块': l1, '题型': l2, '答对/答题': '%d/%d' % (c, n),
            '我的正确率%': round(acc, 1), '目标正确率%': tp,
            '我的用时(秒)': round(avg, 1), '目标用时(秒)': tt,
            '优先级得分': priority, '_目标来源': src_map.get((l1, l2), '未配置'),
            '_题量权重': round(w, 3), '_用时有效数': tv, '_用时0秒数': z,
        })
    out.sort(key=lambda x: -x['优先级得分'])
    return out
