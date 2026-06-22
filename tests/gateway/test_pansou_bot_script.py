from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def load_pansou_bot():
    path = Path('/root/.hermes/skills/devops/wechat-integration/scripts/pansou_bot.py')
    spec = importlib.util.spec_from_file_location('pansou_bot_under_test', path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_standalone_pansou_search_only_keeps_allowed_pan_types(monkeypatch):
    pansou_bot = load_pansou_bot()
    merged = {
        'aliyun': [{'note': '封神演义 阿里', 'url': 'https://aliyun.example/1'}],
        'xunlei': [{'note': '封神演义 迅雷', 'url': 'https://xunlei.example/1'}],
        '115': [{'note': '封神演义 115', 'url': 'https://115.example/1'}],
        'magnet': [{'note': '封神演义 磁力', 'url': 'magnet:?xt=urn:btih:1'}],
        'mobile': [{'note': '封神演义 移动', 'url': 'https://mobile.example/1'}],
        'baidu': [{'note': '封神演义 百度', 'url': 'https://baidu.example/1'}],
        'quark': [{'note': '封神演义 夸克', 'url': 'https://quark.example/1'}],
        'uc': [{'note': '封神演义 UC', 'url': 'https://uc.example/1'}],
        '123': [{'note': '封神演义 123', 'url': 'https://123.example/1'}],
        'others': [{'note': '封神演义 其他', 'url': 'https://others.example/1'}],
    }

    class FakeResponse:
        text = json.dumps({'code': 0, 'data': {'merged_by_type': merged}})

        def raise_for_status(self):
            return None

    monkeypatch.setattr(pansou_bot.requests, 'get', lambda *_args, **_kwargs: FakeResponse())

    results = pansou_bot.search_pansou('封神演义')
    sources = {result['source'] for result in results}
    assert sources == {'quark', '115', 'baidu', 'uc', 'magnet'}
    assert 'aliyun' not in sources
    assert 'xunlei' not in sources
    assert 'mobile' not in sources
    assert '123' not in sources
    assert 'others' not in sources
