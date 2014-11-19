# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Copyright 2013 Nextdoor.com, Inc.

"""
Common package for utility functions.
"""

__author__ = 'Matt Wise (matt@nextdoor.com)'

from logging import handlers
import datetime
import demjson
import functools
import logging
import os
import re
import sys
import time
import traceback

from tornado import gen
from tornado import ioloop
import httplib
import rainbow_logging_handler

from kingpin import exceptions

log = logging.getLogger(__name__)

# Constants for some of the utilities below
STATIC_PATH_NAME = 'static'

# Disable the global threadpool defined here to try to narrow down the random
# unit test failures regarding the IOError. Instead, instantiating a new
# threadpool object for every thread using the 'with' context below.
#
# # Allow up to 10 threads to be executed at once. This is arbitrary, but we
# # want to prvent the app from going thread-crazy.
# THREADPOOL_SIZE = 10
# THREADPOOL = futures.ThreadPoolExecutor(THREADPOOL_SIZE)


def str_to_class(string):
    """Method that converts a string name into a usable Class name

    This is used to take the 'actor' value from the JSON object and convert it
    into a valid object reference.

    Args:
        cls: String name of the wanted class and package.
             eg: kingpin.actor.foo.bar

    Returns:
        A reference to the actual Class to be instantiated
    """
    # Split the string up. The last element is the Class, the rest is
    # the package name.
    string_elements = string.split('.')
    class_name = string_elements.pop()
    module_name = '.'.join(string_elements)

    # load the module, will raise ImportError if module cannot be loaded
    m = __import__(module_name, globals(), locals(), class_name)
    # get the class, will raise AttributeError if class cannot be found
    c = getattr(m, class_name)

    return c


def setup_root_logger(level='warn', syslog=None, color=False):
    """Configures the root logger.

    Args:
        level: Logging level string ('warn' is default)
        syslog: String representing syslog facility to output to.
                If empty, logs are written to console.
        color: Colorize the log output

    Returns:
        A root Logger object
    """

    # Get the logging level string -> object
    level = 'logging.%s' % level.upper()
    level_obj = str_to_class(level)

    # Get our logger
    logger = logging.getLogger()

    # Set our default logging level
    logger.setLevel(level_obj)

    # Set the default logging handler to stream to console..
    if color:
        # Patch the handler's 'is_tty()' method to return True. If the user
        # asked for color, we give them color. The is_tty() method calls the
        # sys.stdout.isatty() method and then refuses to give color output on
        # platforms like Jenkins, where this code is likely to be run.
        rainbow_logging_handler.RainbowLoggingHandler.is_tty = True

        handler = rainbow_logging_handler.RainbowLoggingHandler(
            sys.stdout,

            # Disable colorization of the 'info' log statements. If the code is
            # run in an environment like Jenkins, the background is white, and
            # we don't want to force these log lines to be white as well.
            color_message_info=(None, None, False)
        )
    else:
        handler = logging.StreamHandler()

    # Get our PID .. used below in the log line format.
    details = ''
    if level_obj <= 10:
        details = str(os.getpid()) + ' [%(name)-40s] [%(funcName)-20s]'

    # If syslog enabled, then override the logging handler to go to syslog.
    asctime = '%(asctime)-10s '
    if syslog is not None:
        asctime = ''
        handler = handlers.SysLogHandler(address=('127.0.0.1', 514),
                                         facility=syslog)

    fmt = asctime + '%(levelname)-8s ' + details + ' %(message)s'
    formatter = logging.Formatter(fmt)

    # Append the formatter to the handler, then set the handler as our default
    # handler for the root logger.
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


def super_httplib_debug_logging():
    """Enables DEBUG logging deep in HTTPLIB.

    HTTPLib by default doens't log out things like the raw HTTP headers,
    cookies, response body, etc -- even when your main logger is in DEBUG mode.
    This is because its a security risk, as well as just highly verbose.

    For the purposes of debugging though, this can be useful. This method
    enables deep debug logging of the HTTPLib web requests. This is highly
    insecure, but very useful when troubleshooting failures with remote API
    endpoints.

    Returns:
        Requests 'logger' object (mainly for unit testing)
    """
    httplib.HTTPConnection.debuglevel = 1
    requests_log = logging.getLogger("requests.packages.urllib3")
    requests_log.propagate = True
    requests_log.setLevel(logging.DEBUG)
    return requests_log


