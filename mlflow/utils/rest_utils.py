import base64
import time
import logging
import json
import requests
from contextlib import contextmanager
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from mlflow import __version__
from mlflow.protos import databricks_pb2
from mlflow.protos.databricks_pb2 import INVALID_PARAMETER_VALUE
from mlflow.utils.proto_json_utils import parse_dict
from mlflow.utils.string_utils import strip_suffix
from mlflow.exceptions import MlflowException, RestException

from requests_oauthlib import OAuth2Session
from oauthlib.oauth2 import BackendApplicationClient

_REST_API_PATH_PREFIX = "/api/2.0"
RESOURCE_DOES_NOT_EXIST = "RESOURCE_DOES_NOT_EXIST"

_logger = logging.getLogger(__name__)

_DEFAULT_HEADERS = {"User-Agent": "mlflow-python-client/%s" % __version__}


def get_client_credentials(host):
    """
    Get bearer token from an oath provider.
    :param host: object of class MlflowHostCreds
    :return: token from oath endpoint
    """
    data = json.loads(host.oath_kwargs)
    client = BackendApplicationClient(client_id=data["client_id"])
    oauth = OAuth2Session(client=client)
    try:
        token = oauth.fetch_token(**data)
        return token.get("access_token")
    except (requests.exceptions.HTTPError, requests.exceptions.Timeout):
        return get_client_credentials(host)


def http_request(
    host_creds, endpoint, retries=3, retry_interval=3, max_rate_limit_interval=60, **kwargs
):
    """
    Makes an HTTP request with the specified method to the specified hostname/endpoint. Ratelimit
    error code (429) will be retried with an exponential back off (1, 2, 4, ... seconds) for at most
    `max_rate_limit_interval` seconds.  Internal errors (500s) will be retried up to `retries` times
    , waiting `retry_interval` seconds between successive retries. Parses the API response
    (assumed to be JSON) into a Python object and returns it.

    :param host_creds: A :py:class:`mlflow.rest_utils.MlflowHostCreds` object containing
        hostname and optional authentication.
    :return: Parsed API response
    """
    hostname = host_creds.host
    auth_str = None
    if host_creds.username and host_creds.password:
        basic_auth_str = ("%s:%s" % (host_creds.username, host_creds.password)).encode("utf-8")
        auth_str = "Basic " + base64.standard_b64encode(basic_auth_str).decode("utf-8")
    elif host_creds.token:
        auth_str = "Bearer %s" % host_creds.token
    elif host_creds.oath_kwargs:
        auth_str = "Bearer %s" % get_client_credentials(host_creds)

    from mlflow.tracking.request_header.registry import resolve_request_headers

    headers = dict({**_DEFAULT_HEADERS, **resolve_request_headers()})
    if auth_str:
        headers["Authorization"] = auth_str

    if host_creds.server_cert_path is None:
        verify = not host_creds.ignore_tls_verification
    else:
        verify = host_creds.server_cert_path

    if host_creds.client_cert_path is not None:
        kwargs["cert"] = host_creds.client_cert_path

    def request_with_ratelimit_retries(max_rate_limit_interval, **kwargs):
        response = requests.request(**kwargs)
        time_left = max_rate_limit_interval
        sleep = 1
        while response.status_code == 429 and time_left > 0:
            _logger.warning(
                "API request to {path} returned status code 429 (Rate limit exceeded). "
                "Retrying in %d seconds. "
                "Will continue to retry 429s for up to %d seconds.",
                sleep,
                time_left,
            )
            time.sleep(sleep)
            time_left -= sleep
            response = requests.request(**kwargs)
            sleep = min(time_left, sleep * 2)  # sleep for 1, 2, 4, ... seconds;
        return response

    cleaned_hostname = strip_suffix(hostname, "/")
    url = "%s%s" % (cleaned_hostname, endpoint)
    for i in range(retries):
        response = request_with_ratelimit_retries(
            max_rate_limit_interval, url=url, headers=headers, verify=verify, **kwargs
        )
        if response.status_code >= 200 and response.status_code < 500:
            return response
        else:
            _logger.error(
                "API request to %s failed with code %s != 200, retrying up to %s more times. "
                "API response body: %s",
                url,
                response.status_code,
                retries - i - 1,
                response.text,
            )
            time.sleep(retry_interval)
    raise MlflowException(
        "API request to %s failed to return code 200 after %s tries" % (url, retries)
    )


def _can_parse_as_json(string):
    try:
        json.loads(string)
        return True
    except Exception:
        return False


def http_request_safe(host_creds, endpoint, **kwargs):
    """
    Wrapper around ``http_request`` that also verifies that the request succeeds with code 200.
    """
    response = http_request(host_creds=host_creds, endpoint=endpoint, **kwargs)
    return verify_rest_response(response, endpoint)


def verify_rest_response(response, endpoint):
    """Verify the return code and format, raise exception if the request was not successful."""
    if response.status_code != 200:
        if _can_parse_as_json(response.text):
            raise RestException(json.loads(response.text))
        else:
            base_msg = "API request to endpoint %s failed with error code " "%s != 200" % (
                endpoint,
                response.status_code,
            )
            raise MlflowException("%s. Response body: '%s'" % (base_msg, response.text))

    # Skip validation for endpoints (e.g. DBFS file-download API) which may return a non-JSON
    # response
    if endpoint.startswith(_REST_API_PATH_PREFIX) and not _can_parse_as_json(response.text):
        base_msg = (
            "API request to endpoint was successful but the response body was not "
            "in a valid JSON format"
        )
        raise MlflowException("%s. Response body: '%s'" % (base_msg, response.text))

    return response


