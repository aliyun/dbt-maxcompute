import time
import functools
from typing import Callable, Optional, TypeVar

from dbt.adapters.events.logging import AdapterLogger
from odps.errors import ODPSError, NoSuchObject

from pathlib import Path

# used for this adapter's version and in determining the compatible dbt-core version
VERSION = Path(__file__).parent / "__version__.py"


def _dbt_maxcompute_version() -> str:
    """
    Pull the package version from the main package version file
    """
    attributes: dict[str, str] = {}
    exec(VERSION.read_text(), attributes)
    return attributes["version"]


# MaxCompute 的字符串字面量用反斜杠转义。要转的字符集与 PyODPS 自己拼 DDL 时用的
# odps.utils.escape_odps_string 一致：只转单引号会把文本里的反斜杠当成转义符吃掉
# （描述写成 back\slash 会存成 backslash），以反斜杠结尾时还会吞掉收尾引号。
_LITERAL_ESCAPES = {
    "\\": "\\\\",
    "'": "\\'",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\0": "\\0",
}

# 元数据接口回读时的反转义表（上面那张表的逆；未识别的序列原样保留）。
_META_UNESCAPES = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "b": "\b",
    "0": "\0",
    "'": "'",
    '"': '"',
    "\\": "\\",
}


def quote_string(value: Optional[str]) -> str:
    """Render `value` as a MaxCompute string literal."""
    if value is None:
        raise ValueError(
            "quote_string() needs a string; an absent comment must be skipped by the caller"
        )
    return "'" + "".join(_LITERAL_ESCAPES.get(ch, ch) for ch in value) + "'"


def unescape_meta_comment(value: Optional[str]) -> Optional[str]:
    r"""还原元数据接口返回的表级注释，使它与用户写下的描述逐字相等。

    实测（见工作项 record.md 与 evidence/raw/probe-comment-*）：GetTable 以转义形态
    返回**表级**注释（`"` 变成 `\"`、真换行变成两字符的 `\n`、`\` 变成 `\\`），而
    **列**注释原样返回。不还原就会把转义垃圾写进 catalog.json 与 docs 页面，也会让
    物化视图的配置比对每轮都以为注释变了。空串按"没有注释"处理——dbt 的 catalog
    契约用 None 表示缺失。
    """
    if not value:
        return None
    chars = []
    index = 0
    length = len(value)
    while index < length:
        ch = value[index]
        if ch == "\\" and index + 1 < length:
            following = value[index + 1]
            chars.append(_META_UNESCAPES.get(following, ch + following))
            index += 2
            continue
        chars.append(ch)
        index += 1
    return "".join(chars)


def quote_ref(value: str) -> str:
    value = value.replace("`", "``")
    return f"`{value}`"


def is_schema_not_found(e: ODPSError) -> bool:
    if isinstance(e, NoSuchObject):
        return True
    if "ODPS-0110061" in str(e):
        return True
    if "ODPS-0422155" in str(e):
        return True
    if "ODPS-0420111" in str(e):
        return True
    return False


logger = AdapterLogger("MaxCompute")

T = TypeVar("T")


def retry_on_exception(max_retries=3, delay=1, backoff=2, exceptions=(Exception,), condition=None):
    """
    Decorator for retrying a function if it throws an exception.

    :param max_retries: Maximum number of retries before giving up.
    :param delay: Initial delay between retries in seconds.
    :param backoff: Multiplier applied to delay between retries.
    :param exceptions: Tuple of exceptions to catch. Defaults to base Exception.
    :param condition: Optional function to determine if the exception should trigger a retry.
    """

    def decorator_retry(func):
        @functools.wraps(func)
        def wrapper_retry(*args, **kwargs):
            mtries, mdelay = max_retries, delay
            while mtries > 1:
                try:
                    return func(*args, **kwargs)
                except exceptions as ex:
                    if condition is not None and not condition(ex):
                        raise
                    logger.warning(f"{str(ex)}, Retrying in {mdelay} seconds...")
                    time.sleep(mdelay)
                    mtries -= 1
                    mdelay *= backoff
            return func(*args, **kwargs)

        return wrapper_retry

    return decorator_retry


@retry_on_exception(max_retries=3, delay=0.5, backoff=2, exceptions=(OSError,))
def retry_on_transport_error(operation: Callable[[], T]) -> T:
    """Retry a single idempotent ODPS cleanup operation after transport failures."""

    return operation()
