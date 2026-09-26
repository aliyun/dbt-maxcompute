"""注释文本进出 MaxCompute 的编解码规则（离线，不需要云端凭据）。

规则来自真实服务端的回读，功能用例
tests/functional/maxcompute/test_docs_comments.py 做端到端往返断言；
这里锁住纯函数部分，让 CI 在没有凭据的 PR 上也能挡住转义回归。
"""

import pytest

from dbt.adapters.maxcompute.utils import quote_string, unescape_meta_comment

# 覆盖：中文、双引号、单引号、真换行、制表符、反斜杠（含结尾反斜杠）、
# 注释符号、百分号、dollar-quoting 形态。
SAMPLES = [
    "plain",
    "中文备注",
    'double "quotes" inside',
    "single 'quotes' inside",
    "line1\nline2",
    "tab\there",
    "back\\slash in the middle",
    "trailing backslash\\",
    "80% and -- dash and /* star */",
    "$lbl$ labeled $lbl$ and $$ unlabeled $$",
    "'''abc123'''",
    "\\0 like nul",
]


def _maxcompute_meta_escaping(text):
    """服务端元数据接口对**表级**注释做的转义（实测形态）。

    与 odps.utils.escape_odps_string 同一套字符集：反斜杠本身最先处理。
    """
    table = {
        "\\": "\\\\",
        '"': '\\"',
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
    }
    return "".join(table.get(ch, ch) for ch in text)


def _maxcompute_literal_parsing(literal_body):
    """MaxCompute 解析字符串字面量时的反转义（_maxcompute_meta_escaping 的镜像）。"""
    out = []
    index = 0
    while index < len(literal_body):
        ch = literal_body[index]
        if ch == "\\" and index + 1 < len(literal_body):
            following = literal_body[index + 1]
            out.append(
                {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "0": "\0"}.get(following, following)
            )
            index += 2
            continue
        out.append(ch)
        index += 1
    return "".join(out)


@pytest.mark.parametrize("text", SAMPLES)
def test_quoted_literal_survives_the_servers_unescaping(text):
    """写出去的字段面值，被服务端解析后必须逐字等于原文。

    只转单引号的旧实现会吃掉文本里的反斜杠（"back\\slash" 存成 "backslash"），
    结尾反斜杠还会把收尾引号吞掉。
    """
    literal = quote_string(text)
    assert literal[0] == "'" and literal[-1] == "'"
    assert _maxcompute_literal_parsing(literal[1:-1]) == text


@pytest.mark.parametrize("text", SAMPLES)
def test_literal_body_keeps_sql_on_one_line(text):
    """真换行/制表符不能裸着进 SQL：字面量体内只允许两字符的转义序列。"""
    body = quote_string(text)[1:-1]
    for raw in ("\n", "\r", "\t"):
        assert raw not in body


@pytest.mark.parametrize("text", SAMPLES)
def test_escaping_matches_what_pyodps_generates(text):
    """转义字符集与 PyODPS 自己拼 DDL 时用的是同一套，避免两套规则各转一半。"""
    from odps.utils import escape_odps_string

    assert quote_string(text) == f"'{escape_odps_string(text)}'"


@pytest.mark.parametrize("text", SAMPLES)
def test_meta_comment_round_trip_is_exact(text):
    """服务端把表注释转义回来读，解码后必须还原成用户写下的文本。"""
    assert unescape_meta_comment(_maxcompute_meta_escaping(text)) == text


@pytest.mark.parametrize("value", [None, ""])
def test_absent_meta_comment_is_none(value):
    """dbt 的 catalog 契约用 None 表示"没有注释"，空串不是另一种注释。"""
    assert unescape_meta_comment(value) is None


def test_unknown_escape_is_preserved_not_dropped():
    """认不出的序列原样保留：宁可看着奇怪，也不悄悄丢字符。"""
    assert unescape_meta_comment("keep \\x sequence") == "keep \\x sequence"
    assert unescape_meta_comment("ends with backslash \\") == "ends with backslash \\"


def test_quote_string_refuses_none():
    """None 不能变成字面上的 'None' 落进元数据。"""
    with pytest.raises(ValueError):
        quote_string(None)
