import time
import functools

from odps.errors import ODPSError, NoSuchObject


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
                    msg = "%s, Retrying in %d seconds..." % (str(ex), mdelay)
                    print(msg)
                    time.sleep(mdelay)
                    mtries -= 1
                    mdelay *= backoff
            return func(*args, **kwargs)

        return wrapper_retry

    return decorator_retry
