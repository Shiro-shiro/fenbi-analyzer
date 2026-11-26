# -*- coding: utf-8 -*-
""" 用户设置：目标正确率/目标用时/每题分值（可编辑）、其它偏好
   v2.0：只保存用户真正改动的项，带配置版本，避免旧值长期覆盖默认。 """
import os, json
BASE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE, 'data', 'settings.json')
DEFAULT_FILE = os.path.join(BASE, 'data', 'targets.json')
WEIGHTS_FILE = os.path.join(BASE, 'data', 'mock_weights.json')
WEIGHTS_DEFAULT_FILE = os.path.join(BASE, 'data', 'mock_weights_default.json')
VERSION = 2


def load_defaults():
    return json.load(open(DEFAULT_FILE, encoding='utf-8'))


def load_settings():
    defaults = load_defaults()
    if os.path.exists(SETTINGS_FILE):
        try:
            saved = json.load(open(SETTINGS_FILE, encoding='utf-8'))
            # 只合并用户条目，忽略版本等元数据字段
            for k, v in saved.items():
                if k.startswith('_'): continue
                if isinstance(v, dict) and 'target_acc' in v:
                    defaults[k] = v
        except Exception:
            pass
    return defaults


def save_settings(d):
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    out = {}
    # 只存与默认不同的条目，避免旧值长期覆盖默认
    defaults = load_defaults()
    for k, v in d.items():
        if k in defaults and v == defaults[k]: continue
        out[k] = v
    out['_version'] = VERSION
    json.dump(out, open(SETTINGS_FILE, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)


def reset_settings():
    if os.path.exists(SETTINGS_FILE):
        os.remove(SETTINGS_FILE)
    return load_defaults()


# ---- 固定题量权重：平均出现题数/总题数130（比值形式，可在设置页改） ----
def load_weight_defaults():
    """从 2027 行政执法 4 份模考卷统计出的默认权重（平均出现题数/总题数130）。"""
    return json.load(open(WEIGHTS_DEFAULT_FILE, encoding='utf-8'))


def load_weights():
    """返回 {二级分类: {'count': 出现题数, 'total': 总题数}}。
       用户改动存 mock_weights.json，未改动的用默认值。 """
    defaults = load_weight_defaults()
    if os.path.exists(WEIGHTS_FILE):
        try:
            saved = json.load(open(WEIGHTS_FILE, encoding='utf-8'))
            for k, v in saved.items():
                if k.startswith('_'): continue
                if isinstance(v, dict) and 'count' in v and 'total' in v:
                    defaults[k] = v
        except Exception:
            pass
    return defaults


def save_weights(w):
    """只保存与默认不同的分类条目。"""
    os.makedirs(os.path.dirname(WEIGHTS_FILE), exist_ok=True)
    defaults = load_weight_defaults()
    out = {}
    for k, v in w.items():
        if k.startswith('_'): continue
        if k in defaults and v == defaults[k]: continue
        out[k] = v
    out['_version'] = VERSION
    json.dump(out, open(WEIGHTS_FILE, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)


def reset_weights():
    if os.path.exists(WEIGHTS_FILE):
        os.remove(WEIGHTS_FILE)
    return load_weight_defaults()


def weights_to_ratios(w):
    """转成 {二级分类: 权重小数} 供 build_ranking 使用。"""
    out = {}
    for k, v in w.items():
        if k.startswith('_'): continue
        try:
            total = float(v.get('total') or 0)
            out[k] = (float(v.get('count') or 0) / total) if total else 0.0
        except (TypeError, ValueError):
            out[k] = 0.0
    return out
