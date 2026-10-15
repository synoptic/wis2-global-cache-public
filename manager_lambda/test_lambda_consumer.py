"""Core decisions of the manager Lambda: which failures SQS retries, and when a retried failure is final.

Run inside a container (see README "Operations"); importing the module creates boto3 and Redis clients
(Redis connects lazily, so no server is needed).
"""
import os

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import pytest
import redis
import requests
from botocore.exceptions import ClientError

import wis2_lambda_consumer as c
from wis2_message import DownloadError, IntegrityError


def download_error(cause=None, status=None, tls=False):
    try:
        raise DownloadError("failed", status=status, tls=tls) from cause
    except DownloadError as e:
        return e


@pytest.mark.parametrize("error,expected", [
    (download_error(requests.exceptions.ConnectionError()), True),
    (download_error(requests.exceptions.Timeout()), True),
    (download_error(requests.exceptions.RetryError()), True),
    (download_error(status=503), True),
    (download_error(status=429), True),
    (download_error(status=408), True),
    (download_error(status=404), False),
    (download_error(status=403), False),
    (download_error(requests.exceptions.SSLError(), tls=True), False),
    (IntegrityError("checksum failed"), False),
    (c.PublishError("broker down"), True),
    (redis.exceptions.ConnectionError(), True),
    (ClientError({"Error": {"Code": "SlowDown"}}, "PutObject"), True),
    (KeyError("properties"), False),
])
def test_is_transient(error, expected):
    assert c.is_transient(error) is expected


@pytest.mark.parametrize("receive_count,expected", [
    ("1", False),
    ("2", True),
    ("3", True),
    (None, True),
    ("not-a-number", True),
])
def test_is_final_attempt(receive_count, expected, monkeypatch):
    monkeypatch.setattr(c, "sqs_max_receive_count", 2)
    assert c.is_final_attempt(receive_count) is expected


def test_unset_max_receive_count_treats_every_attempt_as_final(monkeypatch):
    monkeypatch.setattr(c, "sqs_max_receive_count", 1)
    assert c.is_final_attempt("1")
