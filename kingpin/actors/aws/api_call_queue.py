from boto import exception as boto_exception
from botocore import exceptions as botocore_exceptions
from tornado import concurrent
from tornado import gen
from tornado import queues
from tornado import ioloop

from kingpin.actors import exceptions

EXECUTOR = concurrent.futures.ThreadPoolExecutor(10)


class ApiCallQueue:
    """
    Handles queueing up and sending AWS api calls serially,
    with exponential backoff when there is throttling.

    Supports both boto2 and boto3.
    """

    def __init__(self):
        self.executor = EXECUTOR

        self._queue = queues.Queue()
        ioloop.IOLoop.current().spawn_callback(self._process_queue)

        # Used for controlling how fast the work queue is processed,
        # with exponential delay on throttling errors.
        self.delay_min = 0.25
        self.delay_max = 30
        # We don't have a delay until we first get throttled.
        self.delay = 0

        # There are a number of different rate limiting messages
        # boto2 can return when rate limits are reached, depending
        # on which apis are used.
        self.boto2_throttle_strings = (
            'Throttling',
            'Rate exceeded',
            'reached max retries',
        )

    @gen.coroutine
    def call(self, api_function, *args, **kwargs):
        result_queue = queues.Queue(maxsize=1)
        yield self._queue.put((result_queue, api_function, args, kwargs))
        result = yield result_queue.get()
        if isinstance(result, Exception):
            raise result
        raise gen.Return(result)

    @gen.coroutine
    def _process_queue(self):
        while True:
            result_queue, api_function, args, kwargs = yield self._queue.get()
            try:
                result = yield self._call(api_function, *args, **kwargs)
            except BaseException as e:
                result = e
            yield result_queue.put(result)
            if self.delay > 0:
                yield gen.sleep(self.delay)

    @gen.coroutine
    def _call(self, api_function, *args, **kwargs):
        while True:
            try:
                result = yield self._thread(
                    api_function, *args, **kwargs)
                self.decrease_delay()
                raise gen.Return(result)
            except boto_exception.BotoServerError as e:
                # Boto2 exception.
                if e.error_code in self.boto2_throttle_strings:
                    self.increase_delay()
                    yield gen.sleep(self.delay)
                    continue

                # If we're using temporary IAM credentials, when those expire
                # we can get back a blank 400 from Amazon. This is confusing,
                # but it happens because of
                # https://github.com/boto/boto/issues/898.
                # In most cases, these temporary IAM creds can be re-loaded by
                # reaching out to the AWS API (for example, if we're using an
                # IAM Instance Profile role), so thats what Boto tries to do.
                # However, if you're using short-term creds (say from SAML
                # auth'd logins), then this fails and Boto returns a blank
                # 400.
                if (e.status == 400 and
                        e.reason == 'Bad Request' and
                        e.error_code is None):
                    msg = 'Access credentials have expired'
                    e = exceptions.InvalidCredentials(msg)
                elif e.status == 403:
                    msg = '%s: %s' % (e.error_code, e.message)
                    e = exceptions.InvalidCredentials(msg)

                self.decrease_delay()
                raise e
            except botocore_exceptions.ClientError as e:
                # Boto3 exception.
                if e.response['Error']['Code'] == 'Throttling':
                    self.increase_delay()
                    yield gen.sleep(self.delay)
                    continue

                self.decrease_delay()
                raise e

    def decrease_delay(self):
        if self.delay == 0:
            return
        if self.delay == self.delay_min:
            self.delay = 0
            return
        self.delay /= 2
        self.delay = max(self.delay, self.delay_min)

    def increase_delay(self):
        if self.delay == 0:
            self.delay = self.delay_min
            return
        self.delay *= 2
        self.delay = min(self.delay, self.delay_max)

    @concurrent.run_on_executor
    def _thread(self, function, *args, **kwargs):
        """Execute `function` in a concurrent thread.

        This allows execution of any function in a thread without having
        to write a wrapper method that is decorated with run_on_executor().
        """
        return function(*args, **kwargs)