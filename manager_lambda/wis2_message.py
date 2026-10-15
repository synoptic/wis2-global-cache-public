import base64
import hashlib
import io
import json
import os
import traceback
import urllib
from copy import deepcopy
from enum import Enum
from typing import Any
from uuid import uuid4
from datetime import datetime as dt
import requests
import boto3
import shutil
from wis2_monitor import strip_url_query, strip_url_queries


class DownloadError(Exception):
    """The resource could not be downloaded from its data server.

    Attributes:
        status: HTTP status code, or None when no response was received.
        tls: True when the TLS handshake or certificate verification failed.
    """

    def __init__(self, message: str, status: int | None = None, tls: bool = False):
        super().__init__(message)
        self.status = status
        self.tls = tls


class IntegrityError(Exception):
    """The resource failed integrity validation."""


class TooLargeToCache(Exception):
    """The resource exceeds the size this Global Cache will cache."""


class ContentDecodeError(Exception):
    """Embedded content has an unknown encoding, cannot be decoded, or does not match its size property."""


def nested_get(d: dict, keys: list) -> Any:
    """Gets value of nested key/s in dict.

    Args:
        d: Dictionary to search.
        keys: List of keys to traverse.

    Returns:
        Value of nested key or None if not found.
    """
    for key in keys:
        try:
            d = d[key]
        except KeyError:
            # print(f'key "{key}" not found in {d}')
            return None
    return d