def _get_path(path_prefix, endpoint_path):
    return "{}{}".format(path_prefix, endpoint_path)


def extract_api_info_for_service(service, path_prefix):
    """ Return a dictionary mapping each API method to a tuple (path, HTTP method)"""
    service_methods = service.DESCRIPTOR.methods
    res = {}
    for service_method in service_methods:
        endpoints = service_method.GetOptions().Extensions[databricks_pb2.rpc].endpoints
        endpoint = endpoints[0]
        endpoint_path = _get_path(path_prefix, endpoint.path)
        res[service().GetRequestClass(service_method)] = (endpoint_path, endpoint.method)
    return res


def call_endpoint(host_creds, endpoint, method, json_body, response_proto):
    # Convert json string to json dictionary, to pass to requests
    if json_body:
        json_body = json.loads(json_body)
    if method == "GET":
        response = http_request(
            host_creds=host_creds, endpoint=endpoint, method=method, params=json_body
        )
    else:
        response = http_request(
            host_creds=host_creds, endpoint=endpoint, method=method, json=json_body
        )
    response = verify_rest_response(response, endpoint)
    js_dict = json.loads(response.text)
    parse_dict(js_dict=js_dict, message=response_proto)
    return response_proto


# Response codes that generally indicate transient network failures and merit client retries,
# based on guidance from cloud service providers
# (https://docs.microsoft.com/en-us/azure/architecture/best-practices/retry-service-specific#general-rest-and-retry-guidelines)
TRANSIENT_FAILURE_RESPONSE_CODES = [
    408,  # Request Timeout
    429,  # Too Many Requests
    500,  # Internal Server Error
    502,  # Bad Gateway
    503,  # Service Unavailable
    504,  # Gateway Timeout
]


@contextmanager
def cloud_storage_http_request(method, *args, **kwargs):
    """
    Performs an HTTP PUT/GET request using Python's `requests` module with an automatic retry
    policy of `retry_attempts` using exponential backoff for the following response codes:

        - 408 (Request Timeout)
        - 429 (Too Many Requests)
        - 500 (Internal Server Error)
        - 502 (Bad Gateway)
        - 503 (Service Unavailable)
        - 504 (Gateway Timeout)

    :method: string of 'PUT' or 'GET', specify to do http PUT or GET
    :args: Positional arguments to pass to `requests.Session.put/get()`
    :kwargs: Keyword arguments to pass to `requests.Session.put/get()`
    """
    retry_attempts = kwargs.get("retry_attempts", 5)
    retry_strategy = Retry(
        total=None,
        # Don't retry on connect-related errors raised before a request reaches a remote server
        connect=0,
        # Retry once for errors reading the response from a remote server
        read=1,
        # Limit the number of redirects to avoid infinite redirect loops
        redirect=3,
        # Retry a specified number of times for response codes indicating transient failures
        status=retry_attempts,
        status_forcelist=TRANSIENT_FAILURE_RESPONSE_CODES,
        backoff_factor=1,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    with requests.Session() as http:
        http.mount("https://", adapter)
        http.mount("http://", adapter)
        if method.lower() == "put":
            response = http.put(*args, **kwargs)
        elif method.lower() == "get":
            response = http.get(*args, **kwargs)
        else:
            raise ValueError("Illegal http method: " + method)

        with response as r:
            yield r


class MlflowHostCreds(object):
    """
    Provides a hostname and optional authentication for talking to an MLflow tracking server.
    :param host: Hostname (e.g., http://localhost:5000) to MLflow server. Required.
    :param username: Username to use with Basic authentication when talking to server.
        If this is specified, password must also be specified.
    :param password: Password to use with Basic authentication when talking to server.
        If this is specified, username must also be specified.
    :param token: Token to use with Bearer authentication when talking to server.
        If provided, user/password authentication will be ignored.
    :param ignore_tls_verification: If true, we will not verify the server's hostname or TLS
        certificate. This is useful for certain testing situations, but should never be
        true in production.
        If this is set to true ``server_cert_path`` must not be set.
    :param client_cert_path: Path to ssl client cert file (.pem).
        Sets the cert param of the ``requests.request``
        function (see https://requests.readthedocs.io/en/master/api/).
    :param server_cert_path: Path to a CA bundle to use.
        Sets the verify param of the ``requests.request``
        function (see https://requests.readthedocs.io/en/master/api/).
        If this is set ``ignore_tls_verification`` must be false.
    """

    def __init__(
        self,
        host,
        username=None,
        password=None,
        token=None,
        ignore_tls_verification=False,
        client_cert_path=None,
        server_cert_path=None,
        oath_kwargs=None,
    ):
        if not host:
            raise MlflowException(
                message="host is a required parameter for MlflowHostCreds",
                error_code=INVALID_PARAMETER_VALUE,
            )
        if ignore_tls_verification and (server_cert_path is not None):
            raise MlflowException(
                message=(
                    "When 'ignore_tls_verification' is true then 'server_cert_path' "
                    "must not be set! This error may have occurred because the "
                    "'MLFLOW_TRACKING_INSECURE_TLS' and 'MLFLOW_TRACKING_SERVER_CERT_PATH' "
                    "environment variables are both set - only one of these environment "
                    "variables may be set."
                ),
                error_code=INVALID_PARAMETER_VALUE,
            )
        self.host = host
        self.username = username
        self.password = password
        self.token = token
        self.ignore_tls_verification = ignore_tls_verification
        self.client_cert_path = client_cert_path
        self.server_cert_path = server_cert_path
        self.oath_kwargs = oath_kwargs