def exception_logger(func):
    """Explicitly log Exceptions then Raise them.

    Logging Exceptions and Tracebacks while inside of a thread is broken in the
    Tornado futures package for Python 2.7. It swallows most of the traceback
    and only gives you the raw exception object. This little helper method
    allows us to throw a log entry with the full traceback before raising the
    exception.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            log.error('Exception caught in %s(%s, %s): %s' %
                      (func, args, kwargs, e))
            log.error(traceback.format_exc())
            raise
    return wrapper


def retry(excs, retries=3, delay=0.25):
    """Coroutine-compatible Retry Decorator.

    This decorator provides a simple retry mechanism that looks for a
    particular set of exceptions and retries async tasks in the event that
    those exceptions were caught.

    Example usage:
        >>> @gen.coroutine
        ... @retry(excs=(Exception), retries=3)
        ... def login(self):
        ...     raise gen.Return()

    Args:
        excs: A single (or tuple) exception type to catch.
        retries: The number of times to try the operation in total.
        delay: Time (in seconds) to wait between retries
    """
    def _retry_on_exc(f):
        def wrapper(*args, **kwargs):
            i = 1
            while True:
                try:
                    log.debug('Try (%s/%s) of %s(%s, %s)' %
                              (i, retries, f, args, kwargs))
                    ret = yield gen.coroutine(f)(*args, **kwargs)
                    log.debug('Result: %s' % ret)
                    raise gen.Return(ret)
                except excs as e:
                    log.error('Exception raised on try %s: %s' % (i, e))

                    if i >= retries:
                        log.debug('Raising exception: %s' % e)
                        raise e

                    i += 1
                    log.debug('Retrying in %s...' % delay)
                    yield tornado_sleep(delay)
                log.debug('Retrying..')
        return wrapper
    return _retry_on_exc


@gen.coroutine
def tornado_sleep(seconds=1.0):
    """Async method equivalent to sleeping.

    Args:
        seconds: Float seconds. Default 1.0
    """
    yield gen.Task(ioloop.IOLoop.current().add_timeout,
                   time.time() + seconds)


def populate_with_env(string):
    """Insert env variables into the string.

    Will match any environment key wrapped in '%'s and replace it with the
    value of that env var.

    Example:
        export ME=biz

        string='foo %ME% %bar%'
        populate_with_env(string)  # 'foo biz %bar%'
    """

    # First things first, swap out all instances of %<str>% with any matching
    # environment variables found in os.environ.
    for k, v in os.environ.iteritems():
        string = string.replace(('%%%s%%' % k), v)

    # Now, see if we missed anything. If we did, raise an exception and fail.
    missed_tokens = list(set(re.findall(r'%[\w]+%', string)))
    if missed_tokens:
        raise exceptions.InvalidEnvironment(
            'Found un-matched tokens in JSON string: %s' % missed_tokens)

    return string


def convert_json_to_dict(json_file):
    """Converts a JSON file to a config dict.

    Reads in a JSON file, swaps out any environment variables that
    have been used inside the JSON, and then returns a dictionary.

    Args:
        json_file: Path to the JSON file to import

    Returns:
        <Dictonary of Config Data>
    """
    raw = open(json_file).read()
    parsed = populate_with_env(raw)
    return demjson.decode(parsed)

def create_repeating_log(logger, message, handle=None, **kwargs):
    """Create a repeating log message.

    Args:
        message: String to pass to log.info()
        **kwargs: values accepted by datetime.timedelta
                  namely seconds, and milliseconds.

    Must be cleared via _clear_repeating_log()
    Only handles one interval per actor.
    """

    class OpaqueHandle(object):
        """Tornado async io handler."""
        def __init__(self):
            self.timeout_id = None

    if not handle:
        handle = OpaqueHandle()

    def log_and_queue():
        logger(message)
        create_repeating_log(logger, message, handle, **kwargs)

    deadline = datetime.timedelta(**kwargs)
    # Here we only queue the call, we don't want to wait on it!
    timeout_id = ioloop.IOLoop.current().add_timeout(deadline, log_and_queue)
    handle.timeout_id = timeout_id

    return handle

def clear_repeating_log(handle):
    """Stops the timeout function from being called."""
    ioloop.IOLoop.current().remove_timeout(handle.timeout_id)