# class to represent each WIS2 message
class Wis2Message:
    # not using json schema validation at this time, but could
    # https://github.com/wmo-im/wis2-notification-message/blob/main/schemas/wis2-notification-message-bundled.json
    def __init__(self, msg_data: dict, env: dict = None):
        """Initializes Wis2Message.

        Args:
            msg_data: The WIS2 message dictionary.
            env: Environment configuration dictionary.
        """
        self.pubtime_epoch = None
        self.env = env
        self.msg = msg_data
        self.init_parse()
        self.new_uuid = uuid4().__str__()
        self.new_topic = self.topic.replace('origin', 'cache')
        self.is_valid = None
        self.dataserver = None
        self.src_link = self.get_source_link()

    def init_parse(self):
        """Initializes parsing of WIS2 message and sets attributes.

        Raises:
            Exception: If required keys are missing.
        """
        required_keys = {
            'id': ['id'],
            'data_id': ['properties', 'data_id'],
            # 'metadata_id': ['properties', 'metadata_id'],
            'topic': ['topic'],
            'links': ['links'],
            'pubtime': ['properties', 'pubtime']
        }
        for k, v in required_keys.items():
            prop_val = nested_get(self.msg, v)
            if prop_val is None:
                raise Exception(f"required key {k} missing in message {self.msg}")
            setattr(self, k, prop_val)
        setattr(self, 'integrity_block', nested_get(self.msg, ['properties', 'integrity']))
        # setattr(self, "unique_id", self.data_id + self.pubtime)
        setattr(self, 'do_cache', self.check_cache())
        try:
            self.pubtime_epoch = dt.strptime(self.pubtime, '%Y-%m-%dT%H:%M:%SZ').timestamp()
        except:
            # limit decimal places
            if len(self.pubtime.split(':')) == 4:
                # then the last colon should be a decimal - replace with period
                self.pubtime = self.pubtime.rsplit(':', 1)[0] + '.' + self.pubtime.rsplit(':', 1)[1]
            dt_parts = self.pubtime.split('.')
            dt_seconds = dt_parts[1]
            if len(dt_seconds) > 4:
                dt_seconds = dt_seconds[:3]+"Z"
            new_dt = ".".join([dt_parts[0], dt_seconds])
            self.pubtime_epoch = dt.strptime(new_dt, '%Y-%m-%dT%H:%M:%S.%fZ').timestamp()

    def get_source_link(self) -> str:
        """Extracts source link from message.

        Returns:
            Source URL href string.

        Raises:
            TypeError: If no canonical or update link found.
            ValueError: If URL cannot be parsed.
        """
        # Find appropriate link
        canonical_link = [link for link in self.links if link['rel'] == 'canonical']
        update_link = [link for link in self.links if link['rel'] == 'update']
        # support deletion links as well
        delete_link = [link for link in self.links if link['rel'] == 'deletion']
        if delete_link:
            src_link = delete_link[0]
        else:
            src_link = update_link[0] if update_link else canonical_link[0] if canonical_link else None

        if not src_link:
            raise TypeError("missing src link")

        link_href = src_link['href']

        # basic sanity check
        if not link_href or not link_href.strip():
            raise ValueError(f"Empty URL in message {self.data_id}")

        # extract components
        try:
            parsed = urllib.parse.urlparse(link_href)
            # handle edge cases gracefully
            path = parsed.path.rstrip('/')
            filename = path.split('/')[-1] if path else ''

            # Log warning for edge cases but don't fail
            if not filename:
                print(f"Warning: Could not extract filename from URL {strip_url_query(link_href)} in message {self.data_id}")
                filename = 'unknown'

            if not parsed.netloc:
                raise ValueError(f"Invalid URL structure (no hostname): {strip_url_query(link_href)}")

            # Set attributes
            self.src_link = src_link
            self.filename = urllib.parse.unquote(filename)  # Decode percent-encoded filenames
            self.dataserver = parsed.netloc

            return link_href

        except Exception as e:
            # Add context for debugging
            raise ValueError(f"Failed to parse URL for message {self.data_id}: {e}") from e

    def check_cache(self) -> bool:
        """Checks if the message should be cached.

        Returns:
            True if message should be cached, False otherwise.
        """
        # first check if is delete message
        delete_link = [link for link in self.links if link['rel'] == 'deletion']
        if delete_link:
            return False
        # check if cache property exists and or is set
        cache_msg_value = nested_get(self.msg, ['properties', 'cache'])
        if cache_msg_value is None: cache_msg_value = True
        if not cache_msg_value or cache_msg_value == 'false':
            return False
        else:
            return True

    def is_unique(self, last_cache: float | None) -> bool:
        """Determines if the message is new compared to cache.

        Args:
            last_cache: Epoch time of last cache or None.

        Returns:
            True if message is unique/new, False otherwise.
        """
        # check if cache property exists
        if last_cache is None:
            return True
        last_cache = float(last_cache)
        if self.pubtime_epoch <= last_cache:
            return False
        else:
            # check if update or delete link exists
            if any([link for link in self.links if link['rel'] in ['update', 'deletion']]):
                return True
            else:
                return False
        return True

    def cache_msg_data(self, use_content: bool = False) -> bytes:
        """Caches message data from content or download.

        Args:
            use_content: If True, use inline content; else download.

        Returns:
            The data bytes.

        Raises:
            ContentDecodeError: Embedded content is invalid (see decode_content).
        """
        dnld_link = self.src_link
        dndld_keys = {'content': ['properties', 'content', 'value'],
                      'encoding': ['properties', 'content', 'encoding'],
                      'size': ['properties', 'content', 'size']}
        for k, v in dndld_keys.items():
            prop_val = nested_get(self.msg, v)
            setattr(self, k, prop_val)
        data_bytes = None
        # gzip is a valid WNM encoding (RFC 1952 then base64) that is not decoded here, so untrusted input is never
        # decompressed; the resource is downloaded from the URL instead (Guide 2.7.4.1 makes extraction optional)
        self.used_content = bool(use_content and self.content and self.encoding != 'gzip')
        if self.used_content:
            data_bytes = self.decode_content()
        else:
            data_file = self.download_file(dnld_link)
            with open(data_file, "rb") as file:
                data_bytes = file.read()
        # set attribute
        setattr(self, 'data_bytes', data_bytes)
        # set integrity block if missing in the msg
        self.set_integrity_block(data_bytes)
        return data_bytes

    def decode_content(self) -> bytes:
        """Decodes utf-8 or base64 embedded content and checks it against ``properties.content.size``.

        Returns:
            The decoded bytes.

        Raises:
            ContentDecodeError: Unknown encoding, a value that is not valid base64 or not a string, or a decoded
                length different from ``size`` (the unencoded size, WNM 1.15).
        """
        if not isinstance(self.content, str):
            raise ContentDecodeError(f"content value is not a string for {self.data_id}")
        if self.encoding not in ('base64', 'utf-8'):
            raise ContentDecodeError(f"unknown content encoding {self.encoding!r} for {self.data_id}")
        try:
            if self.encoding == 'base64':
                # line breaks are allowed (MIME-style wrapping); any other non-alphabet character is invalid
                data_bytes = base64.b64decode("".join(self.content.split()), validate=True)
            else:
                data_bytes = self.content.encode()
        except ValueError as e:
            # binascii.Error, non-ASCII base64 input, and lone surrogates in utf-8 are all ValueErrors
            raise ContentDecodeError(f"invalid {self.encoding} content for {self.data_id}: {e}") from e
        if isinstance(self.size, int) and len(data_bytes) != self.size:
            raise ContentDecodeError(f"decoded content is {len(data_bytes)} bytes but size is {self.size} "
                                     f"for {self.data_id}")
        return data_bytes

    # function to set the integrity block if it is missing from the message
    def set_integrity_block(self, data_bytes: bytes) -> dict:
        """Sets integrity block if missing.

        Args:
            data_bytes: The data to calculate hash for.

        Returns:
            The integrity block dictionary.
        """
        if self.integrity_block is None:
            # set the integrity block
            sh = hashlib.sha512()
            sh.update(data_bytes)
            b64_digest = base64.b64encode(sh.digest()).decode()
            # hex_digest = sh.hexdigest()
            self.msg['properties']['integrity'] = self.integrity_block = {
                "method": "sha512",
                "value": b64_digest
            }
        return self.integrity_block

    def format_cache_msg(self) -> dict:
        """Formats the message for caching.

        Returns:
            The modified message dictionary for the cache.
        """
        # message is the same, except for the msg_id (id is new uuid), links (new canonical link),
        # and change the topic channel to cache
        # "type" (mimetype) should be retained from the original message.

        cache_msg = self.msg
        # update msg_id
        cache_msg['id'] = self.new_uuid
        # set the global cache property
        cache_msg['properties']['global-cache'] = os.getenv('REPORT_BY')
        # drop the topic
        try:
            cache_msg.pop('topic')
        except:
            pass
        if self.do_cache:
            # overwrite the canonical link and update link if it exists
            for link_type in ['canonical', 'update']:
                links = [link for link in cache_msg['links'] if link['rel'] == link_type]
                if links:
                    original_link = cache_msg['links'].pop(cache_msg['links'].index(links[0]))
                    new_link = deepcopy(original_link)
                    new_link['href'] = self.dnld_url
                    cache_msg['links'].append(new_link)
        return cache_msg

    @staticmethod
    def get_dt_str() -> str:
        """Gets current datetime in RFC3339 format.

        Returns:
            Current datetime string.
        """
        return dt.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')

    def download_file(self, href: str, tmp_dir: str = '/tmp/') -> str:
        """Downloads file from URL to temporary directory.

        Args:
            href: URL to download from.
            tmp_dir: Directory to save to.

        Returns:
            Path to downloaded file.

        Raises:
            TooLargeToCache: If the resource exceeds free /tmp space or MAX_CACHE_BYTES.
            DownloadError: On HTTP or network failure.
            OSError: On local file errors.
        """
        session = requests.Session()
        # Configure limited retries to fail faster
        retries = requests.packages.urllib3.util.retry.Retry(
            total=2,  # Only retry once
            backoff_factor=0.5,  # Short delay between retries
            status_forcelist=[500, 502, 503, 504]  # Only retry on server errors
        )

        # Apply configuration to both HTTP and HTTPS connections
        session.mount('http://', requests.adapters.HTTPAdapter(max_retries=retries))
        session.mount('https://', requests.adapters.HTTPAdapter(max_retries=retries))

        dev_mode = os.environ.get('DEV-MODE', 'False') in ['True', 'true', '1']
        tmp_path = os.path.join(tmp_dir, self.filename)
        max_bytes = shutil.disk_usage(tmp_dir).free
        memory_mb = os.environ.get('AWS_LAMBDA_FUNCTION_MEMORY_SIZE')
        # Default cap is 25% of Lambda memory: the body is held in memory and S3 multipart upload adds buffers.
        # 25% is an unmeasured estimate (ADR 0002). MAX_CACHE_BYTES overrides it; free /tmp always applies.
        # Larger items are not cached; they are republished with origin links plus an item.cache WME.
        configured_max = os.environ.get('MAX_CACHE_BYTES') or (
            int(memory_mb) * 1024 * 1024 // 4 if memory_mb else None)
        if configured_max:
            max_bytes = min(max_bytes, int(configured_max))
        try:
            # timeout=(10, 30) means: 10s connection timeout, 30s read timeout
            with session.get(href, stream=True, timeout=(10, 30), verify=not dev_mode) as r:
                r.raise_for_status()
                expected_size = int(r.headers.get('content-length', 0))
                downloaded_size = 0

                # decided before reading the body so an oversize resource is a cache decision, not a failure
                if expected_size > max_bytes:
                    raise TooLargeToCache(f"resource is {expected_size} bytes; cache limit is {max_bytes} bytes")
                with open(tmp_path, 'wb') as f:
                    # Increase chunk size for better performance with larger files
                    for chunk in r.iter_content(chunk_size=32768):  # 32KB chunks
                        if chunk:
                            downloaded_size += len(chunk)
                            # content-length may be absent or wrong
                            if downloaded_size > max_bytes:
                                raise TooLargeToCache(f"resource exceeds cache limit of {max_bytes} bytes")
                            f.write(chunk)

                # Verify download completeness for files with reported size
                # too aggressive...
                # if expected_size > 0 and downloaded_size != expected_size:
                #     raise IOError(f"Downloaded size ({downloaded_size}) doesn't match expected size ({expected_size})")

                # set attribute for deletion later
                setattr(self, 'tmp_path', tmp_path)
                return tmp_path
        except TooLargeToCache:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
        except requests.exceptions.RequestException as e:
            print(f"Failed to download file from {strip_url_query(href)}: {strip_url_queries(e)}")
            # Clean up partial download
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            # str(e) can embed the URL with its presigned query string, so it is not carried into the message
            status = getattr(getattr(e, 'response', None), 'status_code', None)
            raise DownloadError(f"{type(e).__name__}" + (f": HTTP {status}" if status else ""), status=status,
                                tls=isinstance(e, requests.exceptions.SSLError)) from e
        except (IOError, OSError) as e:
            print(f"I/O error while downloading {strip_url_query(href)}: {strip_url_queries(e)}")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    def validate_integrity(self) -> bool:
        """Validates data integrity against checksum.

        Returns:
            True if valid.

        Raises:
            IntegrityError: If hashing method unsupported or checksum fails.
        """
        input_bytes = self.data_bytes
        valid_methods = {
            "sha256": hashlib.sha256,
            "sha384": hashlib.sha384,
            "sha512": hashlib.sha512,
            "sha3-256": hashlib.sha3_256,
            "sha3-384": hashlib.sha3_384,
            "sha3-512": hashlib.sha3_512
        }
        method = self.integrity_block["method"]
        if method not in valid_methods:
            raise IntegrityError(f"Unsupported hashing method: {method}")

        sh = valid_methods[method]()
        sh.update(input_bytes)
        b64_digest = base64.b64encode(sh.digest()).decode()
        hex_digest = sh.hexdigest()
        if self.integrity_block["value"] not in [b64_digest, hex_digest]:
            setattr(self, 'is_valid', False)
            raise IntegrityError(f"checksum failed for: {self.data_id}")
        setattr(self, 'is_valid', True)
        return True

    def format_s3_key(self) -> str:
        """Formats S3 bucket key path.

        Returns:
            The S3 bucket key path.
        """
        # include a /data prefix for all cached objects
        data_prefix = 'data'
        topic_pieces = self.topic.split('/')
        # get wis2 index from topic_pieces
        topic_prefix = "/".join(topic_pieces[topic_pieces.index('wis2') + 1:])
        s3_key = "/".join([data_prefix, topic_prefix, self.filename])
        return s3_key

    def upload_to_bucket(self, data_bytes: bytes) -> str:
        """Uploads data bytes to S3 bucket.

        Args:
            data_bytes: Data to upload.

        Returns:
            The bucket path key.
        """

        s3_client_obj = boto3.client('s3')
        s3_key = self.format_s3_key()
        # construct download url
        dnld_url = os.path.join(f"https://{self.env['s3_bucket_name']}.s3.amazonaws.com",
                                s3_key)
        setattr(self, 'dnld_url', dnld_url)

        if os.environ.get('DEV-MODE', 'False') in ['True', 'true', '1']:
            print(f"dev no upload: {s3_key}")
            return s3_key
        s3_client_obj.upload_fileobj(Fileobj=io.BytesIO(data_bytes), Bucket=self.env['s3_bucket_name'],
                                     Key=s3_key)
        return s3_key

